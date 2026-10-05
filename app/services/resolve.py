"""Suggest an expense type and group: manual group → type → routing → auto-rule → preferred group.

The final fallback is the caller's preferred group (first in their own group order), not the protected
default group."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AutoRule, ExpenseType, Group, Routing, User, UserGroupSetting
from app.services.access import membership
from app.services.group_order import preferred_group_id


def rule_matches(rule: AutoRule, merchant: str | None, title: str | None) -> bool:
    value = merchant if rule.field == "merchant" else title
    if not value:
        return False
    v, p = value.casefold().strip(), rule.pattern.casefold().strip()
    if rule.mode == "contains":
        return p in v
    if rule.mode == "equals":
        return p == v
    try:
        return re.search(rule.pattern, value, re.IGNORECASE) is not None
    except re.error:
        return False


def first_matching_rule(db: Session, user: User, merchant: str | None, title: str | None) -> AutoRule | None:
    rules = db.scalars(
        select(AutoRule).where(AutoRule.user_id == user.id, AutoRule.enabled.is_(True)).order_by(AutoRule.priority, AutoRule.id)
    ).all()
    return next((r for r in rules if rule_matches(r, merchant, title)), None)


def _usable_group(db: Session, group_id: str | None, user: User) -> str | None:
    """Only groups where the user is an active member and that are not archived."""
    if group_id and membership(db, group_id, user.id):
        g = db.get(Group, group_id)
        if g is not None and g.archived_at is None:
            return group_id
    return None


def resolve(
    db: Session, user: User, merchant: str | None, title: str | None, expense_type_id: str | None,
    group_id: str | None = None,
) -> dict:
    """Suggestion only — the client may override both fields.

    - group picked, no type → the group's default type (caller's setting)
    - type picked, no group → type → routing → group
    - neither → first matching auto-rule, then the above, then my preferred group (+ my default type for it)
    """
    group_id = _usable_group(db, group_id, user)
    manual_group = group_id is not None
    rule = None
    if not expense_type_id and not manual_group:
        rule = first_matching_rule(db, user, merchant, title)
    type_id = expense_type_id or (rule.set_type_id if rule else None)
    source = "rule" if rule else "default"
    if rule and rule.set_group_id:
        group_id = _usable_group(db, rule.set_group_id, user)

    if manual_group and not type_id:
        setting = db.get(UserGroupSetting, (user.id, group_id))
        if setting and setting.default_expense_type_id:
            type_id = setting.default_expense_type_id
            source = "group_default"

    etype = db.get(ExpenseType, type_id) if type_id else None
    if etype is None or etype.user_id != user.id or etype.archived_at is not None:
        etype, type_id = None, None
    routing_id = None
    if etype:
        routing = db.get(Routing, etype.routing_id)
        routing_id = routing.id if routing else None
        if group_id is None and routing is not None:
            group_id = _usable_group(db, routing.group_id, user)
            if group_id and not rule:
                source = "type"
    if manual_group and source == "default":
        source = "manual"
    if group_id is None:
        group_id = preferred_group_id(db, user.id)
    if not rule and etype is None and not manual_group:
        # Nothing picked and no rule: the preferred group plus my default type for it.
        # The type is not routed again, so it can't move the suggestion away from the preferred group.
        setting = db.get(UserGroupSetting, (user.id, group_id)) if group_id else None
        default_type = db.get(ExpenseType, setting.default_expense_type_id) if setting and setting.default_expense_type_id else None
        if default_type and default_type.user_id == user.id and default_type.archived_at is None:
            type_id = default_type.id
    return {
        "expense_type_id": type_id,
        "group_id": group_id,
        "routing_id": routing_id,
        "source": source,
        "matched_rule_id": rule.id if rule else None,
    }
