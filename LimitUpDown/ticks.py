#!/usr/bin/env python3
"""Tick tier tables.

A tick table is a price ladder: at or above this price, the tick is that.
Two sources produce the same ladder - the ATS's own .tsr file (Indonesia)
and rows in config/ticks.csv (everywhere else) - so exactly one lookup
serves both and neither can develop its own rounding behaviour.

The tick is chosen from the REFERENCE price, not from the limit being
rounded.  That is what LimitUpDown.r:315 does and changing it would move
prices near a tier boundary.

    python ticks.py --self-test
"""

from __future__ import annotations

from decimal import Decimal

Tiers = list  # list[tuple[Decimal, Decimal]]


def parse_tsr(text: str) -> Tiers:
    """Read the ATS tick-size-rule format: whitespace-delimited, no header,
    RuleName Floor TickValue.  LimitUpDown.r:308-312 reads it with sep=" "
    and names the columns in that order."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        out[Decimal(parts[1])] = Decimal(parts[2])
    return sorted(out.items())


def parse_rows(rows) -> Tiers:
    out = {}
    for r in rows:
        out[Decimal(str(r["FloorFrom"]))] = Decimal(str(r["Tick"]))
    return sorted(out.items())


def tick_for(tiers: Tiers, ref: Decimal):
    """The tick of the highest tier at or below ref, or None if ref is below
    every floor - which is a row we must not price, not a row we may guess
    at."""
    found = None
    for floor, tick in tiers:
        if floor <= ref:
            found = tick
        else:
            break
    return found


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    D = Decimal
    print("ticks --self-test\n\nreading a .tsr file")
    tsr = "SPOL_JKT 0 1\nSPOL_JKT 200 2\nSPOL_JKT 500 5\n"
    check("three tiers, floor and tick", parse_tsr(tsr),
          [(D("0"), D("1")), (D("200"), D("2")), (D("500"), D("5"))])
    check("blank lines are skipped", parse_tsr("A 0 1\n\n\nA 200 2\n"),
          [(D("0"), D("1")), (D("200"), D("2"))])
    check("rows arrive sorted even when the file is not",
          parse_tsr("A 500 5\nA 0 1\nA 200 2\n"),
          [(D("0"), D("1")), (D("200"), D("2")), (D("500"), D("5"))])
    check("duplicate floors collapse to the last one written",
          parse_tsr("A 0 1\nA 0 2\n"), [(D("0"), D("2"))])
    check("an empty file is an empty ladder, not an error",
          parse_tsr(""), [])

    print("\nreading config rows")
    check("same structure from ticks.csv",
          parse_rows([{"FloorFrom": "0", "Tick": "0.01"},
                      {"FloorFrom": "10", "Tick": "0.05"}]),
          [(D("0"), D("0.01")), (D("10"), D("0.05"))])

    print("\nresolving a price to a tick")
    t = [(D("0"), D("1")), (D("200"), D("2")), (D("500"), D("5"))]
    check("below the second tier", tick_for(t, D("199")), D("1"))
    check("exactly on a boundary takes that tier", tick_for(t, D("200")),
          D("2"))
    check("just above", tick_for(t, D("201")), D("2"))
    check("above the last tier stays on it", tick_for(t, D("99999")), D("5"))
    check("an empty ladder cannot answer", tick_for([], D("100")), None)
    check("a price below every floor cannot answer",
          tick_for([(D("50"), D("1"))], D("49")), None)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
