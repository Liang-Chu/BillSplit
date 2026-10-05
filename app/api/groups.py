from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.currencies import CURRENCIES
from app.db import get_db
from app.errors import APIError, not_found, validation
from app.ids import join_code, utcnow
from app.models import Expense, Group, GroupMember, Period, PeriodBalance, PeriodMember, PeriodTransfer, User
from app.schemas.common import GroupOut, MemberOut, PeriodOut, Recurrence, UserOut
from app.security import current_user
from app.serializers import UserCache, group_out, paginate, user_out
from app.services import access, audit
from app.services.balances import compute_period_balances
from app.services.group_order import sync_group_members, sync_users
from app.services.people import check_claimable, group_placeholders, merge_into
from app.services.periods import apply_recurrence_change, cut_period, rollover_group
from app.services.seed import create_group

router = APIRouter(tags=["groups"])


def _check_currency(code: str) -> str:
    code = code.upper()
    if code not in CURRENCIES:
        raise validation("Unsupported currency", currency=f"one of {', '.join(sorted(CURRENCIES))}")
    return code


def _check_recurrence(r: Recurrence) -> Recurrence:
    try:
        ZoneInfo(r.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise validation("Unknown timezone", **{"recurrence.timezone": "must be an IANA timezone"})
    if r.cadence == "every_n_days" and not r.cadence_n:
        raise validation("cadence_n is required for every_n_days", **{"recurrence.cadence_n": "required"})
    if r.cadence == "weekly" and r.anchor_day is not None and r.anchor_day > 7:
        raise validation("Weekly anchor_day is 1 (Mon) to 7 (Sun)", **{"recurrence.anchor_day": "1-7"})
    return r


def _unique_join_code(db: Session, name: str) -> str:
    while True:
        code = join_code(name)
        if not db.scalar(select(Group.id).where(Group.join_code == code)):
            return code


# ---------------------------------------------------------------- groups


@router.get("/groups", response_model=dict[str, list[GroupOut]])
def list_groups(include_archived: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """Available groups in the caller's own order (see `/me/group-order`); archived groups follow when requested."""
    from app.services.group_order import sync

    rows = db.execute(
        select(Group, GroupMember.role)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .where(GroupMember.user_id == user.id, GroupMember.left_at.is_(None))
    ).all()
    by_id = {g.id: (g, role) for g, role in rows}
    ordered = [by_id[gid] for gid in sync(db, user.id).group_ids if gid in by_id]
    if include_archived:
        ordered += sorted(
            ((g, r) for g, r in rows if g.archived_at is not None), key=lambda gr: (gr[0].name, gr[0].id)
        )
    return {"items": [group_out(db, g, role, viewer_id=user.id) for g, role in ordered]}


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    is_recurring: bool = False
    recurrence: Recurrence | None = None


@router.post("/groups", response_model=GroupOut, status_code=201)
def create(body: GroupCreate, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    currency = _check_currency(body.currency)
    rec = None
    if body.is_recurring:
        rec = _check_recurrence(body.recurrence or Recurrence()).model_dump()
    g = create_group(
        db, user, name=body.name, description=body.description, currency=currency, recurrence=rec,
        join_code=_unique_join_code(db, body.name),
    )
    audit.log(db, user.id, "group", g.id, "create")
    return group_out(db, g, "owner", viewer_id=user.id)


@router.get("/groups/{group_id}", response_model=GroupOut)
def get_group(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    return group_out(db, g, m.role, viewer_id=user.id)


class GroupPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    currency: str | None = None
    is_recurring: bool | None = None
    recurrence: Recurrence | None = None
    join_enabled: bool | None = None


@router.patch("/groups/{group_id}", response_model=GroupOut)
def patch_group(group_id: str, body: GroupPatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        g.name = data["name"]
    if "description" in data:
        g.description = data["description"]
    if data.get("currency") and data["currency"].upper() != g.currency:
        if db.scalar(select(Expense.id).where(Expense.group_id == g.id).limit(1)):
            raise APIError(400, "currency_locked", "Currency can't change after the first expense")
        g.currency = _check_currency(data["currency"])
    if "join_enabled" in data and data["join_enabled"] is not None:
        g.join_enabled = data["join_enabled"]
    effective = None
    if "is_recurring" in data or "recurrence" in data:
        was = g.is_recurring
        if data.get("is_recurring") is not None:
            g.is_recurring = data["is_recurring"]
        if g.is_recurring:
            rec = _check_recurrence(body.recurrence or Recurrence(
                cadence=g.cadence or "monthly", cadence_n=g.cadence_n, anchor_day=g.anchor_day,
                anchor_date=g.anchor_date, timezone=g.timezone, grace_days=g.grace_days,
            ))
            g.cadence, g.cadence_n, g.timezone, g.grace_days = rec.cadence, rec.cadence_n, rec.timezone, rec.grace_days
            g.anchor_day = rec.anchor_day or (1 if rec.cadence in ("monthly", "weekly") else None)
            if rec.anchor_date:
                g.anchor_date = rec.anchor_date
        effective = apply_recurrence_change(db, g, was)
        db.flush()
        rollover_group(db, g)
    audit.log(db, user.id, "group", g.id, "update", {k: str(v) for k, v in data.items()})
    return group_out(db, g, m.role, recurrence_effective_from=effective, viewer_id=user.id)


@router.delete("/groups/{group_id}", status_code=204)
def archive_group(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_owner(m)
    if g.is_default:
        raise APIError(409, "default_group_protected", "The default group can't be deleted")
    g.archived_at = utcnow()
    audit.log(db, user.id, "group", g.id, "archive")
    sync_group_members(db, g.id)  # drops it from every member's order


@router.post("/groups/{group_id}/unarchive", response_model=GroupOut)
def unarchive_group(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_owner(m)
    g.archived_at = None
    audit.log(db, user.id, "group", g.id, "unarchive")
    sync_group_members(db, g.id)  # re-appended at the end of every member's order
    return group_out(db, g, m.role, viewer_id=user.id)


class UserIdIn(BaseModel):
    user_id: str


@router.post("/groups/{group_id}/transfer-ownership", response_model=GroupOut)
def transfer_ownership(group_id: str, body: UserIdIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_owner(m)
    if g.is_default:
        raise APIError(409, "default_group_protected", "The default group can't be transferred")
    if body.user_id == user.id:
        return group_out(db, g, m.role, viewer_id=user.id)  # already the owner
    target = access.membership(db, g.id, body.user_id)
    if target is None or target.user.is_placeholder:
        raise validation("New owner must be a registered member", user_id="not a registered member")
    target.role, m.role, g.owner_id = "owner", "admin", target.user_id
    audit.log(db, user.id, "group", g.id, "transfer_ownership", {"to": body.user_id})
    return group_out(db, g, m.role, viewer_id=user.id)


class MySettingsIn(BaseModel):
    default_expense_type_id: str | None = None


@router.put("/groups/{group_id}/my-settings", response_model=GroupOut)
def put_my_settings(group_id: str, body: MySettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """Caller's own preferences for this group (e.g. the type auto-selected when this group is picked)."""
    from app.models import ExpenseType, UserGroupSetting

    g, m = access.get_group_for_member(db, group_id, user)
    if body.default_expense_type_id:
        t = db.get(ExpenseType, body.default_expense_type_id)
        if t is None or t.user_id != user.id:
            raise not_found("Expense type")
    setting = db.get(UserGroupSetting, (user.id, g.id))
    if setting is None:
        setting = UserGroupSetting(user_id=user.id, group_id=g.id)
        db.add(setting)
    setting.default_expense_type_id = body.default_expense_type_id
    db.flush()
    return group_out(db, g, m.role, viewer_id=user.id)


# ---------------------------------------------------------------- join codes


def _shareable(g: Group) -> None:
    if g.is_default:
        raise APIError(409, "default_group_protected", "The default group can't be shared")


@router.get("/groups/{group_id}/join-code")
def get_join_code(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    _shareable(g)
    if not g.join_code:
        g.join_code = _unique_join_code(db, g.name)
    return {"join_code": g.join_code, "join_enabled": g.join_enabled}


@router.post("/groups/{group_id}/join-code/regenerate")
def regenerate_join_code(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    _shareable(g)
    g.join_code = _unique_join_code(db, g.name)
    audit.log(db, user.id, "group", g.id, "regenerate_join_code")
    return {"join_code": g.join_code, "join_enabled": g.join_enabled}


class JoinIn(BaseModel):
    join_code: str
    claim_placeholder_id: str | None = None


@router.post("/groups/join", response_model=GroupOut)
def join_group(body: JoinIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    from app.api.auth import find_group_by_code

    g = find_group_by_code(db, body.join_code)
    existing = access.membership(db, g.id, user.id)
    if existing and not body.claim_placeholder_id:
        raise APIError(409, "already_member", "You are already a member of this group")
    if body.claim_placeholder_id:
        placeholder = check_claimable(db, g.id, body.claim_placeholder_id)
        merge_into(db, placeholder, user, user.id)
    m = db.get(GroupMember, (g.id, user.id))
    if m is None:
        m = GroupMember(group_id=g.id, user_id=user.id, role="member")
        db.add(m)
    elif m.left_at is not None:
        m.left_at, m.role, m.joined_at = None, "member", utcnow()
    db.flush()
    audit.log(db, user.id, "group", g.id, "join", {"claimed": body.claim_placeholder_id})
    sync_users(db, [user.id])
    return group_out(db, g, m.role, viewer_id=user.id)


# ---------------------------------------------------------------- members


@router.get("/groups/{group_id}/members", response_model=dict[str, list[MemberOut]])
def list_members(group_id: str, include_left: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    access.get_group_for_member(db, group_id, user)
    stmt = select(GroupMember).where(GroupMember.group_id == group_id)
    if not include_left:
        stmt = stmt.where(GroupMember.left_at.is_(None))
    rows = db.scalars(stmt).all()
    order = {"owner": 0, "admin": 1, "member": 2}
    rows = sorted(rows, key=lambda m: (order[m.role], m.user.display_name.lower()))
    return {
        "items": [MemberOut(user=user_out(m.user), role=m.role, joined_at=m.joined_at, left_at=m.left_at) for m in rows]
    }


class AddMemberIn(BaseModel):
    user_id: str | None = None
    display_name: str | None = Field(None, min_length=1, max_length=100)


@router.post("/groups/{group_id}/members", response_model=MemberOut, status_code=201)
def add_member(group_id: str, body: AddMemberIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    if g.is_default:
        raise APIError(409, "default_group_protected", "The default group can't have other members")
    if body.user_id:
        target = db.get(User, body.user_id)
        if target is None or target.merged_into_id or target.disabled_at:
            raise not_found("User")
    elif body.display_name:
        target = User(display_name=body.display_name, is_placeholder=True, created_by_id=user.id)
        db.add(target)
        db.flush()
    else:
        raise validation("Provide user_id or display_name", user_id="required", display_name="required")
    gm = db.get(GroupMember, (g.id, target.id))
    if gm and gm.left_at is None:
        raise APIError(409, "already_member", "Already a member")
    if gm:
        gm.left_at, gm.role, gm.joined_at = None, "member", utcnow()
    else:
        gm = GroupMember(group_id=g.id, user_id=target.id, role="member")
        db.add(gm)
    db.flush()
    audit.log(db, user.id, "group", g.id, "add_member", {"user_id": target.id})
    sync_users(db, [target.id])
    return MemberOut(user=user_out(target), role=gm.role, joined_at=gm.joined_at, left_at=None)


class RoleIn(BaseModel):
    role: str = Field(pattern="^(admin|member)$")


@router.patch("/groups/{group_id}/members/{user_id}", response_model=MemberOut)
def set_role(group_id: str, user_id: str, body: RoleIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    target = access.membership(db, g.id, user_id)
    if target is None:
        raise not_found("Member")
    if target.role == "owner":
        raise APIError(409, "conflict", "Use transfer-ownership to change the owner")
    if target.user.is_placeholder and body.role == "admin":
        raise validation("Placeholders can't be admins", role="placeholder")
    target.role = body.role
    audit.log(db, user.id, "group", g.id, "set_role", {"user_id": user_id, "role": body.role})
    return MemberOut(user=user_out(target.user), role=target.role, joined_at=target.joined_at, left_at=None)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
def remove_member(group_id: str, user_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    target = access.membership(db, g.id, user_id)
    if target is None:
        raise not_found("Member")
    if target.role == "owner":
        raise APIError(409, "conflict", "The owner can't be removed")
    target.left_at = utcnow()
    audit.log(db, user.id, "group", g.id, "remove_member", {"user_id": user_id})
    sync_users(db, [user_id])


@router.post("/groups/{group_id}/leave", status_code=204)
def leave(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    if g.is_default:
        raise APIError(409, "default_group_protected", "You can't leave your default group")
    if m.role == "owner":
        raise APIError(409, "conflict", "Transfer ownership before leaving")
    m.left_at = utcnow()
    audit.log(db, user.id, "group", g.id, "leave")
    sync_users(db, [user.id])


@router.get("/groups/{group_id}/placeholders", response_model=dict[str, list[UserOut]])
def list_placeholders(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    access.get_group_for_member(db, group_id, user)
    return {"items": [user_out(p) for p in group_placeholders(db, group_id)]}


@router.post("/groups/{group_id}/placeholders/{placeholder_id}/claim", response_model=GroupOut)
def claim_placeholder(group_id: str, placeholder_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, _ = access.get_group_for_member(db, group_id, user)
    placeholder = check_claimable(db, g.id, placeholder_id)
    merge_into(db, placeholder, user, user.id)
    m = access.membership(db, g.id, user.id)
    return group_out(db, g, m.role, viewer_id=user.id)


# ---------------------------------------------------------------- periods


def _period_for_member(db: Session, period_id: str, user: User) -> tuple[Period, Group]:
    p = db.get(Period, period_id)
    if p is None:
        raise not_found("Period")
    g, _ = access.get_group_for_member(db, p.group_id, user)
    return p, g


@router.get("/groups/{group_id}/periods")
def list_periods(
    group_id: str, cursor: str | None = None, limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user), db: Session = Depends(get_db, scope="function"),
):
    access.get_group_for_member(db, group_id, user)
    stmt = select(Period).where(Period.group_id == group_id).order_by(Period.starts_on.desc())
    rows, nxt = paginate(db, stmt, cursor, limit)
    return {"items": [PeriodOut.model_validate(p) for p in rows], "next_cursor": nxt}


@router.get("/periods/{period_id}")
def get_period(period_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    p, g = _period_for_member(db, period_id, user)
    users = UserCache(db)
    members = [
        {"user": users.out(pm.user_id), "is_guest": pm.is_guest}
        for pm in db.scalars(select(PeriodMember).where(PeriodMember.period_id == p.id))
    ]
    return {**PeriodOut.model_validate(p).model_dump(), "members": members}


class CutIn(BaseModel):
    ends_on: date | None = None


@router.post("/groups/{group_id}/periods/cut", response_model=PeriodOut)
def cut(group_id: str, body: CutIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    g, m = access.get_group_for_member(db, group_id, user)
    access.require_group_admin(m)
    new = cut_period(db, g, body.ends_on if body else None, user.id)
    return PeriodOut.model_validate(new)


def balances_payload(db: Session, p: Period, g: Group) -> dict:
    b = compute_period_balances(db, p)
    users = UserCache(db)
    member_ids = set(
        db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == g.id, GroupMember.left_at.is_(None)))
    )
    everyone = sorted(member_ids | b.user_ids, key=lambda u: users.get(u).display_name.lower())
    snapshot = progress = None
    if p.status == "frozen":
        bal = db.scalars(select(PeriodBalance).where(PeriodBalance.period_id == p.id)).all()
        tr = db.scalars(select(PeriodTransfer).where(PeriodTransfer.period_id == p.id)).all()
        snapshot = {
            "frozen_at": p.frozen_at,
            "balances": [{"user_id": x.user_id, "paid": x.paid, "owed": x.owed, "net": x.net} for x in bal],
            "transfers": [{"from_user_id": t.from_user_id, "to_user_id": t.to_user_id, "amount": t.amount} for t in tr],
        }
        progress = {
            "total_owed_at_freeze": sum(t.amount for t in tr),
            "outstanding_now": sum(a for _, _, a in b.suggested_transfers()),
        }
    return {
        "period": PeriodOut.model_validate(p).model_dump(),
        "currency": g.currency,
        "members": [
            {
                "user": users.out(uid), "is_guest": uid not in member_ids, "paid": b.paid[uid], "owed": b.owed[uid],
                "settled_in": b.settled_in[uid], "settled_out": b.settled_out[uid], "outstanding_net": b.outstanding_net[uid],
            }
            for uid in everyone
        ],
        "pairwise": [{"from_user_id": f, "to_user_id": t, "amount": a} for f, t, a in b.pairwise],
        "suggested_transfers": [{"from_user_id": f, "to_user_id": t, "amount": a} for f, t, a in b.suggested_transfers()],
        "snapshot": snapshot,
        "progress": progress,
    }


@router.get("/periods/{period_id}/balances")
def period_balances(period_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    p, g = _period_for_member(db, period_id, user)
    return balances_payload(db, p, g)


@router.get("/groups/{group_id}/balances")
def group_balances(group_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    from app.services.periods import open_period

    g, _ = access.get_group_for_member(db, group_id, user)
    p = open_period(db, g.id)
    if p is None:
        raise not_found("Open period")
    return balances_payload(db, p, g)
