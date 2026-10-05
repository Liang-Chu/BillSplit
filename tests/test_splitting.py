import pytest

from app.errors import APIError
from app.services.splitting import ItemInput, ParticipantInput as P, allocate, compute_shares


def amounts(shares):
    return {s.user_id: s.amount for s in shares}


def test_allocate_sums_exactly():
    from fractions import Fraction

    for total in (1, 2, 100, 1001, 99999):
        for n in (1, 2, 3, 7):
            assert sum(allocate(total, [Fraction(1)] * n)) == total


def test_equal_remainder_is_deterministic():
    r = amounts(compute_shares(1000, "equal", [P("c"), P("a"), P("b")]))
    assert r == {"a": 334, "b": 333, "c": 333}


def test_exact_must_match_total():
    with pytest.raises(APIError) as e:
        compute_shares(1000, "exact", [P("a", amount=500), P("b", amount=400)])
    assert e.value.code == "split_mismatch"
    assert amounts(compute_shares(1000, "exact", [P("a", amount=600), P("b", amount=400)])) == {"a": 600, "b": 400}


def test_percent():
    r = amounts(compute_shares(1000, "percent", [P("a", percent="33.33"), P("b", percent="33.33"), P("c", percent="33.34")]))
    assert sum(r.values()) == 1000
    with pytest.raises(APIError):
        compute_shares(1000, "percent", [P("a", percent="50"), P("b", percent="40")])


def test_shares_weights():
    assert amounts(compute_shares(900, "shares", [P("a", weight="2"), P("b", weight="1")])) == {"a": 600, "b": 300}


def test_itemized_spreads_tax_proportionally():
    shares = compute_shares(
        1100,
        "itemized",
        [P("a"), P("b")],
        [ItemInput("steak", 600, ["a"]), ItemInput("salad", 200, ["b"]), ItemInput("bread", 200, [])],
    )
    r = amounts(shares)
    # subtotals a=700, b=300; tax 100 split 70/30
    assert r == {"a": 770, "b": 330}


def test_itemized_items_over_total():
    with pytest.raises(APIError) as e:
        compute_shares(500, "itemized", [P("a")], [ItemInput("x", 600, [])])
    assert e.value.code == "split_mismatch"


def test_duplicates_rejected():
    with pytest.raises(APIError):
        compute_shares(100, "equal", [P("a"), P("a")])
