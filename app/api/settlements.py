from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import forbidden, not_found, validation
from app.ids import utcnow
from app.models import Attachment, Expense, ExpenseShare, Period, Settlement, User
from app.schemas.common import SettlementOut, SettlementPage, ShareRef
from app.security import current_user
from app.serializers import UserCache, paginate, settlement_out
from app.services import access, audit
from app.services.periods import period_for_date

router = APIRouter(prefix="/settlements", tags=["settlements"])


class SettlementIn(BaseModel):
    group_id: str
    period_id: str | None = None
    from_user_id: str
    to_user_id: str
    amount: int = Field(gt=0)
    settled_on: date
    note: str | None = None
    linked_shares: list[ShareRef] = []
    attachment_ids: list[str] = []


class SettlementPatch(BaseModel):
    amount: int | None = Field(None, gt=0)
    settled_on: date | None = None
    note: str | None = None
    linked_shares: list[ShareRef] | None = None
    attachment_ids: list[str] | None = None


def _link_shares(db: Session, s: Settlement, refs: list[ShareRef], actor: User) -> None:
    for old in db.scalars(select(ExpenseShare).where(ExpenseShare.settlement_id == s.id)):
        if ShareRef(expense_id=old.expense_id, user_id=old.user_id) not in refs:
            old.settlement_id = old.checked_at = old.checked_by_id = None
    for ref in refs:
        share = db.get(ExpenseShare, (ref.expense_id, ref.user_id))
        e = db.get(Expense, ref.expense_id)
        if share is None or e is None or e.deleted_at is not None:
            raise validation("Unknown share", linked_shares=f"{ref.expense_id}/{ref.user_id}")
        if e.group_id != s.group_id or share.user_id != s.from_user_id or e.payer_id != s.to_user_id:
            raise validation(
                "Linked shares must be the payer's debts to the receiver in this group",
                linked_shares=f"{ref.expense_id}/{ref.user_id}",
            )
        if share.settlement_id and share.settlement_id != s.id:
            raise validation("Share is already covered by another payment", linked_shares=ref.expense_id)
        share.settlement_id = s.id
        share.checked_at = share.checked_at or utcnow()
        share.checked_by_id = share.checked_by_id or actor.id


def _link_attachments(db: Session, ids: list[str], s: Settlement, user: User, replace: bool) -> None:
    if replace:
        for a in db.scalars(select(Attachment).where(Attachment.owner_type == "settlement", Attachment.owner_id == s.id)):
            if a.id not in ids:
                a.owner_type = a.owner_id = None
    for aid in ids:
        a = db.get(Attachment, aid)
        if a is None or a.deleted_at is not None:
            raise validation("Unknown attachment", attachment_ids=aid)
        if a.owner_id == s.id:
            continue
        if a.owner_id is not None or a.uploaded_by_id != user.id:
            raise validation("Attachment is not yours or already linked", attachment_ids=aid)
        a.owner_type, a.owner_id = "settlement", s.id


@router.get("", response_model=SettlementPage)
def list_settlements(
    group_id: str | None = None, period_id: str | None = None, involving: str = Query("any", pattern="^(me|any)$"),
    limit: int = Query(50, ge=1, le=200), cursor: str | None = None,
    user: User = Depends(current_user), db: Session = Depends(get_db, scope="function"),
):
    mine = or_(Settlement.from_user_id == user.id, Settlement.to_user_id == user.id)
    stmt = select(Settlement).where(Settlement.deleted_at.is_(None))
    if involving == "me":
        stmt = stmt.where(mine)
    else:
        stmt = stmt.where(or_(Settlement.group_id.in_(access.active_group_ids(db, user.id)), mine))
    if group_id:
        stmt = stmt.where(Settlement.group_id == group_id)
    if period_id:
        stmt = stmt.where(Settlement.period_id == period_id)
    stmt = stmt.order_by(Settlement.settled_on.desc(), Settlement.id.desc())
    rows, nxt = paginate(db, stmt, cursor, limit)
    users = UserCache(db)
    return SettlementPage(items=[settlement_out(db, s, users) for s in rows], next_cursor=nxt)


@router.post("", response_model=SettlementOut, status_code=201)
def create_settlement(body: SettlementIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    group, m = access.get_group_for_member(db, body.group_id, user)
    if user.id not in (body.from_user_id, body.to_user_id) and m.role not in ("owner", "admin"):
        raise forbidden("Only the payer, the receiver, or a group admin can record a payment")
    if body.from_user_id == body.to_user_id:
        raise validation("From and to must differ", to_user_id="same as from")
    for uid, f in ((body.from_user_id, "from_user_id"), (body.to_user_id, "to_user_id")):
        u = db.get(User, uid)
        if u is None or u.merged_into_id:
            raise validation("Unknown person", **{f: "unknown"})
    if body.period_id:
        period = db.get(Period, body.period_id)
        if period is None or period.group_id != group.id:
            raise validation("Unknown period", period_id="not in this group")
    else:
        period = period_for_date(db, group, body.settled_on, for_write=False)
    s = Settlement(
        group_id=group.id, period_id=period.id, from_user_id=body.from_user_id, to_user_id=body.to_user_id,
        amount=body.amount, settled_on=body.settled_on, note=body.note, created_by_id=user.id,
    )
    db.add(s)
    db.flush()
    _link_shares(db, s, body.linked_shares, user)
    _link_attachments(db, body.attachment_ids, s, user, replace=False)
    db.flush()
    audit.log(db, user.id, "settlement", s.id, "create", {"amount": s.amount})
    return settlement_out(db, s)


def _visible(db: Session, settlement_id: str, user: User) -> Settlement:
    s = db.get(Settlement, settlement_id)
    if s is None or s.deleted_at is not None:
        raise not_found("Settlement")
    if access.membership(db, s.group_id, user.id) is None and user.id not in (s.from_user_id, s.to_user_id):
        raise not_found("Settlement")
    return s


def _editable(db: Session, settlement_id: str, user: User) -> Settlement:
    s = _visible(db, settlement_id, user)
    if user.id not in (s.created_by_id, s.from_user_id, s.to_user_id):
        raise forbidden("Only the creator, payer or receiver can change this payment")
    return s


@router.get("/{settlement_id}", response_model=SettlementOut)
def get_settlement(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    return settlement_out(db, _visible(db, settlement_id, user))


@router.patch("/{settlement_id}", response_model=SettlementOut)
def patch_settlement(settlement_id: str, body: SettlementPatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    s = _editable(db, settlement_id, user)
    data = body.model_dump(exclude_unset=True)
    if data.get("amount"):
        s.amount = data["amount"]
    if data.get("settled_on"):
        s.settled_on = data["settled_on"]
    if "note" in data:
        s.note = data["note"]
    if body.linked_shares is not None:
        _link_shares(db, s, body.linked_shares, user)
    if body.attachment_ids is not None:
        _link_attachments(db, body.attachment_ids, s, user, replace=True)
    db.flush()
    audit.log(db, user.id, "settlement", s.id, "update", {k: str(v) for k, v in data.items()})
    return settlement_out(db, s)


@router.delete("/{settlement_id}", status_code=204)
def delete_settlement(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    s = _editable(db, settlement_id, user)
    _link_shares(db, s, [], user)  # unchecks shares this payment covered
    s.deleted_at = utcnow()
    audit.log(db, user.id, "settlement", s.id, "delete")


class AttachIn(BaseModel):
    attachment_ids: list[str]


@router.post("/{settlement_id}/attachments", response_model=SettlementOut)
def attach(settlement_id: str, body: AttachIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    s = _visible(db, settlement_id, user)
    _link_attachments(db, body.attachment_ids, s, user, replace=False)
    db.flush()
    return settlement_out(db, s)
