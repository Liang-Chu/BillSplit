"""Periods: every group has them; recurring groups roll open → grace → frozen."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import APIError, validation
from app.ids import utcnow
from app.models import Group, GroupMember, Period, PeriodBalance, PeriodMember, PeriodTransfer
from app.services import audit
from app.services.balances import compute_period_balances


def local_today(group: Group, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(ZoneInfo(group.timezone)).date()


def _add_month(d: date, day: int) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, day)


def next_boundary(group: Group, after: date) -> date:
    """First period start strictly after `after`, according to the group's current settings."""
    if group.cadence == "monthly":
        day = group.anchor_day or 1
        cand = date(after.year, after.month, day)
        return cand if cand > after else _add_month(cand, day)
    if group.cadence == "weekly":
        target = (group.anchor_day or 1) - 1  # 0 = Monday
        delta = (target - after.weekday()) % 7 or 7
        return after + timedelta(days=delta)
    if group.cadence == "every_n_days":
        n = group.cadence_n or 1
        anchor = group.anchor_date or after
        if after < anchor:
            return anchor
        k = (after - anchor).days // n + 1
        return anchor + timedelta(days=k * n)
    raise ValueError("group is not recurring")


def current_cycle_start(group: Group, today: date) -> date:
    """Most recent period boundary on or before `today`."""
    if group.cadence == "every_n_days" and group.anchor_date is None:
        return today
    # Start from a date at least one full cycle back, so a boundary always lies in (probe, today].
    span = {"monthly": 40, "weekly": 8}.get(group.cadence, (group.cadence_n or 1) + 1)
    start = today - timedelta(days=span)
    while (nxt := next_boundary(group, start)) <= today:
        start = nxt
    return start


def period_end(group: Group, start: date) -> date | None:
    if not group.is_recurring:
        return None
    return next_boundary(group, start) - timedelta(days=1)


def grace_until(group: Group, ends_on: date) -> datetime:
    local_midnight = datetime.combine(ends_on + timedelta(days=1 + group.grace_days), time.min, ZoneInfo(group.timezone))
    return local_midnight.astimezone(UTC)


def create_initial_period(db: Session, group: Group) -> Period:
    today = local_today(group)
    if group.is_recurring:
        if group.cadence == "every_n_days" and group.anchor_date is None:
            group.anchor_date = today
        start = current_cycle_start(group, today)
    else:
        start = today
    p = Period(group_id=group.id, starts_on=start, ends_on=period_end(group, start), status="open")
    db.add(p)
    db.flush()
    return p


def open_period(db: Session, group_id: str) -> Period | None:
    return db.scalars(
        select(Period).where(Period.group_id == group_id, Period.status == "open").order_by(Period.starts_on.desc())
    ).first()


def period_for_date(db: Session, group: Group, d: date, *, for_write: bool = True) -> Period:
    """Find the period a dated bill/settlement belongs to.

    Dates before the first period belong to the first period. Dates after the open period are rejected."""
    periods = db.scalars(select(Period).where(Period.group_id == group.id).order_by(Period.starts_on)).all()
    if not periods:
        raise APIError(500, "internal_error", "Group has no periods")
    target = None
    if d < periods[0].starts_on:
        target = periods[0]
    else:
        for p in periods:
            if p.starts_on <= d and (p.ends_on is None or d <= p.ends_on):
                target = p
                break
    if target is None:
        raise validation(
            "Date is after the current period", expense_date=f"must be on or before {periods[-1].ends_on.isoformat()}"
        )
    if for_write and target.status == "frozen":
        raise APIError(
            409,
            "period_frozen",
            f"The period {target.starts_on.isoformat()}–{target.ends_on.isoformat() if target.ends_on else ''} is frozen",
            {"period_id": target.id},
        )
    return target


def apply_recurrence_change(db: Session, group: Group, was_recurring: bool) -> date | None:
    """Called after recurrence settings change. Returns the date the new settings take effect."""
    current = open_period(db, group.id)
    if current is None:
        return None
    if group.is_recurring and not was_recurring:
        if group.cadence == "every_n_days" and group.anchor_date is None:
            group.anchor_date = local_today(group)
        current.ends_on = next_boundary(group, local_today(group)) - timedelta(days=1)
        if current.ends_on < current.starts_on:
            current.ends_on = current.starts_on
        return current.starts_on
    if not group.is_recurring and was_recurring:
        current.ends_on = None
        return current.starts_on
    return current.ends_on + timedelta(days=1) if current.ends_on else None


def _start_grace(group: Group, period: Period) -> None:
    period.status = "grace"
    period.grace_until = grace_until(group, period.ends_on)


def cut_period(db: Session, group: Group, ends_on: date | None, actor_id: str) -> Period:
    current = open_period(db, group.id)
    if current is None:
        raise APIError(409, "conflict", "No open period")
    ends_on = ends_on or local_today(group)
    if ends_on < current.starts_on:
        raise validation("ends_on is before the period start", ends_on=f"must be >= {current.starts_on.isoformat()}")
    if current.ends_on is not None and ends_on > current.ends_on:
        raise validation("ends_on is after the period end", ends_on=f"must be <= {current.ends_on.isoformat()}")
    if not group.is_recurring:
        raise validation("Only recurring groups have periods to cut")
    current.ends_on = ends_on
    _start_grace(group, current)
    start = ends_on + timedelta(days=1)
    new = Period(group_id=group.id, starts_on=start, ends_on=period_end(group, start), status="open")
    db.add(new)
    db.flush()
    audit.log(db, actor_id, "period", current.id, "cut", {"ends_on": ends_on.isoformat()})
    freeze_due(db, group)
    return new


def freeze_period(db: Session, group: Group, period: Period) -> None:
    b = compute_period_balances(db, period)
    members = db.scalars(
        select(GroupMember.user_id).where(GroupMember.group_id == group.id, GroupMember.left_at.is_(None))
    ).all()
    member_set = set(members)
    for uid in sorted(member_set | b.user_ids):
        db.add(PeriodMember(period_id=period.id, user_id=uid, is_guest=uid not in member_set))
    for uid in sorted(b.user_ids):
        db.add(PeriodBalance(period_id=period.id, user_id=uid, paid=b.paid[uid], owed=b.owed[uid], net=b.outstanding_net[uid]))
    for frm, to, amt in b.suggested_transfers():
        db.add(PeriodTransfer(period_id=period.id, from_user_id=frm, to_user_id=to, amount=amt))
    period.status = "frozen"
    period.frozen_at = utcnow()
    audit.log(db, None, "period", period.id, "freeze", None)


def freeze_due(db: Session, group: Group, now: datetime | None = None) -> None:
    now = now or utcnow()
    for p in db.scalars(select(Period).where(Period.group_id == group.id, Period.status == "grace")).all():
        if p.grace_until is not None and p.grace_until <= now:
            freeze_period(db, group, p)


def rollover_group(db: Session, group: Group, now: datetime | None = None) -> None:
    """Idempotent: catches up any number of missed cutoffs."""
    now = now or utcnow()
    if group.is_recurring:
        today = local_today(group, now)
        for _ in range(1000):  # safety bound
            current = open_period(db, group.id)
            if current is None or current.ends_on is None or current.ends_on >= today:
                break
            _start_grace(group, current)
            start = current.ends_on + timedelta(days=1)
            db.add(Period(group_id=group.id, starts_on=start, ends_on=period_end(group, start), status="open"))
            db.flush()
    freeze_due(db, group, now)


def rollover_all(db: Session, now: datetime | None = None) -> None:
    groups = db.scalars(
        select(Group).where(Group.archived_at.is_(None)).where(
            Group.is_recurring.is_(True) | Group.id.in_(select(Period.group_id).where(Period.status == "grace"))
        )
    ).all()
    for g in groups:
        rollover_group(db, g, now)
        db.flush()
