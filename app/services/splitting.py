"""Split math. All amounts are integer minor units; results always sum exactly to the total."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from app.errors import APIError, validation

SPLIT_METHODS = ("equal", "exact", "percent", "shares", "itemized")


@dataclass
class ParticipantInput:
    user_id: str
    amount: int | None = None  # exact
    percent: str | None = None  # percent
    weight: str | None = None  # shares


@dataclass
class ItemInput:
    name: str
    amount: int
    participant_ids: list[str] = field(default_factory=list)


@dataclass
class ShareResult:
    user_id: str
    amount: int
    split_value: str | None


def allocate(total: int, weights: list[Fraction]) -> list[int]:
    """Largest-remainder allocation of `total` proportionally to `weights`.

    Leftover units go to the largest fractional remainders, ties broken by input order
    (callers sort participants by user id, so this is deterministic)."""
    if not weights:
        return []
    wsum = sum(weights)
    if wsum == 0:
        weights = [Fraction(1)] * len(weights)
        wsum = Fraction(len(weights))
    exact = [Fraction(total) * w / wsum for w in weights]
    floors = [int(e // 1) for e in exact]
    leftover = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


def _decimal(value: str | None, field_name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise validation(f"Invalid {field_name}", **{field_name: "must be a decimal string"})
    if not d.is_finite() or d < 0:
        raise validation(f"Invalid {field_name}", **{field_name: "must be a non-negative number"})
    return d


def compute_shares(
    amount: int, method: str, participants: list[ParticipantInput], items: list[ItemInput] | None = None
) -> list[ShareResult]:
    if amount <= 0:
        raise validation("Amount must be positive", amount="must be > 0")
    if method not in SPLIT_METHODS:
        raise validation("Unknown split method", split_method=f"must be one of {', '.join(SPLIT_METHODS)}")
    if not participants:
        raise validation("At least one participant is required", participants="must not be empty")
    ids = [p.user_id for p in participants]
    if len(set(ids)) != len(ids):
        raise validation("Duplicate participants", participants="each person may appear once")

    parts = sorted(participants, key=lambda p: p.user_id)

    if method == "equal":
        amounts = allocate(amount, [Fraction(1)] * len(parts))
        return [ShareResult(p.user_id, a, None) for p, a in zip(parts, amounts)]

    if method == "exact":
        for p in parts:
            if p.amount is None or p.amount < 0:
                raise validation("Each participant needs a non-negative amount", participants="amount required")
        total = sum(p.amount for p in parts)
        if total != amount:
            raise APIError(
                400, "split_mismatch", "Exact amounts must add up to the total", {"expected": amount, "actual": total}
            )
        return [ShareResult(p.user_id, p.amount, str(p.amount)) for p in parts]

    if method == "percent":
        pcts = [_decimal(p.percent, "percent") for p in parts]
        if sum(pcts) != Decimal(100):
            raise APIError(
                400, "split_mismatch", "Percentages must add up to 100", {"expected": "100", "actual": str(sum(pcts))}
            )
        amounts = allocate(amount, [Fraction(d) for d in pcts])
        return [ShareResult(p.user_id, a, str(d)) for p, a, d in zip(parts, amounts, pcts)]

    if method == "shares":
        weights = [_decimal(p.weight, "weight") for p in parts]
        if any(w <= 0 for w in weights):
            raise validation("Weights must be positive", weight="must be > 0")
        amounts = allocate(amount, [Fraction(w) for w in weights])
        return [ShareResult(p.user_id, a, str(w)) for p, a, w in zip(parts, amounts, weights)]

    # itemized
    items = items or []
    if not items:
        raise validation("Itemized split needs at least one item", items="must not be empty")
    part_ids = [p.user_id for p in parts]
    part_set = set(part_ids)
    item_total = 0
    subtotal: dict[str, int] = {uid: 0 for uid in part_ids}
    for item in items:
        if item.amount < 0:
            raise validation("Item amounts must be non-negative", items="amount must be >= 0")
        item_total += item.amount
        who = sorted(set(item.participant_ids)) or part_ids
        unknown = set(who) - part_set
        if unknown:
            raise validation("Item participants must also be bill participants", items=f"unknown: {sorted(unknown)}")
        for uid, a in zip(who, allocate(item.amount, [Fraction(1)] * len(who))):
            subtotal[uid] += a
    if item_total > amount:
        raise APIError(
            400, "split_mismatch", "Items add up to more than the total", {"expected": amount, "actual": item_total}
        )
    extra = amount - item_total
    extras = allocate(extra, [Fraction(subtotal[uid]) for uid in part_ids])
    return [ShareResult(uid, subtotal[uid] + e, None) for uid, e in zip(part_ids, extras)]
