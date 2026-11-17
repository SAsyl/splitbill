"""Bill math: who owes how much, rounded so the parts sum exactly to the total."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import floor
from typing import Any

Bill = dict[str, Any]

ROUND_UNIT = 100  # minor units per rounding step: 100 = whole tenge/rubles


def item_split(item: dict[str, Any], people: dict[str, Any]) -> tuple[dict[str, Fraction], Fraction]:
    """One item: exact cost per person and the unclaimed rest.

    An item of N portions costs price/N per portion: unclaimed portions stay unclaimed instead of
    making the others pay for them. Claiming more portions than printed splits the price by count.
    """
    counts = {pid: c for pid, c in item["shares"].items() if c > 0 and pid in people}
    claimed = sum(counts.values())
    if claimed == 0:
        return {}, Fraction(item["price"])
    portions = max(item.get("qty", 1), claimed)
    costs = {pid: Fraction(item["price"] * count, portions) for pid, count in counts.items()}
    return costs, Fraction(item["price"] * (portions - claimed), portions)


def service_factor(bill: Bill, item: dict[str, Any]) -> Fraction:
    """Price multiplier of one item: set menus priced with the service included are exempt."""
    if item.get("no_service"):
        return Fraction(1)
    return 1 + Fraction(Decimal(str(bill["service_pct"]))) / 100


def exact_shares(bill: Bill) -> tuple[dict[str, Fraction], Fraction]:
    """Per-person exact amounts (service included) and the unassigned sum (before service)."""
    shares: dict[str, Fraction] = {pid: Fraction(0) for pid in bill["people"]}
    unassigned = Fraction(0)
    for item in bill["items"]:
        costs, rest = item_split(item, shares)
        unassigned += rest
        factor = service_factor(bill, item)
        for pid, cost in costs.items():
            shares[pid] += cost * factor
    return shares, unassigned


def round_preserving_sum(exact: dict[str, Fraction], unit: int) -> dict[str, int]:
    """Largest-remainder rounding: each part rounded to `unit`, parts sum to rounded total."""
    in_units = {pid: v / unit for pid, v in exact.items()}
    floors = {pid: floor(v) for pid, v in in_units.items()}
    target = floor(sum(in_units.values(), Fraction(0)) + Fraction(1, 2))
    leftover = target - sum(floors.values())
    by_remainder = sorted(in_units, key=lambda pid: (floors[pid] - in_units[pid], pid))
    for pid in by_remainder[:leftover]:
        floors[pid] += 1
    return {pid: n * unit for pid, n in floors.items()}


def compute(bill: Bill, unit: int = ROUND_UNIT) -> dict[str, Any]:
    """Final amounts per person in minor units, plus totals for the UI."""
    shares, unassigned = exact_shares(bill)
    owed = round_preserving_sum(shares, unit)
    items_sum = sum(item["price"] for item in bill["items"])
    with_service = sum((item["price"] * service_factor(bill, item) for item in bill["items"]), Fraction(0))
    return {
        "owed": owed,
        "items_sum": items_sum,
        "with_service": round(with_service),
        "unassigned": round(unassigned),
        "total": sum(owed.values()),
    }


def _demo() -> None:
    people = {p: {"name": p, "paid": False} for p in "abc"}
    bill: Bill = {
        "people": people,
        "service_pct": 10,
        "items": [
            {"price": 1000_00, "shares": {"a": 1, "b": 1, "c": 1}},  # 333.33 each
            {"price": 450_00, "shares": {"a": 2, "b": 1}},  # a drank 2 of 3 beers
            {"price": 999_00, "shares": {}},  # nobody claimed it
        ],
    }
    r = compute(bill)
    assert r["unassigned"] == 999_00
    # exact: a=633.33*1.1=696.67, b=483.33*1.1=531.67, c=333.33*1.1=366.67 → sum 1595
    assert r["owed"] == {"a": 697_00, "b": 532_00, "c": 366_00}, r["owed"]
    assert r["total"] == 1595_00

    # 10 people, 1000 split evenly: rounding must not lose or invent money
    ten: Bill = {
        "people": {str(i): {} for i in range(10)},
        "service_pct": 12.5,
        "items": [{"price": 1001_00, "shares": {str(i): 1 for i in range(10)}}],
    }
    r = compute(ten)
    assert r["total"] == 1126_00, r["total"]  # 1001 * 1.125 = 1126.125
    assert sorted(set(r["owed"].values())) == [112_00, 113_00]
    # "Set x4": 3 of 4 portions claimed, the 4th is not spread over the three
    sets: Bill = {
        "people": {p: {} for p in "abc"},
        "service_pct": 0,
        "items": [{"price": 400_00, "qty": 4, "shares": {"a": 1, "b": 1, "c": 1}}],
    }
    r = compute(sets)
    assert r["owed"] == {"a": 100_00, "b": 100_00, "c": 100_00} and r["unassigned"] == 100_00, r
    sets["items"][0]["shares"]["a"] = 3  # 5 claimed of 4: split by count, nothing unassigned
    r = compute(sets)
    assert r["owed"] == {"a": 240_00, "b": 80_00, "c": 80_00} and r["unassigned"] == 0, r
    # Service-inclusive set: 10% only on the tea
    exempt: Bill = {
        "people": {"a": {}},
        "service_pct": 10,
        "items": [{"price": 500_00, "no_service": True, "shares": {"a": 1}}, {"price": 100_00, "shares": {"a": 1}}],
    }
    r = compute(exempt)
    assert r["owed"] == {"a": 610_00} and r["with_service"] == 610_00, r
    print("split.py: ok")


if __name__ == "__main__":
    _demo()
