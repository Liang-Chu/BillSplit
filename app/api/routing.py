"""Per-user routings, expense types and auto-rules."""

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import APIError, not_found, validation
from app.ids import utcnow
from app.models import AutoRule, Expense, ExpenseType, Routing, User, UserGroupSetting
from app.security import current_user
from app.services import access
from app.services.resolve import first_matching_rule

router = APIRouter(tags=["routing"])


def _own(db: Session, model, obj_id: str, user: User, what: str):
    obj = db.get(model, obj_id)
    if obj is None or obj.user_id != user.id:
        raise not_found(what)
    return obj


def _check_group(db: Session, group_id: str | None, user: User) -> None:
    if group_id and access.membership(db, group_id, user.id) is None:
        raise validation("You are not a member of that group", group_id="not a member")


def _unique_name(db: Session, model, user: User, name: str, exclude_id: str | None = None) -> None:
    stmt = select(model.id).where(model.user_id == user.id, func.lower(model.name) == name.lower())
    if exclude_id:
        stmt = stmt.where(model.id != exclude_id)
    if db.scalar(stmt):
        raise APIError(409, "conflict", f"You already have one named '{name}'")


# ---------------------------------------------------------------- routings


def _routing_out(r: Routing) -> dict:
    return {"id": r.id, "name": r.name, "group_id": r.group_id, "archived_at": r.archived_at}


class RoutingIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_id: str | None = None


class RoutingPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    group_id: str | None = None
    archived: bool | None = None


@router.get("/routings")
def list_routings(include_archived: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    stmt = select(Routing).where(Routing.user_id == user.id).order_by(Routing.name)
    if not include_archived:
        stmt = stmt.where(Routing.archived_at.is_(None))
    return {"items": [_routing_out(r) for r in db.scalars(stmt)]}


@router.post("/routings", status_code=201)
def create_routing(body: RoutingIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    _check_group(db, body.group_id, user)
    _unique_name(db, Routing, user, body.name)
    r = Routing(user_id=user.id, name=body.name, group_id=body.group_id)
    db.add(r)
    db.flush()
    return _routing_out(r)


@router.patch("/routings/{routing_id}")
def patch_routing(routing_id: str, body: RoutingPatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    r = _own(db, Routing, routing_id, user, "Routing")
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        _unique_name(db, Routing, user, data["name"], r.id)
        r.name = data["name"]
    if "group_id" in data:
        _check_group(db, data["group_id"], user)
        r.group_id = data["group_id"]
    if data.get("archived") is not None:
        r.archived_at = utcnow() if data["archived"] else None
    return _routing_out(r)


@router.delete("/routings/{routing_id}", status_code=204)
def delete_routing(routing_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    r = _own(db, Routing, routing_id, user, "Routing")
    if db.scalar(select(ExpenseType.id).where(ExpenseType.routing_id == r.id).limit(1)):
        raise APIError(409, "in_use", "Expense types still use this routing; move or delete them first")
    db.delete(r)


# ---------------------------------------------------------------- expense types


def _type_out(t: ExpenseType) -> dict:
    return {"id": t.id, "name": t.name, "routing_id": t.routing_id, "icon": t.icon, "color": t.color,
            "archived_at": t.archived_at}


class TypeIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    routing_id: str
    icon: str | None = Field(None, max_length=50)
    color: str | None = Field(None, max_length=20)


class TypePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    routing_id: str | None = None
    icon: str | None = None
    color: str | None = None
    archived: bool | None = None


@router.get("/expense-types")
def list_types(include_archived: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    stmt = select(ExpenseType).where(ExpenseType.user_id == user.id).order_by(ExpenseType.name)
    if not include_archived:
        stmt = stmt.where(ExpenseType.archived_at.is_(None))
    return {"items": [_type_out(t) for t in db.scalars(stmt)]}


@router.post("/expense-types", status_code=201)
def create_type(body: TypeIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    _own(db, Routing, body.routing_id, user, "Routing")
    _unique_name(db, ExpenseType, user, body.name)
    t = ExpenseType(user_id=user.id, name=body.name, routing_id=body.routing_id, icon=body.icon, color=body.color)
    db.add(t)
    db.flush()
    return _type_out(t)


@router.patch("/expense-types/{type_id}")
def patch_type(type_id: str, body: TypePatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    t = _own(db, ExpenseType, type_id, user, "Expense type")
    data = body.model_dump(exclude_unset=True)
    if data.get("name"):
        _unique_name(db, ExpenseType, user, data["name"], t.id)
        t.name = data["name"]
    if data.get("routing_id"):
        _own(db, Routing, data["routing_id"], user, "Routing")
        t.routing_id = data["routing_id"]
    for f in ("icon", "color"):
        if f in data:
            setattr(t, f, data[f])
    if data.get("archived") is not None:
        t.archived_at = utcnow() if data["archived"] else None
    return _type_out(t)


@router.delete("/expense-types/{type_id}", status_code=204)
def delete_type(type_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    t = _own(db, ExpenseType, type_id, user, "Expense type")
    if db.scalar(select(Expense.id).where(Expense.expense_type_id == t.id).limit(1)):
        t.archived_at = utcnow()  # used by expenses: archive instead
        return
    db.query(UserGroupSetting).filter_by(default_expense_type_id=t.id).update({"default_expense_type_id": None})
    db.query(AutoRule).filter_by(set_type_id=t.id).update({"set_type_id": None})
    db.delete(t)


# ---------------------------------------------------------------- auto rules


def _rule_out(r: AutoRule) -> dict:
    return {"id": r.id, "priority": r.priority, "enabled": r.enabled, "field": r.field, "mode": r.mode,
            "pattern": r.pattern, "set_type_id": r.set_type_id, "set_group_id": r.set_group_id}


class RuleIn(BaseModel):
    field: str = Field("merchant", pattern="^(merchant|title)$")
    mode: str = Field("contains", pattern="^(contains|equals|regex)$")
    pattern: str = Field(min_length=1, max_length=255)
    set_type_id: str | None = None
    set_group_id: str | None = None
    enabled: bool = True


class RulePatch(BaseModel):
    field: str | None = Field(None, pattern="^(merchant|title)$")
    mode: str | None = Field(None, pattern="^(contains|equals|regex)$")
    pattern: str | None = Field(None, min_length=1, max_length=255)
    set_type_id: str | None = None
    set_group_id: str | None = None
    enabled: bool | None = None


def _validate_rule(db: Session, user: User, r: AutoRule) -> None:
    if not r.set_type_id and not r.set_group_id:
        raise validation("A rule must set a type, a group, or both", set_type_id="required", set_group_id="required")
    if r.set_type_id:
        _own(db, ExpenseType, r.set_type_id, user, "Expense type")
    _check_group(db, r.set_group_id, user)
    if r.mode == "regex":
        try:
            re.compile(r.pattern)
        except re.error as e:
            raise validation("Invalid regular expression", pattern=str(e))


@router.get("/auto-rules")
def list_rules(user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    rows = db.scalars(select(AutoRule).where(AutoRule.user_id == user.id).order_by(AutoRule.priority, AutoRule.id))
    return {"items": [_rule_out(r) for r in rows]}


@router.post("/auto-rules", status_code=201)
def create_rule(body: RuleIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    top = db.scalar(select(func.max(AutoRule.priority)).where(AutoRule.user_id == user.id)) or 0
    r = AutoRule(user_id=user.id, priority=top + 10, **body.model_dump())
    _validate_rule(db, user, r)
    db.add(r)
    db.flush()
    return _rule_out(r)


class OrderIn(BaseModel):
    ids: list[str]


@router.put("/auto-rules/order")
def reorder_rules(body: OrderIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    rules = {r.id: r for r in db.scalars(select(AutoRule).where(AutoRule.user_id == user.id))}
    if set(body.ids) != set(rules):
        raise validation("ids must list every one of your rules exactly once", ids="mismatch")
    for i, rid in enumerate(body.ids):
        rules[rid].priority = (i + 1) * 10
    return list_rules(user, db)


class RuleTestIn(BaseModel):
    merchant: str | None = None
    title: str | None = None


@router.post("/auto-rules/test")
def test_rules(body: RuleTestIn, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    r = first_matching_rule(db, user, body.merchant, body.title)
    return {"matched_rule": _rule_out(r) if r else None}


@router.patch("/auto-rules/{rule_id}")
def patch_rule(rule_id: str, body: RulePatch, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    r = _own(db, AutoRule, rule_id, user, "Rule")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(r, k, v)
    _validate_rule(db, user, r)
    return _rule_out(r)


@router.delete("/auto-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, user: User = Depends(current_user), db: Session = Depends(get_db, scope="function")):
    db.delete(_own(db, AutoRule, rule_id, user, "Rule"))
