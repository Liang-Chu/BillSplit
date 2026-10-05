from sqlalchemy.orm import Session

from app.models import ExpenseType, Group, GroupMember, Routing, User, UserGroupSetting
from app.services.periods import create_initial_period

SEED = [
    # (group/routing/type name, is_default)
    ("Personal", True),
    ("Life Expense", False),
    ("Reimbursement", False),
]


def create_group(
    db: Session, owner: User, *, name: str, currency: str, description: str | None = None,
    is_default: bool = False, recurrence: dict | None = None, join_code: str | None = None,
) -> Group:
    g = Group(
        name=name, description=description, currency=currency, owner_id=owner.id, is_default=is_default,
        join_code=join_code, join_enabled=not is_default,
    )
    if recurrence:
        g.is_recurring = True
        g.cadence = recurrence.get("cadence", "monthly")
        g.cadence_n = recurrence.get("cadence_n")
        g.anchor_day = recurrence.get("anchor_day")
        g.anchor_date = recurrence.get("anchor_date")
        g.timezone = recurrence.get("timezone") or "UTC"
        g.grace_days = recurrence.get("grace_days", 3)
        if g.cadence in ("monthly", "weekly") and g.anchor_day is None:
            g.anchor_day = 1
    db.add(g)
    db.flush()
    db.add(GroupMember(group_id=g.id, user_id=owner.id, role="owner"))
    db.flush()
    create_initial_period(db, g)
    return g


def seed_user(db: Session, user: User, currency: str | None = None) -> Group:
    """Creates the 3 only-me groups with matching routings and types. Returns the default group."""
    from app.config import get_settings
    from app.ids import join_code

    currency = currency or get_settings().default_currency
    default = None
    for name, is_default in SEED:
        g = create_group(
            db, user, name=name, currency=currency, is_default=is_default,
            join_code=None if is_default else join_code(name),
        )
        r = Routing(user_id=user.id, name=name, group_id=g.id)
        db.add(r)
        db.flush()
        t = ExpenseType(user_id=user.id, name=name, routing_id=r.id)
        db.add(t)
        db.flush()
        db.add(UserGroupSetting(user_id=user.id, group_id=g.id, default_expense_type_id=t.id))
        if is_default:
            default = g
    db.flush()
    return default
