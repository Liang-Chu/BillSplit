from datetime import date

from app.models import Group
from app.services.periods import current_cycle_start, next_boundary, period_end


def g(**kw):
    defaults = dict(is_recurring=True, cadence="monthly", anchor_day=1, timezone="UTC", grace_days=3)
    defaults.update(kw)
    return Group(**defaults)


def test_monthly():
    grp = g()
    assert current_cycle_start(grp, date(2026, 10, 5)) == date(2026, 10, 1)
    assert current_cycle_start(grp, date(2026, 10, 1)) == date(2026, 10, 1)
    assert period_end(grp, date(2026, 10, 1)) == date(2026, 10, 31)
    assert period_end(grp, date(2026, 12, 1)) == date(2026, 12, 31)
    assert period_end(grp, date(2026, 10, 16)) == date(2026, 10, 31)  # after a manual cut


def test_monthly_anchor_15():
    grp = g(anchor_day=15)
    assert current_cycle_start(grp, date(2026, 10, 5)) == date(2026, 9, 15)
    assert period_end(grp, date(2026, 9, 15)) == date(2026, 10, 14)


def test_weekly():
    grp = g(cadence="weekly", anchor_day=1)  # Monday
    assert current_cycle_start(grp, date(2026, 10, 8)) == date(2026, 10, 5)  # Thu → Mon
    assert period_end(grp, date(2026, 10, 5)) == date(2026, 10, 11)


def test_every_n_days():
    grp = g(cadence="every_n_days", cadence_n=10, anchor_date=date(2026, 1, 1), anchor_day=None)
    assert current_cycle_start(grp, date(2026, 1, 25)) == date(2026, 1, 21)
    assert next_boundary(grp, date(2026, 1, 21)) == date(2026, 1, 31)
