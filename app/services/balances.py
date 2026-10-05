"""Balance math for a group period.

owed(A→B) = Σ unchecked shares where A is the debtor on a bill paid by B
          − Σ settlements A→B that link no shares   (linked ones are reflected by the checked flags)
"""

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Expense, ExpenseShare, Period, Settlement


@dataclass
class PeriodBalances:
    user_ids: set[str] = field(default_factory=set)
    paid: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    owed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    settled_in: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    settled_out: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    pairwise: list[tuple[str, str, int]] = field(default_factory=list)  # (from, to, amount) netted, amount > 0
    outstanding_net: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_gross_debt: int = 0  # Σ non-payer shares (what had to be paid back, before any checks)

    def suggested_transfers(self) -> list[tuple[str, str, int]]:
        return simplify(self.outstanding_net)


def simplify(net: dict[str, int]) -> list[tuple[str, str, int]]:
    """Greedy min-cash-flow: largest debtor pays largest creditor. At most n-1 transfers."""
    creditors = sorted(((v, u) for u, v in net.items() if v > 0), key=lambda x: (-x[0], x[1]))
    debtors = sorted(((-v, u) for u, v in net.items() if v < 0), key=lambda x: (-x[0], x[1]))
    creditors = [list(c) for c in creditors]
    debtors = [list(d) for d in debtors]
    out: list[tuple[str, str, int]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i][0], creditors[j][0])
        if amt > 0:
            out.append((debtors[i][1], creditors[j][1], amt))
        debtors[i][0] -= amt
        creditors[j][0] -= amt
        if debtors[i][0] == 0:
            i += 1
        if creditors[j][0] == 0:
            j += 1
    return out


def compute_period_balances(db: Session, period: Period) -> PeriodBalances:
    b = PeriodBalances()
    debt: dict[tuple[str, str], int] = defaultdict(int)

    expenses = db.scalars(
        select(Expense).where(Expense.period_id == period.id, Expense.deleted_at.is_(None))
    ).all()
    for e in expenses:
        b.user_ids.add(e.payer_id)
        b.paid[e.payer_id] += e.amount
        for s in e.shares:
            b.user_ids.add(s.user_id)
            b.owed[s.user_id] += s.amount
            if s.user_id == e.payer_id or s.amount == 0:
                continue
            b.total_gross_debt += s.amount
            if s.checked_at is None:
                debt[(s.user_id, e.payer_id)] += s.amount

    settlements = db.scalars(
        select(Settlement).where(Settlement.period_id == period.id, Settlement.deleted_at.is_(None))
    ).all()
    linked = set(
        db.scalars(
            select(ExpenseShare.settlement_id).where(
                ExpenseShare.settlement_id.in_([s.id for s in settlements])
            )
        ).all()
    ) if settlements else set()
    for s in settlements:
        b.user_ids.update((s.from_user_id, s.to_user_id))
        b.settled_out[s.from_user_id] += s.amount
        b.settled_in[s.to_user_id] += s.amount
        if s.id not in linked:
            debt[(s.from_user_id, s.to_user_id)] -= s.amount

    seen: set[tuple[str, str]] = set()
    for (a, c) in list(debt.keys()):
        key = tuple(sorted((a, c)))
        if key in seen:
            continue
        seen.add(key)
        x, y = key
        net = debt.get((x, y), 0) - debt.get((y, x), 0)  # >0: x owes y
        if net > 0:
            b.pairwise.append((x, y, net))
        elif net < 0:
            b.pairwise.append((y, x, -net))
    b.pairwise.sort(key=lambda t: (t[0], t[1]))
    for frm, to, amt in b.pairwise:
        b.outstanding_net[frm] -= amt
        b.outstanding_net[to] += amt
    return b
