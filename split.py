"""Bill math: who owes how much, rounded so the parts sum exactly to the total."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from math import floor
from typing import Any

Bill = dict[str, Any]

ROUND_UNIT = 100  # minor units per rounding step: 100 = whole tenge/rubles


def exact_shares(bill: Bill) -> tuple[dict[str, Fraction], int]:
    """Per-person exact subtotals (before service) and the unassigned sum."""
    shares: dict[str, Fraction] = {pid: Fraction(0) for pid in bill["people"]}
    unassigned = 0
    for item in bill["items"]:
        counts = {pid: c for pid, c in item["shares"].items() if c > 0 and pid in shares}
        total_count = sum(counts.values())
        if total_count == 0:
            unassigned += item["price"]
            continue
        for pid, count in counts.items():
            shares[pid] += Fraction(item["price"] * count, total_count)
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
    multiplier = 1 + Fraction(Decimal(str(bill["service_pct"]))) / 100
    owed = round_preserving_sum({pid: v * multiplier for pid, v in shares.items()}, unit)
    items_sum = sum(item["price"] for item in bill["items"])
    return {
        "owed": owed,
        "items_sum": items_sum,
        "unassigned": unassigned,
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
    print("split.py: ok")


if __name__ == "__main__":
    _demo()
