import secrets

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.currencies import CURRENCIES
from app.db import get_db
from app.errors import APIError, not_found
from app.ids import utcnow
from app.models import Group, GroupMember, Period, User
from app.schemas.common import MeOut, UserOut
from app.security import current_user, current_user_any, hash_password, super_admin
from app.serializers import me_out, user_out
from app.services import access, audit
from app.services.balances import compute_period_balances
from app.services.seed import seed_user

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeOut)
def get_me(user: User = Depends(current_user_any), db: Session = Depends(get_db, scope="function")):
    return me_out(db, user)


class MePatch(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    email: str | None = None


@router.patch("/me", response_model=MeOut)
def patch_me(body: MePatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    data = body.model_dump(exclude_unset=True)
    if data.get("display_name"):
        user.display_name = data["display_name"]
    if "email" in data:
        user.email = data["email"]
    db.add(user)
    return me_out(db, user)


@router.get("/me/overview")
def overview(user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """What I owe / am owed, per currency and per group period with outstanding amounts."""
    from app.models import Expense, ExpenseShare

    member_groups = set(access.active_group_ids(db, user.id))
    guest_groups = set(
        db.scalars(
            select(Expense.group_id)
            .join(ExpenseShare, ExpenseShare.expense_id == Expense.id)
            .where(ExpenseShare.user_id == user.id, Expense.deleted_at.is_(None))
        )
    )
    totals: dict[str, dict[str, int]] = {}
    groups_out = []
    for gid in sorted(member_groups | guest_groups):
        g = db.get(Group, gid)
        if g is None or g.archived_at is not None:
            continue
        periods_out = []
        for p in db.scalars(select(Period).where(Period.group_id == gid).order_by(Period.starts_on.desc())).all():
            b = compute_period_balances(db, p)
            i_owe = sum(a for f, _, a in b.pairwise if f == user.id)
            owed_to_me = sum(a for _, t, a in b.pairwise if t == user.id)
            if i_owe == 0 and owed_to_me == 0 and p.status != "open":
                continue
            label = p.starts_on.strftime("%Y-%m") if g.is_recurring and g.cadence == "monthly" else p.starts_on.isoformat()
            periods_out.append(
                {"period_id": p.id, "label": label, "status": p.status, "starts_on": p.starts_on, "ends_on": p.ends_on,
                 "i_owe": i_owe, "owed_to_me": owed_to_me, "net": owed_to_me - i_owe}
            )
            t = totals.setdefault(g.currency, {"i_owe": 0, "owed_to_me": 0})
            t["i_owe"] += i_owe
            t["owed_to_me"] += owed_to_me
        groups_out.append(
            {"group_id": g.id, "group_name": g.name, "currency": g.currency, "is_member": gid in member_groups,
             "periods": periods_out}
        )
    return {
        "totals": [
            {"currency": c, "i_owe": t["i_owe"], "owed_to_me": t["owed_to_me"], "net": t["owed_to_me"] - t["i_owe"]}
            for c, t in sorted(totals.items())
        ],
        "groups": groups_out,
    }


@router.get("/people", response_model=dict[str, list[UserOut]])
def people(q: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    """People I can add to a bill: members of my groups + placeholders I created."""
    my_groups = access.active_group_ids(db, user.id)
    co_members = select(GroupMember.user_id).where(GroupMember.group_id.in_(my_groups), GroupMember.left_at.is_(None))
    stmt = select(User).where(
        User.merged_into_id.is_(None), User.disabled_at.is_(None),
        or_(User.id.in_(co_members), User.created_by_id == user.id),
    )
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.display_name).like(like), func.lower(User.username).like(like)))
    return {"items": [user_out(u) for u in db.scalars(stmt.order_by(User.display_name).limit(50))]}


class PlaceholderIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    group_id: str | None = None


@router.post("/placeholders", response_model=UserOut, status_code=201)
def create_placeholder(body: PlaceholderIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    if body.group_id:
        g, _ = access.get_group_for_member(db, body.group_id, user)
        if g.is_default:
            raise APIError(409, "default_group_protected", "The default group can't have other members")
    p = User(display_name=body.display_name, is_placeholder=True, created_by_id=user.id)
    db.add(p)
    db.flush()
    if body.group_id:
        db.add(GroupMember(group_id=body.group_id, user_id=p.id, role="member"))
    audit.log(db, user.id, "user", p.id, "create_placeholder", {"group_id": body.group_id})
    return user_out(p)


@router.get("/meta/currencies")
def currencies(_: User = Depends(current_user_any)):
    return {"items": [{"code": c, "minor_units": m, "name": n} for c, (m, n) in sorted(CURRENCIES.items())]}


# ---------------------------------------------------------------- super admin

admin = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserOut(BaseModel):
    id: str
    username: str | None
    display_name: str
    is_placeholder: bool
    is_super_admin: bool
    must_change_password: bool
    disabled: bool
    merged_into_id: str | None


def _admin_out(u: User) -> AdminUserOut:
    return AdminUserOut(
        id=u.id, username=u.username, display_name=u.display_name, is_placeholder=u.is_placeholder,
        is_super_admin=u.is_super_admin, must_change_password=u.must_change_password,
        disabled=u.disabled_at is not None, merged_into_id=u.merged_into_id,
    )


@admin.get("/users", response_model=dict[str, list[AdminUserOut]])
def admin_users(_: User = Depends(super_admin), db: Session = Depends(get_db, scope="function")):
    return {"items": [_admin_out(u) for u in db.scalars(select(User).order_by(User.created_at))]}


class AdminCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=100)


@admin.post("/users", response_model=AdminUserOut, status_code=201)
def admin_create(body: AdminCreateIn, actor: User = Depends(super_admin), db: Session = Depends(get_db, scope="function")):
    if db.scalar(select(User.id).where(func.lower(User.username) == body.username.lower())):
        raise APIError(409, "username_taken", "Username is already taken")
    u = User(
        username=body.username, display_name=body.display_name, password_hash=hash_password(body.password),
        must_change_password=True, created_by_id=actor.id,
    )
    db.add(u)
    db.flush()
    seed_user(db, u)
    audit.log(db, actor.id, "user", u.id, "admin_create")
    return _admin_out(u)


def _registered(db: Session, user_id: str) -> User:
    u = db.get(User, user_id)
    if u is None or u.is_placeholder:
        raise not_found("User")
    return u


@admin.post("/users/{user_id}/reset-password")
def admin_reset(user_id: str, actor: User = Depends(super_admin), db: Session = Depends(get_db, scope="function")):
    u = _registered(db, user_id)
    temp = secrets.token_urlsafe(9)
    u.password_hash = hash_password(temp)
    u.must_change_password = True
    audit.log(db, actor.id, "user", u.id, "admin_reset_password")
    return {"temporary_password": temp}


@admin.post("/users/{user_id}/disable", response_model=AdminUserOut)
def admin_disable(user_id: str, actor: User = Depends(super_admin), db: Session = Depends(get_db, scope="function")):
    u = _registered(db, user_id)
    if u.id == actor.id:
        raise APIError(409, "conflict", "You can't disable yourself")
    u.disabled_at = utcnow()
    audit.log(db, actor.id, "user", u.id, "disable")
    return _admin_out(u)


@admin.post("/users/{user_id}/enable", response_model=AdminUserOut)
def admin_enable(user_id: str, actor: User = Depends(super_admin), db: Session = Depends(get_db, scope="function")):
    u = _registered(db, user_id)
    u.disabled_at = None
    audit.log(db, actor.id, "user", u.id, "enable")
    return _admin_out(u)
