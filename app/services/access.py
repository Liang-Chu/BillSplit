"""Authorization helpers. 404 is returned for things the caller can't see."""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.errors import forbidden, not_found
from app.models import Expense, ExpenseShare, Group, GroupMember, User


def membership(db: Session, group_id: str, user_id: str) -> GroupMember | None:
    m = db.get(GroupMember, (group_id, user_id))
    return m if m and m.left_at is None else None


def get_group_for_member(db: Session, group_id: str, user: User) -> tuple[Group, GroupMember]:
    group = db.get(Group, group_id)
    m = membership(db, group_id, user.id) if group else None
    if group is None or m is None:
        raise not_found("Group")
    return group, m


def require_group_admin(m: GroupMember) -> None:
    if m.role not in ("owner", "admin"):
        raise forbidden("Group owner or admin only")


def require_group_owner(m: GroupMember) -> None:
    if m.role != "owner":
        raise forbidden("Group owner only")


def active_group_ids(db: Session, user_id: str) -> list[str]:
    return list(
        db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id, GroupMember.left_at.is_(None)))
    )


def is_on_expense(db: Session, expense: Expense, user_id: str) -> bool:
    if expense.payer_id == user_id:
        return True
    return bool(
        db.scalar(
            select(exists().where(ExpenseShare.expense_id == expense.id, ExpenseShare.user_id == user_id))
        )
    )


def get_visible_expense(db: Session, expense_id: str, user: User) -> Expense:
    e = db.get(Expense, expense_id)
    if e is None or e.deleted_at is not None:
        raise not_found("Expense")
    if membership(db, e.group_id, user.id) is None and not is_on_expense(db, e, user.id):
        raise not_found("Expense")
    return e


def can_edit_expense(db: Session, e: Expense, user: User) -> bool:
    if user.id in (e.created_by_id, e.payer_id):
        return True
    m = membership(db, e.group_id, user.id)
    return m is not None and m.role == "owner"


def can_check_share(e: Expense, share_user_id: str, user: User) -> bool:
    return user.id in (e.payer_id, share_user_id)
