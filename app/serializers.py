"""Build API response objects from ORM rows."""

import base64
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import validation
from app.models import Attachment, Expense, ExpenseShare, ExpenseType, Group, GroupMember, Settlement, User
from app.schemas.common import (
    AttachmentOut,
    ExpenseOut,
    ExpensePermissions,
    ExpenseTypeRef,
    GroupOut,
    ItemOut,
    MeOut,
    PeriodBrief,
    Recurrence,
    SettlementOut,
    ShareOut,
    ShareRef,
    UserOut,
)
from app.services import access
from app.services.periods import open_period

API = "/api/v1"


class UserCache:
    def __init__(self, db: Session):
        self.db = db
        self._c: dict[str, User] = {}

    def get(self, uid: str | None) -> User | None:
        if uid is None:
            return None
        if uid not in self._c:
            self._c[uid] = self.db.get(User, uid)
        return self._c[uid]

    def out(self, uid: str | None) -> UserOut | None:
        u = self.get(uid)
        return user_out(u) if u else None


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, display_name=u.display_name, username=u.username, is_placeholder=u.is_placeholder)


def me_out(db: Session, u: User) -> MeOut:
    from app.services.people import default_group

    from app.services.group_order import preferred_group_id

    dg = default_group(db, u.id)
    return MeOut(
        preferred_group_id=preferred_group_id(db, u.id),
        id=u.id, username=u.username, display_name=u.display_name, email=u.email, is_super_admin=u.is_super_admin,
        must_change_password=u.must_change_password, default_group_id=dg.id if dg else None, created_at=u.created_at,
    )


def attachment_out(a: Attachment, users: UserCache) -> AttachmentOut:
    return AttachmentOut(
        id=a.id, original_name=a.original_name, mime=a.mime, size=a.size,
        file_url=f"{API}/attachments/{a.id}/file", thumb_url=f"{API}/attachments/{a.id}/thumb" if a.thumb_key else None,
        uploaded_by=users.out(a.uploaded_by_id), created_at=a.created_at,
    )


def attachments_for(db: Session, owner_type: str, owner_id: str, users: UserCache) -> list[AttachmentOut]:
    rows = db.scalars(
        select(Attachment)
        .where(Attachment.owner_type == owner_type, Attachment.owner_id == owner_id, Attachment.deleted_at.is_(None))
        .order_by(Attachment.created_at)
    ).all()
    return [attachment_out(a, users) for a in rows]


def group_out(db: Session, g: Group, role: str, recurrence_effective_from=None, viewer_id: str | None = None) -> GroupOut:
    from app.models import UserGroupSetting

    setting = db.get(UserGroupSetting, (viewer_id, g.id)) if viewer_id else None
    count = db.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == g.id, GroupMember.left_at.is_(None))
    )
    p = open_period(db, g.id)
    rec = None
    if g.is_recurring:
        rec = Recurrence(
            cadence=g.cadence, cadence_n=g.cadence_n, anchor_day=g.anchor_day, anchor_date=g.anchor_date,
            timezone=g.timezone, grace_days=g.grace_days,
        )
    return GroupOut(
        id=g.id, name=g.name, description=g.description, currency=g.currency, is_default=g.is_default,
        is_recurring=g.is_recurring, recurrence=rec, my_role=role, member_count=count or 0,
        current_period=PeriodBrief.model_validate(p) if p else None, join_enabled=g.join_enabled,
        default_expense_type_id=setting.default_expense_type_id if setting else None,
        archived_at=g.archived_at, created_at=g.created_at, recurrence_effective_from=recurrence_effective_from,
    )


def expense_out(db: Session, e: Expense, viewer: User, users: UserCache | None = None, include_ocr: bool = True) -> ExpenseOut:
    users = users or UserCache(db)
    group = db.get(Group, e.group_id)
    etype = db.get(ExpenseType, e.expense_type_id) if e.expense_type_id else None
    period_frozen = e.period_id and _period_status(db, e.period_id) == "frozen"
    can_edit = access.can_edit_expense(db, e, viewer) and not period_frozen
    shares = [
        ShareOut(
            user=users.out(s.user_id), is_guest=s.is_guest, split_value=s.split_value, amount=s.amount,
            checked_at=s.checked_at, checked_by=users.out(s.checked_by_id), settlement_id=s.settlement_id,
        )
        for s in e.shares
    ]
    owed = [s for s in e.shares if s.user_id != e.payer_id and s.amount > 0]
    return ExpenseOut(
        id=e.id, group_id=e.group_id, period_id=e.period_id, currency=group.currency, title=e.title,
        merchant=e.merchant, amount=e.amount, expense_date=e.expense_date,
        expense_type=ExpenseTypeRef(id=etype.id, name=etype.name, owner_id=etype.user_id) if etype else None,
        payer=users.out(e.payer_id), split_method=e.split_method, shares=shares,
        items=[
            ItemOut(id=i.id, name=i.name, amount=i.amount, participant_ids=sorted(p.user_id for p in i.participants))
            for i in e.items
        ],
        attachments=attachments_for(db, "expense", e.id, users), notes=e.notes,
        ocr_result=e.ocr_result if include_ocr else None,
        fully_checked=all(s.checked_at is not None for s in owed),
        permissions=ExpensePermissions(can_edit=can_edit, can_delete=can_edit),
        created_by=users.out(e.created_by_id), created_at=e.created_at, updated_at=e.updated_at,
    )


def _period_status(db: Session, period_id: str) -> str | None:
    from app.models import Period

    p = db.get(Period, period_id)
    return p.status if p else None


def settlement_out(db: Session, s: Settlement, users: UserCache | None = None) -> SettlementOut:
    users = users or UserCache(db)
    group = db.get(Group, s.group_id)
    linked = db.scalars(select(ExpenseShare).where(ExpenseShare.settlement_id == s.id)).all()
    return SettlementOut(
        id=s.id, group_id=s.group_id, period_id=s.period_id, currency=group.currency,
        from_user=users.out(s.from_user_id), to_user=users.out(s.to_user_id), amount=s.amount,
        settled_on=s.settled_on, note=s.note,
        linked_shares=[ShareRef(expense_id=x.expense_id, user_id=x.user_id) for x in linked],
        attachments=attachments_for(db, "settlement", s.id, users), created_by=users.out(s.created_by_id),
        created_at=s.created_at,
    )


# ---------------------------------------------------------------- pagination


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["o"])
    except Exception:
        raise validation("Invalid cursor", cursor="invalid")


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def paginate(db: Session, stmt, cursor: str | None, limit: int):
    offset = decode_cursor(cursor)
    rows = db.scalars(stmt.offset(offset).limit(limit + 1)).all()
    nxt = encode_cursor(offset + limit) if len(rows) > limit else None
    return rows[:limit], nxt
