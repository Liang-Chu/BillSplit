from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import APIError, forbidden, not_found, validation
from app.ids import utcnow
from app.models import (
    Attachment,
    Expense,
    ExpenseItem,
    ExpenseItemParticipant,
    ExpenseShare,
    ExpenseType,
    Group,
    GroupMember,
    Period,
    User,
)
from app.schemas.common import ExpenseOut, ExpensePage
from app.security import current_user
from app.serializers import UserCache, expense_out, paginate
from app.services import access, audit
from app.services.periods import period_for_date
from app.services.resolve import resolve
from app.services.splitting import ItemInput, ParticipantInput, compute_shares

router = APIRouter(prefix="/expenses", tags=["expenses"])


class ParticipantIn(BaseModel):
    user_id: str
    amount: int | None = Field(None, ge=0, description="exact split: share in minor units")
    percent: str | None = Field(None, description='percent split: e.g. "33.33"')
    weight: str | None = Field(None, description='shares split: e.g. "2"')


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    amount: int = Field(ge=0)
    participant_ids: list[str] = []


class ExpenseIn(BaseModel):
    group_id: str
    title: str = Field(min_length=1, max_length=200)
    merchant: str | None = Field(None, max_length=200)
    amount: int = Field(gt=0)
    expense_date: date
    expense_type_id: str | None = None
    payer_id: str
    split_method: str = Field("equal", pattern="^(equal|exact|percent|shares|itemized)$")
    participants: list[ParticipantIn] = Field(min_length=1)
    items: list[ItemIn] = []
    notes: str | None = None
    attachment_ids: list[str] = []
    ocr_result: dict | None = None


class ExpensePatch(BaseModel):
    group_id: str | None = None
    title: str | None = Field(None, min_length=1, max_length=200)
    merchant: str | None = Field(None, max_length=200)
    amount: int | None = Field(None, gt=0)
    expense_date: date | None = None
    expense_type_id: str | None = None
    payer_id: str | None = None
    split_method: str | None = Field(None, pattern="^(equal|exact|percent|shares|itemized)$")
    participants: list[ParticipantIn] | None = None
    items: list[ItemIn] | None = None
    notes: str | None = None
    attachment_ids: list[str] | None = None


class SplitPreviewIn(BaseModel):
    amount: int = Field(gt=0)
    split_method: str = Field("equal", pattern="^(equal|exact|percent|shares|itemized)$")
    participants: list[ParticipantIn] = Field(min_length=1)
    items: list[ItemIn] = []
    payer_id: str | None = None


def _shares(amount: int, method: str, participants: list[ParticipantIn], items: list[ItemIn]):
    return compute_shares(
        amount, method,
        [ParticipantInput(p.user_id, p.amount, p.percent, p.weight) for p in participants],
        [ItemInput(i.name, i.amount, i.participant_ids) for i in items] if method == "itemized" else None,
    )


def _live_user(db: Session, uid: str, field: str) -> User:
    u = db.get(User, uid)
    if u is None or u.merged_into_id:
        raise validation("Unknown person", **{field: f"unknown user {uid}"})
    return u


def _check_type(db: Session, type_id: str | None, owner_id: str) -> None:
    if type_id:
        t = db.get(ExpenseType, type_id)
        if t is None or t.user_id != owner_id:
            raise validation("Unknown expense type", expense_type_id="not one of your types")


def _link_attachments(db: Session, ids: list[str], expense_id: str, user: User, replace: bool) -> None:
    if replace:
        for a in db.scalars(
            select(Attachment).where(Attachment.owner_type == "expense", Attachment.owner_id == expense_id)
        ):
            if a.id not in ids:
                a.owner_type = a.owner_id = None
    for aid in ids:
        a = db.get(Attachment, aid)
        if a is None or a.deleted_at is not None:
            raise validation("Unknown attachment", attachment_ids=f"unknown {aid}")
        if a.owner_id == expense_id:
            continue
        if a.owner_id is not None or a.uploaded_by_id != user.id:
            raise validation("Attachment is not yours or already linked", attachment_ids=aid)
        a.owner_type, a.owner_id = "expense", expense_id


def _apply_split(db: Session, e: Expense, group: Group, participants: list[ParticipantIn], items: list[ItemIn]) -> None:
    for p in participants:
        _live_user(db, p.user_id, "participants")
    results = _shares(e.amount, e.split_method, participants, items)
    member_ids = set(
        db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group.id, GroupMember.left_at.is_(None)))
    )
    old = {s.user_id: s for s in e.shares}
    new_shares = []
    for r in results:
        prev = old.get(r.user_id)
        share = ExpenseShare(
            user_id=r.user_id, amount=r.amount, split_value=r.split_value,
            is_guest=r.user_id not in member_ids if prev is None else prev.is_guest,
        )
        if prev is not None and prev.amount == r.amount:  # keep checks when the amount didn't change
            share.checked_at, share.checked_by_id, share.settlement_id = prev.checked_at, prev.checked_by_id, prev.settlement_id
        new_shares.append(share)
    e.shares.clear()
    db.flush()
    e.shares.extend(new_shares)
    e.items.clear()
    db.flush()
    if e.split_method == "itemized":
        for pos, it in enumerate(items):
            item = ExpenseItem(name=it.name, amount=it.amount, position=pos)
            item.participants = [ExpenseItemParticipant(user_id=uid) for uid in sorted(set(it.participant_ids))]
            e.items.append(item)


def _check_payer(db: Session, payer_id: str, group: Group, participants: list[ParticipantIn]) -> None:
    _live_user(db, payer_id, "payer_id")
    if access.membership(db, group.id, payer_id) is None and payer_id not in {p.user_id for p in participants}:
        raise validation("Payer must be a group member or a participant", payer_id="not in group or participants")


class ResolveIn(BaseModel):
    merchant: str | None = None
    title: str | None = None
    expense_type_id: str | None = None
    group_id: str | None = Field(None, description="set when the user picked the group manually")


@router.post("/resolve")
def resolve_endpoint(body: ResolveIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """Suggested type and group. A suggestion only — the user may override both."""
    return resolve(db, user, body.merchant, body.title, body.expense_type_id, body.group_id)


@router.post("/preview-split")
def preview_split(body: SplitPreviewIn, _: User = Depends(current_user)):
    results = _shares(body.amount, body.split_method, body.participants, body.items)
    return {"shares": [{"user_id": r.user_id, "amount": r.amount} for r in results]}


@router.post("", response_model=ExpenseOut, status_code=201)
def create_expense(body: ExpenseIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    group, _ = access.get_group_for_member(db, body.group_id, user)
    if group.archived_at is not None:
        raise validation("Group is archived", group_id="archived")
    period = period_for_date(db, group, body.expense_date)
    _check_type(db, body.expense_type_id, user.id)
    _check_payer(db, body.payer_id, group, body.participants)
    e = Expense(
        group_id=group.id, period_id=period.id, expense_type_id=body.expense_type_id, title=body.title,
        merchant=body.merchant, amount=body.amount, expense_date=body.expense_date, payer_id=body.payer_id,
        split_method=body.split_method, notes=body.notes, ocr_result=body.ocr_result, created_by_id=user.id,
    )
    db.add(e)
    db.flush()
    _apply_split(db, e, group, body.participants, body.items)
    _link_attachments(db, body.attachment_ids, e.id, user, replace=False)
    db.flush()
    audit.log(db, user.id, "expense", e.id, "create", {"amount": e.amount, "group_id": group.id})
    return expense_out(db, e, user)


@router.get("", response_model=ExpensePage)
def list_expenses(
    group_id: list[str] | None = Query(None),
    period_id: str | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    involving: str = Query("any", pattern="^(me|any)$"),
    expense_type_id: str | None = None,
    checked: bool | None = None,
    q: str | None = None,
    sort: str = Query("-expense_date", pattern="^-?(expense_date|amount|created_at)$"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    my_groups = access.active_group_ids(db, user.id)
    mine = or_(
        Expense.payer_id == user.id,
        exists().where(ExpenseShare.expense_id == Expense.id, ExpenseShare.user_id == user.id),
    )
    stmt = select(Expense).where(Expense.deleted_at.is_(None))
    stmt = stmt.where(mine) if involving == "me" else stmt.where(or_(Expense.group_id.in_(my_groups), mine))
    if group_id:
        stmt = stmt.where(Expense.group_id.in_(group_id))
    if period_id:
        stmt = stmt.where(Expense.period_id == period_id)
    if from_:
        stmt = stmt.where(Expense.expense_date >= from_)
    if to:
        stmt = stmt.where(Expense.expense_date <= to)
    if expense_type_id:
        stmt = stmt.where(Expense.expense_type_id == expense_type_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Expense.title.ilike(like), Expense.merchant.ilike(like), Expense.notes.ilike(like)))
    if checked is not None:
        unchecked = exists().where(
            ExpenseShare.expense_id == Expense.id, ExpenseShare.user_id != Expense.payer_id,
            ExpenseShare.amount > 0, ExpenseShare.checked_at.is_(None),
        )
        stmt = stmt.where(~unchecked if checked else unchecked)
    col = getattr(Expense, sort.lstrip("-"))
    stmt = stmt.order_by(col.desc() if sort.startswith("-") else col.asc(), Expense.id.desc())
    rows, nxt = paginate(db, stmt, cursor, limit)
    users = UserCache(db)
    return ExpensePage(items=[expense_out(db, e, user, users, include_ocr=False) for e in rows], next_cursor=nxt)


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    return expense_out(db, access.get_visible_expense(db, expense_id, user), user)


def _editable(db: Session, expense_id: str, user: User) -> Expense:
    e = access.get_visible_expense(db, expense_id, user)
    if not access.can_edit_expense(db, e, user):
        raise forbidden("Only the creator, the payer, or the group owner can change this bill")
    if db.get(Period, e.period_id).status == "frozen":
        raise APIError(409, "period_frozen", "This bill's period is frozen", {"period_id": e.period_id})
    return e


@router.patch("/{expense_id}", response_model=ExpenseOut)
def patch_expense(expense_id: str, body: ExpensePatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e = _editable(db, expense_id, user)
    data = body.model_dump(exclude_unset=True)
    group = db.get(Group, e.group_id)
    if data.get("group_id") and data["group_id"] != e.group_id:
        group, _ = access.get_group_for_member(db, data["group_id"], user)
        e.group_id = group.id
    if "expense_type_id" in data:
        _check_type(db, data["expense_type_id"], user.id)
        e.expense_type_id = data["expense_type_id"]
    for f in ("title", "merchant", "notes"):
        if f in data:
            setattr(e, f, data[f])
    if data.get("expense_date") or "group_id" in data:
        e.expense_date = data.get("expense_date") or e.expense_date
        e.period_id = period_for_date(db, group, e.expense_date).id
    resplit = any(k in data for k in ("amount", "split_method", "participants", "items", "group_id"))
    if data.get("amount"):
        e.amount = data["amount"]
    if data.get("split_method"):
        e.split_method = data["split_method"]
    if resplit:
        participants = body.participants
        if participants is None:  # keep the current participants and their inputs
            participants = [_participant_from_share(e.split_method, s) for s in e.shares]
        items = body.items
        if items is None:
            items = [
                ItemIn(name=i.name, amount=i.amount, participant_ids=[p.user_id for p in i.participants]) for i in e.items
            ]
        payer = data.get("payer_id") or e.payer_id
        _check_payer(db, payer, group, participants)
        _apply_split(db, e, group, participants, items)
    if data.get("payer_id"):
        if not resplit:
            _check_payer(db, data["payer_id"], group, [ParticipantIn(user_id=s.user_id) for s in e.shares])
        e.payer_id = data["payer_id"]
    if body.attachment_ids is not None:
        _link_attachments(db, body.attachment_ids, e.id, user, replace=True)
    e.updated_at = utcnow()
    db.flush()
    audit.log(db, user.id, "expense", e.id, "update", {k: str(v) for k, v in data.items() if k != "participants"})
    return expense_out(db, e, user)


def _participant_from_share(method: str, s: ExpenseShare) -> ParticipantIn:
    if method == "exact":
        return ParticipantIn(user_id=s.user_id, amount=int(s.split_value) if s.split_value else s.amount)
    if method == "percent":
        return ParticipantIn(user_id=s.user_id, percent=s.split_value)
    if method == "shares":
        return ParticipantIn(user_id=s.user_id, weight=s.split_value or "1")
    return ParticipantIn(user_id=s.user_id)


@router.delete("/{expense_id}", status_code=204)
def delete_expense(expense_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e = _editable(db, expense_id, user)
    e.deleted_at = utcnow()
    audit.log(db, user.id, "expense", e.id, "delete")


def _share(db: Session, expense_id: str, user_id: str, user: User) -> tuple[Expense, ExpenseShare]:
    e = access.get_visible_expense(db, expense_id, user)
    s = db.get(ExpenseShare, (expense_id, user_id))
    if s is None:
        raise not_found("Share")
    if not access.can_check_share(e, user_id, user):
        raise forbidden("Only the payer or that person can tick this share")
    return e, s


@router.post("/{expense_id}/shares/{user_id}/check", response_model=ExpenseOut)
def check_share(expense_id: str, user_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e, s = _share(db, expense_id, user_id, user)
    if s.checked_at is None:
        s.checked_at, s.checked_by_id = utcnow(), user.id
        audit.log(db, user.id, "expense", e.id, "check", {"user_id": user_id})
    db.flush()
    return expense_out(db, e, user)


@router.delete("/{expense_id}/shares/{user_id}/check", response_model=ExpenseOut)
def uncheck_share(expense_id: str, user_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e, s = _share(db, expense_id, user_id, user)
    if s.checked_at is not None:
        s.checked_at = s.checked_by_id = s.settlement_id = None
        audit.log(db, user.id, "expense", e.id, "uncheck", {"user_id": user_id})
    db.flush()
    return expense_out(db, e, user)


class AttachIn(BaseModel):
    attachment_ids: list[str]


@router.post("/{expense_id}/attachments", response_model=ExpenseOut)
def attach(expense_id: str, body: AttachIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e = access.get_visible_expense(db, expense_id, user)
    _link_attachments(db, body.attachment_ids, e.id, user, replace=False)
    db.flush()
    return expense_out(db, e, user)


@router.delete("/{expense_id}/attachments/{attachment_id}", status_code=204)
def detach(expense_id: str, attachment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    e = access.get_visible_expense(db, expense_id, user)
    a = db.get(Attachment, attachment_id)
    if a is None or a.owner_id != e.id:
        raise not_found("Attachment")
    if not (access.can_edit_expense(db, e, user) or a.uploaded_by_id == user.id):
        raise forbidden()
    a.owner_type = a.owner_id = None
