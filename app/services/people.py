"""Placeholder conversion and merging."""

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.errors import APIError
from app.ids import utcnow
from app.models import (
    Attachment,
    Expense,
    ExpenseItemParticipant,
    ExpenseShare,
    Group,
    GroupMember,
    PeriodBalance,
    PeriodMember,
    PeriodTransfer,
    Settlement,
    User,
)
from app.services import audit


def default_group(db: Session, user_id: str) -> Group | None:
    return db.scalars(select(Group).where(Group.owner_id == user_id, Group.is_default.is_(True))).first()


def group_placeholders(db: Session, group_id: str) -> list[User]:
    """Unclaimed placeholders that are members of the group or guests on its bills."""
    member_ids = select(GroupMember.user_id).where(GroupMember.group_id == group_id, GroupMember.left_at.is_(None))
    guest_ids = (
        select(ExpenseShare.user_id)
        .join(Expense, Expense.id == ExpenseShare.expense_id)
        .where(Expense.group_id == group_id, Expense.deleted_at.is_(None))
    )
    return list(
        db.scalars(
            select(User)
            .where(User.is_placeholder.is_(True), User.merged_into_id.is_(None))
            .where(User.id.in_(member_ids) | User.id.in_(guest_ids))
            .order_by(User.display_name)
        )
    )


def check_claimable(db: Session, group_id: str, placeholder_id: str) -> User:
    for p in group_placeholders(db, group_id):
        if p.id == placeholder_id:
            return p
    raise APIError(404, "not_found", "Placeholder not found in this group")


def _sum_values(a: str | None, b: str | None) -> str | None:
    if a is None or b is None:
        return a or b
    try:
        from decimal import Decimal

        return str(Decimal(a) + Decimal(b))
    except Exception:
        return a


def merge_into(db: Session, placeholder: User, target: User, actor_id: str) -> None:
    """Re-point every reference from `placeholder` to `target` in the current transaction."""
    if not placeholder.is_placeholder or placeholder.merged_into_id:
        raise APIError(409, "conflict", "Not an unclaimed placeholder")
    if placeholder.id == target.id:
        raise APIError(409, "conflict", "Cannot merge into self")
    pid, tid = placeholder.id, target.id

    # Memberships
    for m in db.scalars(select(GroupMember).where(GroupMember.user_id == pid)).all():
        existing = db.get(GroupMember, (m.group_id, tid))
        if existing:
            if existing.left_at is not None and m.left_at is None:
                existing.left_at = None
            db.delete(m)
        else:
            db.execute(
                update(GroupMember)
                .where(GroupMember.group_id == m.group_id, GroupMember.user_id == pid)
                .values(user_id=tid)
            )
    db.flush()

    # Shares (combine if both on the same bill)
    for s in db.scalars(select(ExpenseShare).where(ExpenseShare.user_id == pid)).all():
        other = db.get(ExpenseShare, (s.expense_id, tid))
        if other:
            other.amount += s.amount
            other.split_value = _sum_values(other.split_value, s.split_value)
            if not (other.checked_at and s.checked_at):
                other.checked_at = None
                other.checked_by_id = None
                other.settlement_id = None
            db.delete(s)
        else:
            db.execute(
                update(ExpenseShare)
                .where(ExpenseShare.expense_id == s.expense_id, ExpenseShare.user_id == pid)
                .values(user_id=tid)
            )
    db.flush()

    # Item participants (ignore duplicates)
    for ip in db.scalars(select(ExpenseItemParticipant).where(ExpenseItemParticipant.user_id == pid)).all():
        if db.get(ExpenseItemParticipant, (ip.item_id, tid)):
            db.delete(ip)
        else:
            db.execute(
                update(ExpenseItemParticipant)
                .where(ExpenseItemParticipant.item_id == ip.item_id, ExpenseItemParticipant.user_id == pid)
                .values(user_id=tid)
            )
    db.flush()

    db.execute(update(Expense).where(Expense.payer_id == pid).values(payer_id=tid))
    db.execute(update(Expense).where(Expense.created_by_id == pid).values(created_by_id=tid))
    db.execute(update(ExpenseShare).where(ExpenseShare.checked_by_id == pid).values(checked_by_id=tid))
    db.execute(update(Settlement).where(Settlement.from_user_id == pid).values(from_user_id=tid))
    db.execute(update(Settlement).where(Settlement.to_user_id == pid).values(to_user_id=tid))
    db.execute(
        update(Settlement)
        .where(Settlement.from_user_id == Settlement.to_user_id, Settlement.deleted_at.is_(None))
        .values(deleted_at=utcnow())
    )
    db.execute(update(Attachment).where(Attachment.uploaded_by_id == pid).values(uploaded_by_id=tid))

    # Frozen-period snapshots
    for pm in db.scalars(select(PeriodMember).where(PeriodMember.user_id == pid)).all():
        if db.get(PeriodMember, (pm.period_id, tid)):
            db.delete(pm)
        else:
            db.execute(
                update(PeriodMember)
                .where(PeriodMember.period_id == pm.period_id, PeriodMember.user_id == pid)
                .values(user_id=tid)
            )
    for pb in db.scalars(select(PeriodBalance).where(PeriodBalance.user_id == pid)).all():
        other = db.get(PeriodBalance, (pb.period_id, tid))
        if other:
            other.paid += pb.paid
            other.owed += pb.owed
            other.net += pb.net
            db.delete(pb)
        else:
            db.execute(
                update(PeriodBalance)
                .where(PeriodBalance.period_id == pb.period_id, PeriodBalance.user_id == pid)
                .values(user_id=tid)
            )
    db.execute(update(PeriodTransfer).where(PeriodTransfer.from_user_id == pid).values(from_user_id=tid))
    db.execute(update(PeriodTransfer).where(PeriodTransfer.to_user_id == pid).values(to_user_id=tid))
    db.execute(delete(PeriodTransfer).where(PeriodTransfer.from_user_id == PeriodTransfer.to_user_id))
    db.execute(update(User).where(User.created_by_id == pid).values(created_by_id=tid))

    placeholder.merged_into_id = tid
    db.flush()
    from app.services.group_order import sync_users

    sync_users(db, [tid])
    audit.log(db, actor_id, "user", pid, "merge", {"into": tid})
