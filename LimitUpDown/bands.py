#!/usr/bin/env python3
"""Daily price bands: tier selection, the band itself, and tick rounding.

Numbers in, numbers out.  No kdb, no files, no clock - which is what makes
every market rule here testable on a laptop.

THE RULE IS DATA.  A market is a set of tiers in config/bands.csv, and a
tier is a floor on the reference price plus an up and a down move.  A flat
symmetric market is one tier with equal values; Indonesia is three tiers;
Japan (later) is thirty-three tiers with kind='abs'.  None of them is a
branch in this file.

MOST MARKETS DO NOT ROUND, and rounding='none' is still the common case: the
band is ref x (1 +/- pct) and that number is published as it comes out.  A
venue rounds only where markets.csv says so - Indonesia and Korea's KSC-MAIN
today - and a venue with rounding='none' needs no tick at all.

Inward rounding does not change which orders the band admits.
floor(raw/tick)*tick is the largest valid tick price <= raw, so for any
order price m that is itself on a tick, m <= rounded exactly when m <= raw.
Rounding makes the published number match what the exchange prints; it does
not make the check stricter.

EACH LEG ROUNDS ON ITS OWN TICK, which is why `tick` may be a callable
rather than a value.  A ladder is a function of price and the two legs are
at different prices, so a band that spans a tier boundary has two ticks and
not one.

  Korea's 000250 KQ is the case that proved it.  Previous close 157,500,
  which is under 200,000, so the tick THERE is 100.  The limit up is
  204,750, which is over it, where the tick is 500.  Rounded on the close's
  tick we published 204,700; Bloomberg publishes 204,500, which is 204,750
  floored on 500.

The caller decides what each leg's tick is - see price_computed, which takes
the COARSER of the close's tick and the leg's own.  Two Bloomberg-confirmed
names pin that down and they point opposite ways, so neither price alone is
the rule; the comment there has both.

DECIMAL, NOT FLOAT.  floor(Decimal('1.15') / Decimal('0.05')) is 23.  In
binary floating point it is 22.  Tick rounding is exactly where that bites,
so every quantity here is a Decimal and the callers hand us Decimals.

    python bands.py --self-test
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import NamedTuple, Optional


class Tier(NamedTuple):
    kind: str            # 'pct' | 'abs'
    sym_prefix: str      # '' means the venue default
    floor_from: Decimal
    up: Decimal
    down: Decimal
    #  A word from the exchange's own name for the security, matched case
    #  insensitively.  '' means the venue default, exactly as sym_prefix
    #  does.  Korea prices a leveraged product at twice the ordinary band
    #  and nothing in the TICKER says so, which is why this exists.
    name_marker: str = ""


class BandError(Exception):
    """A band that could not be computed.

    `reason` GROUPS - the run report counts by it, so it must not carry a
    price or any other per-name value, or one cause fragments into a line
    per name.  Anything name-specific goes in `detail`."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


#  A multiple, as an exchange writes one: 2X, -2X, 0.5x, 3X.  Anchored so
#  that MATRIX 2XL is not a 2x product and a bare "x" is not a multiple.
MULTIPLE = re.compile(r"(?<![a-z0-9.])-?(\d+(?:\.\d+)?)x(?![a-z0-9])")


def multiple_in(name: str):
    """The multiple this name carries, as it is written, or None.

    "SAMSUNG KODEX Inverse 3X ETN" -> "3x".  The SIGN is dropped: a -2x and
    a 2x move the same distance, and the band is a width."""
    m = MULTIPLE.search((name or "").lower())
    return f"{m.group(1)}x" if m else None


def marker_matches(marker: str, name: str) -> bool:
    """Is this marker a WORD of this name?

    Substring matching would make "2x" match anything containing it and
    "leverage" match Coverage Analytics, so the marker has to sit between
    separators.  Case folded, because the exchange name is whatever the
    feed stored - Leverage, leverage and LEVERAGE are one product.

    Shared with kdbclose.is_leveraged so that "this name is special" and
    "this is the row for it" can never disagree."""
    if not marker:
        return True
    low = f" {(name or '').lower()} "
    seps = " -()/,."
    for a in seps:
        for b in seps:
            if f"{a}{marker}{b}" in low:
                return True
    return False


def select_tier(tiers, ticker: str, ref: Decimal,
                name: str = "") -> Optional[Tier]:
    """Name marker, then prefix, then floor.

    Each step keeps only the MOST SPECIFIC matches before the next one
    looks, and that order matters twice over.  A STAR name must walk STAR's
    own ladder rather than fall back onto the main board's - and a
    leveraged ETF must take its own band before either, because the thing
    that makes it leveraged is in the exchange's name for it and in nothing
    else we hold.

    An empty marker or prefix is the venue default and matches anything, so
    a venue with no special rows behaves exactly as it always did."""
    matching = [t for t in tiers
                if marker_matches(t.name_marker, name)]
    if not matching:
        return None

    #  A MULTIPLE OUTRANKS A WORD, whatever their lengths.  "Inverse 3X"
    #  matches both `inverse` and `3x`, and on length alone the longer
    #  `inverse` would win and price a 3x product at the 1x band - a third
    #  of its real width.  The multiple is what sets the band, so it wins.
    def rank(t):
        return (1 if multiple_in(t.name_marker) else 0, len(t.name_marker))

    strongest = max(rank(t) for t in matching)
    matching = [t for t in matching if rank(t) == strongest]

    matching = [t for t in matching
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


def _tick_at(tick, price: Decimal, rounding: str):
    """The tick for one leg.

    EACH LEG RESOLVES ITS OWN, because a tick ladder is a function of price
    and the two legs are at different prices.  `tick` may be a plain value,
    which both legs then share, or a callable taking the price being
    rounded - which is what a caller holding a ladder passes."""
    got = tick(price) if callable(tick) else tick
    if got is None:
        raise BandError(f"rounding {rounding!r} needs a tick and none was "
                        f"resolved")
    if got <= 0:
        raise BandError("tick is not positive")
    return got


def round_band(up: Decimal, down: Decimal, tick, rounding: str):
    """Most markets do not round at all - see the note in the module
    docstring.  'none' ignores the tick, which may be None."""
    if rounding == "none":
        return up, down
    if rounding not in ("inward", "outward", "nearest"):
        raise BandError(f"unknown rounding mode {rounding!r}")
    ut = _tick_at(tick, up, rounding)
    dt = _tick_at(tick, down, rounding)
    if rounding == "inward":
        #  A BAND ALREADY ON THE TICK IS LEFT EXACTLY WHERE IT IS.  8750 x
        #  1.3 is 11375, on the 5 tick table 10392 gives 0000D0 KP, and
        #  Bloomberg publishes 11375.
        #
        #  There was briefly an `inward-strict` here that moved such a band
        #  one tick further in.  It was inferred from ONE name, and a
        #  compare against Bloomberg over the whole Korean universe then
        #  showed hundreds out by exactly one tick, every one of them the
        #  wrong way.  The one name is the open question, not the rule:
        #
        #    0080Y0 KP  close 8025, leverage -> 12840/3210 here, and
        #               Bloomberg published 12835/3215.
        #
        #  No symmetric band around 8025 rounds to both of those on any
        #  tick, so something else about that name differs - its reference
        #  price, or a band that is not exactly 60%.  Worth resolving, but
        #  not by making every other name wrong.
        return ((up / ut).to_integral_value(ROUND_FLOOR) * ut,
                (down / dt).to_integral_value(ROUND_CEILING) * dt)
    if rounding == "outward":
        return ((up / ut).to_integral_value(ROUND_CEILING) * ut,
                (down / dt).to_integral_value(ROUND_FLOOR) * dt)
    return ((up / ut).to_integral_value(ROUND_HALF_UP) * ut,
            (down / dt).to_integral_value(ROUND_HALF_UP) * dt)


def compute(tiers, ticker: str, ref: Decimal, tick,
            min_price: Optional[Decimal], rounding: str, name: str = ""):
    if ref is None or ref <= 0:
        raise BandError("reference price is not positive")
    tier = select_tier(tiers, ticker, ref, name)
    if tier is None:
        raise BandError("no band tier for the previous close",
                        detail=f"price {ref}")
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

    print("\na band that already lands on its tick is LEFT ALONE")
    check("0000D0 KP: 8750 x 1.3 is 11375 exactly, on the 5 tick its table "
          "gives, and Bloomberg publishes 11375 - not 11370",
          round_band(D("11375"), D("6125"), D("5"), "inward"),
          (D("11375"), D("6125")))
    check("which is not a special case but what floor and ceiling already "
          "do - a band NOT on the tick still rounds inward",
          round_band(D("6695"), D("3605"), D("10"), "inward"),
          (D("6690"), D("3610")))

    print("\neach leg on its own tick, when the caller passes a callable")
    #  Korea's table 6132 around the 200,000 boundary: 100 below, 500 at or
    #  above.  000250 KQ's band straddles it.
    def kr(price):
        return D("500") if price >= D("200000") else D("100")

    check("A BAND THAT SPANS A TIER BOUNDARY HAS TWO TICKS - 204,750 "
          "floors on 500 to 204,500, which is what Bloomberg publishes, "
          "while the down leg keeps the 100 it sits on",
          round_band(D("204750"), D("110250"), kr, "inward"),
          (D("204500"), D("110300")))
    check("a callable that answers the same everywhere is the old "
          "behaviour, unchanged",
          round_band(D("204750"), D("110250"), lambda p: D("100"), "inward"),
          round_band(D("204750"), D("110250"), D("100"), "inward"))
    raises("a callable that cannot resolve a leg is a bug, not a silent pass",
           lambda: round_band(D("110"), D("90"), lambda p: None, "inward"),
           "rounding 'inward' needs a tick and none was resolved")
    raises("nor may it answer zero",
           lambda: round_band(D("110"), D("90"), lambda p: D("0"), "inward"),
           "tick is not positive")

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
           "no band tier for the previous close")
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
