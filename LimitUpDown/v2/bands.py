#!/usr/bin/env python3
"""Daily price bands: tier selection, the band itself, and tick rounding.

Numbers in, numbers out.  No kdb, no files, no clock - which is what makes
every market rule here testable on a laptop.

THE RULE IS DATA.  A market is a set of tiers in config/bands.csv, and a
tier is a floor on the reference price plus an up and a down move.  A flat
symmetric market is one tier with equal values; Indonesia is three tiers;
Japan (later) is thirty-three tiers with kind='abs'.  None of them is a
branch in this file.

MOST MARKETS DO NOT ROUND, and rounding='none' is the common case.  The
band is ref x (1 +/- pct) and that number is published as it comes out.
Two reasons:

  Exactly ONE market rounds - Indonesia - because Indonesia is the one
  market whose band is computed here rather than read from Bloomberg.
  Every other market's limits arrive already struck by the exchange.

  And inward rounding does not change which orders the band admits.
  floor(raw/tick)*tick is the largest valid tick price <= raw, so for any
  order price m that is itself on a tick, m <= rounded exactly when
  m <= raw.  Korea at 71,300 with 100 KRW ticks: raw limit up 92,690,
  rounded 92,600, and the only ticks nearby are 92,600 and 92,700.  Nothing
  falls between them.  Rounding makes the published number match what the
  exchange prints; it does not make the check stricter.

So a market rounds only where we know it should: Indonesia today, Japan
when it arrives.  A venue with rounding='none' needs no tick table at all,
which is why there is no tick data to source for Korea, Malaysia, Taiwan,
China or the Philippines.

DECIMAL, NOT FLOAT.  floor(Decimal('1.15') / Decimal('0.05')) is 23.  In
binary floating point it is 22.  Tick rounding is exactly where that bites,
so every quantity here is a Decimal and the callers hand us Decimals.

    python bands.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import NamedTuple, Optional


class Tier(NamedTuple):
    kind: str            # 'pct' | 'abs'
    sym_prefix: str      # '' means the venue default
    floor_from: Decimal
    up: Decimal
    down: Decimal


class BandError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def select_tier(tiers, ticker: str, ref: Decimal) -> Optional[Tier]:
    """Prefix first, then floor.  Filtering by the longest matching prefix
    BEFORE looking at the floor matters: a STAR name must walk STAR's own
    ladder, not fall back onto the main board's."""
    matching = [t for t in tiers
                if t.sym_prefix == "" or ticker.startswith(t.sym_prefix)]
    if not matching:
        return None
    longest = max(len(t.sym_prefix) for t in matching)
    matching = [t for t in matching if len(t.sym_prefix) == longest]
    eligible = [t for t in matching if t.floor_from <= ref]
    if not eligible:
        return None
    return max(eligible, key=lambda t: t.floor_from)


def raw_band(tier: Tier, ref: Decimal):
    if tier.kind == "pct":
        return ref * (Decimal(1) + tier.up), ref * (Decimal(1) - tier.down)
    if tier.kind == "abs":
        return ref + tier.up, ref - tier.down
    raise BandError(f"unknown tier kind {tier.kind!r}")


def round_band(up: Decimal, down: Decimal, tick, rounding: str):
    """Most markets do not round at all - see the note in the module
    docstring.  'none' ignores the tick, which may be None."""
    if rounding == "none":
        return up, down
    if tick is None:
        raise BandError(f"rounding {rounding!r} needs a tick and none was "
                        f"resolved")
    if tick <= 0:
        raise BandError("tick is not positive")
    if rounding == "inward":
        return ((up / tick).to_integral_value(ROUND_FLOOR) * tick,
                (down / tick).to_integral_value(ROUND_CEILING) * tick)
    if rounding == "outward":
        return ((up / tick).to_integral_value(ROUND_CEILING) * tick,
                (down / tick).to_integral_value(ROUND_FLOOR) * tick)
    if rounding == "nearest":
        return ((up / tick).to_integral_value(ROUND_HALF_UP) * tick,
                (down / tick).to_integral_value(ROUND_HALF_UP) * tick)
    raise BandError(f"unknown rounding mode {rounding!r}")


def compute(tiers, ticker: str, ref: Decimal, tick,
            min_price: Optional[Decimal], rounding: str):
    if ref is None or ref <= 0:
        raise BandError("reference price is not positive")
    tier = select_tier(tiers, ticker, ref)
    if tier is None:
        raise BandError(f"no band tier for price {ref}")
    up, down = raw_band(tier, ref)
    if min_price is not None:
        down = max(down, min_price)      # floor first, THEN round
    up, down = round_band(up, down, tick, rounding)
    if not (up > down > 0):
        raise BandError(f"band is not sane: up={up} down={down}")
    return up, down


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

    def raises(name, fn, reason):
        nonlocal ok
        try:
            fn()
            got = "no exception"
        except BandError as e:
            got = e.reason
        except Exception as e:                       # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        good = got == reason
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {reason!r}"))

    D = Decimal

    def P(pfx, floor, up, dn):
        return Tier("pct", pfx, D(floor), D(up), D(dn))

    #  Indonesia, verified against Indo_maping_limit_up.csv:
    #  floors 50 / 200 / 5000, coefficients 1.35 / 1.25 / 1.2
    IDN = [P("", "50", "0.35", "0.35"),
           P("", "200", "0.25", "0.25"),
           P("", "5000", "0.20", "0.20")]

    print("bands --self-test\n\npicking the tier")
    check("the bottom tier", select_tier(IDN, "BBCA", D("100")), IDN[0])
    check("a boundary belongs to the tier it opens",
          select_tier(IDN, "BBCA", D("200")), IDN[1])
    check("just under stays below", select_tier(IDN, "BBCA", D("199")), IDN[0])
    check("the top tier is open ended",
          select_tier(IDN, "BBCA", D("999999")), IDN[2])
    check("below the lowest floor there is NO tier - Indonesia starts at 50 "
          "and a rupiah name under that is not ours to price",
          select_tier(IDN, "BBCA", D("49")), None)

    CN = [P("688", "0", "0.20", "0.20"), P("", "0", "0.10", "0.10")]
    print("\nsymbol prefixes - STAR and ChiNext")
    check("a 688 name takes the STAR tier",
          select_tier(CN, "688001", D("50")), CN[0])
    check("anything else takes the venue default",
          select_tier(CN, "600001", D("50")), CN[1])
    check("the longest matching prefix wins, then the floor",
          select_tier([P("6", "0", "0.15", "0.15"),
                       P("688", "0", "0.20", "0.20"),
                       P("", "0", "0.10", "0.10")], "688001", D("50")),
          Tier("pct", "688", D("0"), D("0.20"), D("0.20")))

    print("\nthe raw band")
    check("thirty five percent either way",
          raw_band(P("", "50", "0.35", "0.35"), D("100")),
          (D("135.00"), D("65.00")))
    check("asymmetric is expressible",
          raw_band(P("", "0", "0.20", "0.10"), D("100")),
          (D("120.00"), D("90.00")))
    check("an absolute rule adds and subtracts instead - this is how Japan "
          "arrives later with no code change",
          raw_band(Tier("abs", "", D("0"), D("300"), D("300")), D("1234")),
          (D("1534"), D("934")))

    print("\nnot rounding at all - the common case")
    check("'none' publishes the band exactly as computed",
          round_band(D("92690"), D("49910"), None, "none"),
          (D("92690"), D("49910")))
    check("and does not need a tick to do it",
          compute([P("", "0", "0.30", "0.30")], "005930", D("71300"), None,
                  None, "none"),
          (D("92690.0"), D("49910.0")))
    raises("but any OTHER mode without a tick is a bug, not a silent pass",
           lambda: round_band(D("100"), D("90"), None, "inward"),
           "rounding 'inward' needs a tick and none was resolved")

    print("\nrounding to a tick")
    check("inward pulls both bounds into the band",
          round_band(D("1358.01"), D("1111.10"), D("1"), "inward"),
          (D("1358"), D("1112")))
    check("outward pushes both out",
          round_band(D("1358.01"), D("1111.10"), D("1"), "outward"),
          (D("1359"), D("1111")))
    check("nearest goes to the closest tick",
          round_band(D("1358.01"), D("1111.90"), D("1"), "nearest"),
          (D("1358"), D("1112")))
    check("a half rounds AWAY from zero, not to even",
          round_band(D("1358.50"), D("1111.50"), D("1"), "nearest"),
          (D("1359"), D("1112")))
    check("an exact multiple is left alone by inward",
          round_band(D("110.00"), D("90.00"), D("0.05"), "inward"),
          (D("110.00"), D("90.00")))
    check("the float trap: 1.15 over a 0.05 tick is 23 ticks, not 22",
          round_band(D("1.15"), D("1.15"), D("0.05"), "inward"),
          (D("1.15"), D("1.15")))

    print("\ncompute, end to end")
    check("Indonesia at 100 with a 1 tick",
          compute(IDN, "BBCA", D("100"), D("1"), D("50"), "inward"),
          (D("135"), D("65")))
    check("the minimum price floors the down limit BEFORE rounding",
          compute(IDN, "BBCA", D("60"), D("1"), D("50"), "inward"),
          (D("81"), D("50")))
    check("no minimum price configured, no floor",
          compute(IDN, "BBCA", D("60"), D("1"), None, "inward"),
          (D("81"), D("39")))
    raises("a price under every tier is refused, not guessed",
           lambda: compute(IDN, "BBCA", D("49"), D("1"), D("50"), "inward"),
           "no band tier for price 49")
    raises("a zero reference price is refused",
           lambda: compute(IDN, "BBCA", D("0"), D("1"), None, "inward"),
           "reference price is not positive")
    raises("a negative reference price is refused",
           lambda: compute(IDN, "BBCA", D("-5"), D("1"), None, "inward"),
           "reference price is not positive")
    raises("an unknown rounding mode is a config bug, not a default",
           lambda: compute(IDN, "BBCA", D("100"), D("1"), None, "sideways"),
           "unknown rounding mode 'sideways'")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
