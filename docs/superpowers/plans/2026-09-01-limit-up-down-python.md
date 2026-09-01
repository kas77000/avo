# Limit Up/Down Python Feed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `Nova/LimitUpDown/LimitUpDown.r` with a Python job that computes daily price bands for ID/MY/KR/PH/CN/TW from config-driven rules and a kdb reference price, with no Bloomberg dependency.

**Architecture:** Pure arithmetic modules (`bands.py`, `ticks.py`) that take numbers and return numbers, wrapped by I/O modules (`marketcfg.py`, `crosscode.py`, `kdbsource.py`) and an orchestrator (`limit_up_down.py`). Every market rule is a row in a CSV, not a branch in the code. kdb supplies one thing: the reference price.

**Tech Stack:** Python 3.13, stdlib only except `pykx` (lazily imported, only in `kdbsource.py`). No pandas, no pytest.

**Spec:** `Nova/docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md`

## Global Constraints

- **All price arithmetic uses `decimal.Decimal`.** Never float. Binary floats produce off-by-one-tick errors in `floor(raw / tick)`.
- **`nearest` rounding uses `ROUND_HALF_UP`**, not Python's default banker's rounding.
- **Tests are embedded `self_test()` functions** run via `python <module>.py --self-test`, following `kdb-queries/scripts/lib/price_bands.py`. No pytest, no test framework, stdlib only.
- **`pykx` is imported inside functions, never at module level**, so every other module runs on a machine with no kdb and no q licence.
- **The config key is `FidessaVenueID`.** `BBGVenueCode` is not unique (China `CG` and `CS` each map to two venues) and must never be used as a lookup key.
- **Output schema is fixed and must not change:** `#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue`
- **Report, never silently drop.** Every excluded row is counted and listed.
- **Nothing partially published.** Write to temp, validate, then copy.
- Markets in scope: ID, MY, KR, PH, CN, TW. Japan/Thailand/India are config-only additions later; no code may hardcode against their absence.

## Prerequisites

`Nova/` is not a git repository, so the commit steps below will fail until it is one. Before Task 1:

```bash
cd "C:/Users/user/Desktop/Projects/Nova"
git init
printf 'local_settings.py\n__pycache__/\n*.pyc\nout/\n' > .gitignore
git add -A && git commit -m "chore: initial commit of Nova LimitUpDown"
```

## File Structure

| File | Responsibility |
|---|---|
| `LimitUpDown/ticks.py` | Tick tier tables: load from `.tsr` or config rows, resolve a price to a tick. Pure. |
| `LimitUpDown/bands.py` | Tier selection, raw band arithmetic, tick rounding. Pure. |
| `LimitUpDown/marketcfg.py` | Load and validate `markets.csv` / `bands.csv` / `ticks.csv` into typed objects. |
| `LimitUpDown/crosscode.py` | Read `CrossCode.csv`, filter by security type, configured venue, and cutoff time. |
| `LimitUpDown/kdbsource.py` | Reference prices from kdb. The only module that imports pykx. |
| `LimitUpDown/mailer.py` | Failure and exclusion-report email. |
| `LimitUpDown/limit_up_down.py` | Orchestration, output validation, environment copy, `--demo`, `--compare`. |
| `LimitUpDown/config/markets.csv` | One row per venue. |
| `LimitUpDown/config/bands.csv` | One row per venue per band tier. |
| `LimitUpDown/config/ticks.csv` | One row per venue per tick tier. |
| `LimitUpDown/local_settings.py` | Gitignored: kdb host/port, paths, SMTP, recipients. |

Dependency direction is strictly one way: `limit_up_down.py` → everything else; `bands.py` and `ticks.py` depend on nothing.

---

### Task 1: Tick tier tables (`ticks.py`)

Pure module. Two readers producing one structure, one lookup.

**Files:**
- Create: `Nova/LimitUpDown/ticks.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Tiers = list[tuple[Decimal, Decimal]]` — `(floor_from, tick)`, sorted ascending by floor
  - `parse_tsr(text: str) -> Tiers`
  - `parse_rows(rows: list[dict]) -> Tiers` — rows have keys `FloorFrom`, `Tick`
  - `tick_for(tiers: Tiers, ref: Decimal) -> Decimal | None`

- [ ] **Step 1: Create the module with its self-test harness and the first failing checks**

```python
#!/usr/bin/env python3
"""Tick tier tables.

A tick table is a price ladder: at or above this price, the tick is that.
Two sources produce the same ladder - the ATS's own .tsr file (Indonesia)
and rows in config/ticks.csv (everywhere else) - so exactly one lookup
serves both and neither can develop its own rounding behaviour.

The tick is chosen from the REFERENCE price, not from the limit being
rounded.  That is what LimitUpDown.r:315 does and changing it would move
prices near a tier boundary.
"""

from __future__ import annotations

from decimal import Decimal

Tiers = list  # list[tuple[Decimal, Decimal]]


def parse_tsr(text: str) -> Tiers:
    raise NotImplementedError


def parse_rows(rows) -> Tiers:
    raise NotImplementedError


def tick_for(tiers: Tiers, ref: Decimal) -> Decimal | None:
    raise NotImplementedError


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
    check("above the last tier stays on it", tick_for(t, D("99999")),
          D("5"))
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
```

- [ ] **Step 2: Run it to verify the checks fail**

Run: `python Nova/LimitUpDown/ticks.py --self-test`
Expected: `NotImplementedError` — the functions have no bodies yet.

- [ ] **Step 3: Implement the three functions**

```python
def parse_tsr(text: str) -> Tiers:
    """Read the ATS tick-size-rule format: whitespace-delimited, no header,
    RuleName Floor TickValue.  LimitUpDown.r:308-312 reads it with
    sep=" " and names the columns in that order."""
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


def tick_for(tiers: Tiers, ref: Decimal) -> Decimal | None:
    """The tick of the highest tier at or below ref, or None if ref is
    below every floor - which is a row we must not price, not a row we
    may guess at."""
    found = None
    for floor, tick in tiers:
        if floor <= ref:
            found = tick
        else:
            break
    return found
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `python Nova/LimitUpDown/ticks.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/ticks.py
git commit -m "feat(luld): tick tier tables from .tsr or config rows"
```

---

### Task 2: Band arithmetic (`bands.py`)

**Files:**
- Create: `Nova/LimitUpDown/bands.py`

**Interfaces:**
- Consumes: `ticks.tick_for` is *not* called here — the caller passes a resolved tick in.
- Produces:
  - `Tier = NamedTuple(kind: str, sym_prefix: str, floor_from: Decimal, up: Decimal, down: Decimal)`
  - `BandError(Exception)` with a `.reason` string
  - `select_tier(tiers: list[Tier], ticker: str, ref: Decimal) -> Tier | None`
  - `raw_band(tier: Tier, ref: Decimal) -> tuple[Decimal, Decimal]` → `(up, down)`
  - `round_band(up, down, tick: Decimal, rounding: str) -> tuple[Decimal, Decimal]`
  - `compute(tiers, ticker, ref, tick, min_price, rounding) -> tuple[Decimal, Decimal]` — raises `BandError`

- [ ] **Step 1: Write the module with failing self-test checks**

```python
#!/usr/bin/env python3
"""Daily price bands: tier selection, the band itself, and tick rounding.

Numbers in, numbers out.  No kdb, no files, no clock - which is what makes
every market rule here testable on a laptop.

THE RULE IS DATA.  A market is a set of tiers in config/bands.csv, and a
tier is a floor on the reference price plus an up and a down move.  A flat
symmetric market is one tier with equal values; Indonesia is three tiers;
Japan (later) is thirty-three tiers with kind='abs'.  None of them is a
branch in this file.

DECIMAL, NOT FLOAT.  floor(Decimal('1.15') / Decimal('0.05')) is 23.
In binary floating point it is 22.  Tick rounding is exactly where that
bites, so every quantity here is a Decimal and the callers hand us
Decimals.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
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
    raise NotImplementedError


def raw_band(tier: Tier, ref: Decimal):
    raise NotImplementedError


def round_band(up: Decimal, down: Decimal, tick: Decimal, rounding: str):
    raise NotImplementedError


def compute(tiers, ticker: str, ref: Decimal, tick: Decimal,
            min_price: Optional[Decimal], rounding: str):
    raise NotImplementedError


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
    P = lambda pfx, floor, up, dn: Tier("pct", pfx, D(floor), D(up), D(dn))

    #  Indonesia, verified against Indo_maping_limit_up.csv:
    #  floors 50 / 200 / 5000, coefficients 1.35 / 1.25 / 1.2
    IDN = [P("", "50", "0.35", "0.35"),
           P("", "200", "0.25", "0.25"),
           P("", "5000", "0.20", "0.20")]

    print("bands --self-test\n\npicking the tier")
    check("the bottom tier", select_tier(IDN, "BBCA", D("100")), IDN[0])
    check("a boundary belongs to the tier it opens",
          select_tier(IDN, "BBCA", D("200")), IDN[1])
    check("just under stays below", select_tier(IDN, "BBCA", D("199")),
          IDN[0])
    check("the top tier is open ended",
          select_tier(IDN, "BBCA", D("999999")), IDN[2])
    check("below the lowest floor there is NO tier - Indonesia starts at "
          "50 and a rupiah name under that is not ours to price",
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
    check("an absolute rule adds and subtracts instead - this is how "
          "Japan arrives later with no code change",
          raw_band(Tier("abs", "", D("0"), D("300"), D("300")), D("1234")),
          (D("1534"), D("934")))

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
```

- [ ] **Step 2: Run it to verify the checks fail**

Run: `python Nova/LimitUpDown/bands.py --self-test`
Expected: `NotImplementedError`

- [ ] **Step 3: Implement**

```python
def select_tier(tiers, ticker: str, ref: Decimal):
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


def round_band(up: Decimal, down: Decimal, tick: Decimal, rounding: str):
    if tick <= 0:
        raise BandError("tick is not positive")
    if rounding == "inward":
        return (up / tick).to_integral_value("ROUND_FLOOR") * tick, \
               (down / tick).to_integral_value("ROUND_CEILING") * tick
    if rounding == "outward":
        return (up / tick).to_integral_value("ROUND_CEILING") * tick, \
               (down / tick).to_integral_value("ROUND_FLOOR") * tick
    if rounding == "nearest":
        return (up / tick).to_integral_value(ROUND_HALF_UP) * tick, \
               (down / tick).to_integral_value(ROUND_HALF_UP) * tick
    raise BandError(f"unknown rounding mode {rounding!r}")


def compute(tiers, ticker: str, ref: Decimal, tick: Decimal,
            min_price, rounding: str):
    if ref is None or ref <= 0:
        raise BandError("reference price is not positive")
    tier = select_tier(tiers, ticker, ref)
    if tier is None:
        raise BandError(f"no band tier for price {ref}")
    up, down = raw_band(tier, ref)
    if min_price is not None:
        down = max(down, min_price)          # before rounding - LimitUpDown.r:296
    up, down = round_band(up, down, tick, rounding)
    if not (up > down > 0):
        raise BandError(f"band is not sane: up={up} down={down}")
    return up, down
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `python Nova/LimitUpDown/bands.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/bands.py
git commit -m "feat(luld): tiered, asymmetric band arithmetic with tick rounding"
```

---

### Task 3: Config files and loader (`marketcfg.py` + `config/*.csv`)

**Files:**
- Create: `Nova/LimitUpDown/marketcfg.py`
- Create: `Nova/LimitUpDown/config/markets.csv`
- Create: `Nova/LimitUpDown/config/bands.csv`
- Create: `Nova/LimitUpDown/config/ticks.csv`

**Interfaces:**
- Consumes: `bands.Tier`, `ticks.parse_rows`, `ticks.parse_tsr`
- Produces:
  - `Venue = dataclass(country, venue_id, bbg_venue, bbg_composite, cutoff: time, ref_price, tick_source, min_price: Decimal|None, rounding)`
  - `Config = dataclass(venues: dict[str, Venue], bands: dict[str, list[Tier]], ticks: dict[str, Tiers])`
  - `ConfigError(Exception)`
  - `load(config_dir: Path, tsr_dir: Path) -> Config` — raises `ConfigError`

- [ ] **Step 1: Write the three config files**

`config/markets.csv` — values transcribed from `config_cash.xml`. `KOE-MAIN→KQ` and `KSC-MAIN→KP` are correct as written; do not "fix" them.

```csv
Country,FidessaVenueID,BBGVenueCode,BBGComposite,Time,RefPrice,TickSource,MinPrice,Rounding
Korea,KOE-MAIN,KQ,KS,07:30:00,close_print,config,,inward
Korea,KSC-MAIN,KP,KS,07:30:00,close_print,config,,inward
Malaysia,KLS-MAIN,MK,MK,07:59:00,last_trade,config,,inward
Taiwan,TAI-MAIN,TT,TT,07:59:00,close_print,config,,inward
Indonesia,JKT-MAIN,IJ,IJ,07:59:00,close_print,spol_JKT.tsr,50,inward
China,SHA-MAIN,CG,CH,09:03:00,close_print,config,,inward
China,SHH-MAIN,CG,CH,09:03:00,close_print,config,,inward
China,SSC-MAIN,C1,CH,09:03:00,close_print,config,,inward
China,SZA-MAIN,CS,CH,09:03:00,close_print,config,,inward
China,SHZ-MAIN,CS,CH,09:03:00,close_print,config,,inward
China,SZC-MAIN,C2,CH,09:03:00,close_print,config,,inward
Philippines,PHS-MAIN,PM,PM,09:03:00,close_print,config,,inward
```

`config/bands.csv` — Indonesia's three tiers are the verified contents of `Indo_maping_limit_up.csv` converted from multipliers to fractions (1.35 → 0.35).

```csv
FidessaVenueID,Kind,SymPrefix,FloorFrom,Up,Down
JKT-MAIN,pct,,50,0.35,0.35
JKT-MAIN,pct,,200,0.25,0.25
JKT-MAIN,pct,,5000,0.20,0.20
KOE-MAIN,pct,,0,0.30,0.30
KSC-MAIN,pct,,0,0.30,0.30
KLS-MAIN,pct,,0,0.30,0.30
TAI-MAIN,pct,,0,0.10,0.10
PHS-MAIN,pct,,0,0.30,0.30
SHA-MAIN,pct,688,0,0.20,0.20
SHA-MAIN,pct,,0,0.10,0.10
SHH-MAIN,pct,688,0,0.20,0.20
SHH-MAIN,pct,,0,0.10,0.10
SSC-MAIN,pct,688,0,0.20,0.20
SSC-MAIN,pct,,0,0.10,0.10
SZA-MAIN,pct,300,0,0.20,0.20
SZA-MAIN,pct,,0,0.10,0.10
SHZ-MAIN,pct,300,0,0.20,0.20
SHZ-MAIN,pct,,0,0.10,0.10
SZC-MAIN,pct,300,0,0.20,0.20
SZC-MAIN,pct,,0,0.10,0.10
```

`config/ticks.csv` — **placeholder values that MUST be replaced in Task 9.** Committing them now lets the pipeline be built and tested; shipping them would produce wrong prices.

```csv
FidessaVenueID,FloorFrom,Tick
KOE-MAIN,0,1
KSC-MAIN,0,1
KLS-MAIN,0,0.005
TAI-MAIN,0,0.01
SHA-MAIN,0,0.01
SHH-MAIN,0,0.01
SSC-MAIN,0,0.01
SZA-MAIN,0,0.01
SHZ-MAIN,0,0.01
SZC-MAIN,0,0.01
PHS-MAIN,0,0.0001
```

- [ ] **Step 2: Write `marketcfg.py` with failing self-test checks**

```python
#!/usr/bin/env python3
"""Load and validate the three config files.

Validation is strict and loud.  A venue with no band tiers, a rounding
mode nobody implements, a bands row for a venue that markets.csv has
never heard of - each of those is a config bug that would otherwise
surface as a missing market in a production feed, which is exactly the
failure nobody notices until a trader does.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import bands
import ticks

VALID_ROUNDING = ("inward", "outward", "nearest")
VALID_REFPRICE = ("close_print", "last_trade")
VALID_KIND = ("pct", "abs")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Venue:
    country: str
    venue_id: str
    bbg_venue: str
    bbg_composite: str
    cutoff: time
    ref_price: str
    tick_source: str
    min_price: Optional[Decimal]
    rounding: str


@dataclass(frozen=True)
class Config:
    venues: dict
    bands: dict
    ticks: dict


def _decimal(value: str, what: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ConfigError(f"{what}: {value!r} is not a number")


def load(config_dir: Path, tsr_dir: Path) -> Config:
    raise NotImplementedError


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            fn()
            got = "no exception"
        except ConfigError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    MK = ("Country,FidessaVenueID,BBGVenueCode,BBGComposite,Time,RefPrice,"
          "TickSource,MinPrice,Rounding\n"
          "Indonesia,JKT-MAIN,IJ,IJ,07:59:00,close_print,spol_JKT.tsr,50,"
          "inward\n")
    BD = ("FidessaVenueID,Kind,SymPrefix,FloorFrom,Up,Down\n"
          "JKT-MAIN,pct,,50,0.35,0.35\n")
    TK = "FidessaVenueID,FloorFrom,Tick\n"
    TSR = "SPOL_JKT 0 1\n"

    def write(d, mk=MK, bd=BD, tk=TK, tsr=TSR):
        cfg = Path(d) / "config"
        cfg.mkdir(exist_ok=True)
        (cfg / "markets.csv").write_text(mk, encoding="utf-8")
        (cfg / "bands.csv").write_text(bd, encoding="utf-8")
        (cfg / "ticks.csv").write_text(tk, encoding="utf-8")
        (Path(d) / "spol_JKT.tsr").write_text(tsr, encoding="utf-8")
        return cfg, Path(d)

    print("marketcfg --self-test\n\na good config")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d)
        c = load(cfg, tsrd)
        v = c.venues["JKT-MAIN"]
        check("the venue is keyed by FidessaVenueID, never by BBG code",
              list(c.venues), ["JKT-MAIN"])
        check("cutoff parses to a time", v.cutoff, time(7, 59, 0))
        check("min price is a Decimal", v.min_price, Decimal("50"))
        check("the bbg code is kept as an attribute", v.bbg_venue, "IJ")
        check("one band tier", len(c.bands["JKT-MAIN"]), 1)
        check("the tier carries fractions, not multipliers",
              c.bands["JKT-MAIN"][0].up, Decimal("0.35"))
        check("the .tsr ladder was read for this venue",
              c.ticks["JKT-MAIN"], [(Decimal("0"), Decimal("1"))])

    print("\nblank MinPrice means no floor")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace(",50,inward", ",,inward"))
        check("no minimum", load(cfg, tsrd).venues["JKT-MAIN"].min_price,
              None)

    print("\nconfig that must be refused")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("inward", "sideways"))
        raises("an unimplemented rounding mode", lambda: load(cfg, tsrd),
               "rounding")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("close_print", "crystal_ball"))
        raises("an unknown reference price source",
               lambda: load(cfg, tsrd), "RefPrice")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd=BD.replace("JKT-MAIN", "MARS-MAIN"))
        raises("a band tier for a venue markets.csv does not define",
               lambda: load(cfg, tsrd), "MARS-MAIN")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd="FidessaVenueID,Kind,SymPrefix,FloorFrom,"
                                "Up,Down\n")
        raises("a venue with no band tiers at all",
               lambda: load(cfg, tsrd), "no band tiers")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd=BD.replace("pct", "vibes"))
        raises("an unknown tier kind", lambda: load(cfg, tsrd), "Kind")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK + "Indonesia,JKT-MAIN,IJ,IJ,07:59:00,"
                                     "close_print,config,,inward\n")
        raises("the same venue defined twice", lambda: load(cfg, tsrd),
               "duplicate")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 3: Run it to verify the checks fail**

Run: `cd Nova/LimitUpDown && python marketcfg.py --self-test`
Expected: `NotImplementedError`

- [ ] **Step 4: Implement `load`**

```python
def load(config_dir: Path, tsr_dir: Path) -> Config:
    def rows(name):
        path = Path(config_dir) / name
        if not path.is_file():
            raise ConfigError(f"{path} does not exist")
        with path.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    venues = {}
    for r in rows("markets.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid in venues:
            raise ConfigError(f"markets.csv: duplicate venue {vid}")
        if r["Rounding"].strip() not in VALID_ROUNDING:
            raise ConfigError(
                f"markets.csv {vid}: Rounding {r['Rounding']!r} is not one of "
                f"{VALID_ROUNDING}")
        if r["RefPrice"].strip() not in VALID_REFPRICE:
            raise ConfigError(
                f"markets.csv {vid}: RefPrice {r['RefPrice']!r} is not one of "
                f"{VALID_REFPRICE}")
        raw_min = (r["MinPrice"] or "").strip()
        try:
            hh, mm, ss = (int(x) for x in r["Time"].strip().split(":"))
            cutoff = time(hh, mm, ss)
        except ValueError:
            raise ConfigError(
                f"markets.csv {vid}: Time {r['Time']!r} is not HH:MM:SS")
        venues[vid] = Venue(
            country=r["Country"].strip(),
            venue_id=vid,
            bbg_venue=r["BBGVenueCode"].strip(),
            bbg_composite=r["BBGComposite"].strip(),
            cutoff=cutoff,
            ref_price=r["RefPrice"].strip(),
            tick_source=r["TickSource"].strip(),
            min_price=_decimal(raw_min, f"markets.csv {vid} MinPrice")
                      if raw_min else None,
            rounding=r["Rounding"].strip())
    if not venues:
        raise ConfigError("markets.csv defines no venues")

    band_map = {}
    for r in rows("bands.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid not in venues:
            raise ConfigError(
                f"bands.csv: venue {vid} is not defined in markets.csv")
        kind = r["Kind"].strip()
        if kind not in VALID_KIND:
            raise ConfigError(
                f"bands.csv {vid}: Kind {kind!r} is not one of {VALID_KIND}")
        band_map.setdefault(vid, []).append(bands.Tier(
            kind=kind,
            sym_prefix=r["SymPrefix"].strip(),
            floor_from=_decimal(r["FloorFrom"], f"bands.csv {vid} FloorFrom"),
            up=_decimal(r["Up"], f"bands.csv {vid} Up"),
            down=_decimal(r["Down"], f"bands.csv {vid} Down")))

    tick_rows = {}
    for r in rows("ticks.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid not in venues:
            raise ConfigError(
                f"ticks.csv: venue {vid} is not defined in markets.csv")
        tick_rows.setdefault(vid, []).append(r)

    tick_map = {}
    for vid, v in venues.items():
        if vid not in band_map:
            raise ConfigError(f"{vid} has no band tiers in bands.csv")
        if v.tick_source == "config":
            if vid not in tick_rows:
                raise ConfigError(
                    f"{vid} has TickSource=config but no rows in ticks.csv")
            tick_map[vid] = ticks.parse_rows(tick_rows[vid])
        else:
            path = Path(tsr_dir) / v.tick_source
            if not path.is_file():
                raise ConfigError(f"{vid}: tick file {path} does not exist")
            tick_map[vid] = ticks.parse_tsr(path.read_text(encoding="utf-8"))
        if not tick_map[vid]:
            raise ConfigError(f"{vid}: tick table is empty")

    return Config(venues=venues, bands=band_map, ticks=tick_map)
```

- [ ] **Step 5: Run the self-test to verify it passes**

Run: `cd Nova/LimitUpDown && python marketcfg.py --self-test`
Expected: `all checks passed`

- [ ] **Step 6: Commit**

```bash
git add Nova/LimitUpDown/marketcfg.py Nova/LimitUpDown/config
git commit -m "feat(luld): config files and strict loader keyed on FidessaVenueID"
```

---

### Task 4: Crosscode reader (`crosscode.py`)

**Files:**
- Create: `Nova/LimitUpDown/crosscode.py`

**Interfaces:**
- Consumes: `marketcfg.Venue`
- Produces:
  - `Row = dataclass(ric, bbg, ticker, fidessa_code, venue_id, sec_type)` — `ticker` is `bbg` with the exchange code stripped, used for `SymPrefix` matching
  - `Excluded = dataclass(reason: str, rows: list[str])`
  - `load(path, venues: dict[str, Venue], now: time) -> tuple[list[Row], list[Excluded]]`

- [ ] **Step 1: Write the module with failing self-test checks**

```python
#!/usr/bin/env python3
"""Read CrossCode.csv and cut it down to the rows we will price today.

THREE FILTERS, in this order:

  security type      Equity and ETF only, as LimitUpDown.r:420 does
  configured venue   a FidessaMarket with no row in markets.csv
  cutoff             a venue whose Time has not yet passed

The cutoff is why running this at 07:59 and again at 09:03 produces
different files.  Each run rewrites the WHOLE output with only the venues
that are open, so the 09:03 run republishes Korea and adds China.  That is
existing behaviour (LimitUpDown.r:93-98) and changing it silently would
strand a market.

Nothing is dropped quietly.  Every filter returns what it removed so the
run can report it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

KEEP_TYPES = ("Equity", "ETF")


@dataclass(frozen=True)
class Row:
    ric: str
    bbg: str
    ticker: str
    fidessa_code: str
    venue_id: str
    sec_type: str


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)


def split_bbg(bbg: str):
    """('005930 KP') -> ('005930', 'KP').  Same split as
    CreateTradingDataENT.r:261."""
    raise NotImplementedError


def load(path, venues: dict, now: time):
    raise NotImplementedError


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    from decimal import Decimal
    import marketcfg
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def venue(vid, cutoff):
        return marketcfg.Venue(
            country="X", venue_id=vid, bbg_venue="XX", bbg_composite="XX",
            cutoff=cutoff, ref_price="close_print", tick_source="config",
            min_price=None, rounding="inward")

    V = {"JKT-MAIN": venue("JKT-MAIN", time(7, 59)),
         "SHA-MAIN": venue("SHA-MAIN", time(9, 3))}

    HDR = "RicCode,BloombergCode,FidessaCode,FidessaMarket,Type\n"
    BODY = ("BBCA.JK,BBCA IJ,BBCA.ID,JKT-MAIN,Equity\n"
            "600001.SS,600001 CG,600001.CN,SHA-MAIN,Equity\n"
            "XYZ.JK,XYZ IJ,XYZ.ID,JKT-MAIN,Warrant\n"
            "ABC.KS,ABC KP,ABC.KR,KSC-MAIN,Equity\n")

    print("crosscode --self-test\n\nsplitting the bloomberg code")
    check("ticker and exchange", split_bbg("005930 KP"), ("005930", "KP"))
    check("a ticker with a space in it keeps everything but the last word",
          split_bbg("BRK/A US Equity"), ("BRK/A US", "Equity"))
    check("no space at all", split_bbg("ABC"), ("ABC", ""))

    print("\nfiltering")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + BODY, encoding="utf-8")

        rows, excl = load(p, V, time(10, 0))
        check("both open venues, equities only", sorted(r.ric for r in rows),
              ["600001.SS", "BBCA.JK"])
        check("the ticker is the bbg code without the exchange",
              [r.ticker for r in rows if r.ric == "600001.SS"], ["600001"])
        reasons = {e.reason: e.rows for e in excl}
        check("the warrant is reported, not vanished",
              reasons["security type not Equity/ETF"], ["XYZ.JK"])
        check("so is the venue nobody configured",
              reasons["venue not in markets.csv"], ["ABC.KS"])

        rows, excl = load(p, V, time(8, 30))
        check("at 08:30 Shanghai has not opened yet",
              [r.ric for r in rows], ["BBCA.JK"])
        reasons = {e.reason: e.rows for e in excl}
        check("and says so", reasons["cutoff not reached"], ["600001.SS"])

        rows, excl = load(p, V, time(7, 0))
        check("before every cutoff, nothing is published", rows, [])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify the checks fail**

Run: `cd Nova/LimitUpDown && python crosscode.py --self-test`
Expected: `NotImplementedError`

- [ ] **Step 3: Implement**

```python
def split_bbg(bbg: str):
    parts = (bbg or "").rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def load(path, venues: dict, now: time):
    kept = []
    by_reason = {}

    def drop(reason, ric):
        by_reason.setdefault(reason, []).append(ric)

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            ric = (r.get("RicCode") or "").strip()
            sec_type = (r.get("Type") or "").strip()
            venue_id = (r.get("FidessaMarket") or "").strip()
            if sec_type not in KEEP_TYPES:
                drop("security type not Equity/ETF", ric)
                continue
            v = venues.get(venue_id)
            if v is None:
                drop("venue not in markets.csv", ric)
                continue
            if now < v.cutoff:
                drop("cutoff not reached", ric)
                continue
            bbg = (r.get("BloombergCode") or "").strip()
            ticker, _exch = split_bbg(bbg)
            kept.append(Row(ric=ric, bbg=bbg, ticker=ticker,
                            fidessa_code=(r.get("FidessaCode") or "").strip(),
                            venue_id=venue_id, sec_type=sec_type))

    return kept, [Excluded(reason=k, rows=v) for k, v in sorted(by_reason.items())]
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `cd Nova/LimitUpDown && python crosscode.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/crosscode.py
git commit -m "feat(luld): crosscode reader with type, venue and cutoff filters"
```

---

### Task 5: kdb reference prices (`kdbsource.py`)

**Files:**
- Create: `Nova/LimitUpDown/kdbsource.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `connect(host: str, port: int)` — returns a pykx `SyncQConnection`
  - `close_prices(conn, date, syms: list[str]) -> dict[str, Decimal]`
  - `last_prices(conn, syms: list[str]) -> dict[str, Decimal]`
  - Both accept any object with a `__call__(query, *args)`, so the self-test passes a fake.

- [ ] **Step 1: Write the module with failing self-test checks**

```python
#!/usr/bin/env python3
"""Reference prices out of kdb.  This is the ONLY module that touches kdb,
and the only thing it fetches is a price.

Bloomberg used to answer "what is the limit" directly.  Nothing does now -
the limit is computed from a rule - so all kdb owes us is the number the
rule is struck off:

  close_print.price    the previous session's official closing print
  qatt.lastPrice       the last traded price, for venues struck intraday

target_stock is deliberately NOT used.  Its orgclose/adjclose are cached
values; close_print is the print itself.

ONE ROUND TRIP PER SOURCE, not per symbol.  A universe is tens of
thousands of names and a per-symbol query would take longer than the
window between the cutoff and the open.

pykx is imported inside connect(), so every other module - and both
--self-test and --demo - runs on a machine with no kdb and no q licence.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

CLOSE_Q = ("{[d;s] select last price by sym from close_print "
           "where date=d, sym in s}")
LAST_Q = ("{[s] select last lastPrice by sym from qatt "
          "where sym in s, not null lastPrice}")


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


def _to_decimal(value):
    """kdb hands back numpy floats, pykx atoms or bytes depending on the
    build.  Anything that will not become a positive Decimal is not a
    price and is dropped - the caller reports the symbol as unpriced."""
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    return d if d.is_finite() and d > 0 else None


def _as_map(result, price_col: str):
    raise NotImplementedError


def close_prices(conn, date, syms):
    raise NotImplementedError


def last_prices(conn, syms):
    raise NotImplementedError


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

    class FakeConn:
        """Stands in for pykx.SyncQConnection: called with (query, *args)
        and returns something dict-like keyed by sym."""
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def __call__(self, query, *args):
            self.calls.append((query, args))
            return self.payload

    print("kdbsource --self-test\n\nconverting what kdb returns")
    check("a plain number", _to_decimal(123.45), D("123.45"))
    check("a string", _to_decimal("10"), D("10"))
    check("bytes, as a symbol column can arrive",
          _to_decimal(b"7.5"), D("7.5"))
    check("a null is not a price", _to_decimal(None), None)
    check("nor is zero", _to_decimal(0), None)
    check("nor is a negative", _to_decimal(-1), None)
    check("nor is a nan", _to_decimal(float("nan")), None)
    check("nor is nonsense", _to_decimal("n/a"), None)

    print("\nshaping the result into sym -> price")
    check("a dict of dicts, as a keyed table converts to",
          _as_map({"BBCA": {"price": 8000.0},
                   "TLKM": {"price": 3000.0}}, "price"),
          {"BBCA": D("8000.0"), "TLKM": D("3000.0")})
    check("bytes keys are decoded to str",
          _as_map({b"BBCA": {"price": 1.0}}, "price"), {"BBCA": D("1.0")})
    check("unpriceable rows are left out entirely, not zero filled",
          _as_map({"A": {"price": 0.0}, "B": {"price": 5.0}}, "price"),
          {"B": D("5.0")})
    check("an empty result is an empty map", _as_map({}, "price"), {})

    print("\nthe queries")
    c = FakeConn({"BBCA": {"price": 8000.0}})
    check("close_prices returns the map",
          close_prices(c, "2026-09-01", ["BBCA"]), {"BBCA": D("8000.0")})
    check("and asks close_print exactly once",
          [q for q, _ in c.calls], [CLOSE_Q])
    check("no symbols means no round trip at all",
          close_prices(FakeConn(None), "2026-09-01", []), {})

    c = FakeConn({"ABC": {"lastPrice": 12.5}})
    check("last_prices reads the lastPrice column",
          last_prices(c, ["ABC"]), {"ABC": D("12.5")})
    check("from qatt", [q for q, _ in c.calls], [LAST_Q])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify the checks fail**

Run: `cd Nova/LimitUpDown && python kdbsource.py --self-test`
Expected: `NotImplementedError`

- [ ] **Step 3: Implement the three functions**

```python
def _as_map(result, price_col: str):
    if result is None:
        return {}
    try:
        items = result.items()
    except AttributeError:
        items = dict(result).items()
    out = {}
    for sym, row in items:
        if isinstance(sym, (bytes, bytearray)):
            sym = sym.decode()
        value = row[price_col] if hasattr(row, "__getitem__") else row
        price = _to_decimal(value)
        if price is not None:
            out[str(sym)] = price
    return out


def close_prices(conn, date, syms):
    if not syms:
        return {}
    return _as_map(conn(CLOSE_Q, date, list(syms)), "price")


def last_prices(conn, syms):
    if not syms:
        return {}
    return _as_map(conn(LAST_Q, list(syms)), "lastPrice")
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `cd Nova/LimitUpDown && python kdbsource.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/kdbsource.py
git commit -m "feat(luld): kdb reference prices from close_print and qatt"
```

---

### Task 6: Orchestrator and output (`limit_up_down.py`)

Builds the pipeline and the CSV. Environment copy and email come in Task 7.

**Files:**
- Create: `Nova/LimitUpDown/limit_up_down.py`
- Create: `Nova/LimitUpDown/local_settings.py.example`

**Interfaces:**
- Consumes: `marketcfg.load`, `crosscode.load`, `bands.compute`, `ticks.tick_for`, `kdbsource.*`
- Produces:
  - `OUT_HEADER: list[str]`
  - `price_universe(cfg, rows, refs) -> tuple[list[dict], list[Excluded]]`
  - `dedupe(out_rows) -> tuple[list[dict], list[str]]`
  - `validate(out_rows) -> list[str]` — returns fatal problems, empty means good
  - `write_csv(path, out_rows)`

- [ ] **Step 1: Write the module with failing self-test checks for the pure parts**

```python
#!/usr/bin/env python3
"""Build limitUpDown.csv.

  CrossCode.csv + markets.csv  ->  the universe we owe a price for
  kdb                          ->  a reference price for each name
  bands.csv + tick tables      ->  the band itself
  temp file -> validate -> Test / Pilot / Prod

REPORT, NEVER SILENTLY DROP.  Every name we cannot price leaves the
universe with a reason attached, and the reasons are counted in the run
report.  The R job it replaces filtered unpriceable names out with a
dplyr filter and no one ever saw the list.

NOTHING PARTIALLY PUBLISHED.  The file is written to a temp path and
validated before a single environment is touched, so a bad run leaves
yesterday's file in place rather than half of today's.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import bands
import crosscode
import marketcfg
import ticks

OUT_HEADER = ["#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice",
              "LimitDownPrice", "FidessaCode", "Venue"]


def price_universe(cfg, rows, refs):
    """rows -> output dicts, plus what could not be priced and why."""
    raise NotImplementedError


def dedupe(out_rows):
    """Drop repeated BloombergCodes, keeping the first.  LimitUpDown.r:154
    does the same and emails the list."""
    raise NotImplementedError


def validate(out_rows):
    """Fatal problems only.  Empty list means the file may be published."""
    raise NotImplementedError


def write_csv(path, out_rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_HEADER, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    from datetime import time
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    D = Decimal

    venue = marketcfg.Venue(
        country="Indonesia", venue_id="JKT-MAIN", bbg_venue="IJ",
        bbg_composite="IJ", cutoff=time(7, 59), ref_price="close_print",
        tick_source="spol_JKT.tsr", min_price=D("50"), rounding="inward")
    cfg = marketcfg.Config(
        venues={"JKT-MAIN": venue},
        bands={"JKT-MAIN": [bands.Tier("pct", "", D("50"), D("0.35"),
                                       D("0.35")),
                            bands.Tier("pct", "", D("200"), D("0.25"),
                                       D("0.25"))]},
        ticks={"JKT-MAIN": [(D("0"), D("1"))]})

    def row(ric, bbg, code):
        return crosscode.Row(ric=ric, bbg=bbg, ticker=bbg.rsplit(" ", 1)[0],
                             fidessa_code=code, venue_id="JKT-MAIN",
                             sec_type="Equity")

    print("limit_up_down --self-test\n\npricing the universe")
    rows = [row("BBCA.JK", "BBCA IJ", "BBCA.ID"),
            row("TLKM.JK", "TLKM IJ", "TLKM.ID"),
            row("TINY.JK", "TINY IJ", "TINY.ID"),
            row("NOPX.JK", "NOPX IJ", "NOPX.ID")]
    refs = {"BBCA.JK": D("100"), "TLKM.JK": D("1000"), "TINY.JK": D("10")}

    out, excl = price_universe(cfg, rows, refs)
    check("two names priced", [r["#ReutersCode"] for r in out],
          ["BBCA.JK", "TLKM.JK"])
    check("a 100 rupiah name gets the 35% tier",
          (out[0]["LimitUpPrice"], out[0]["LimitDownPrice"]), ("135", "65"))
    check("a 1000 rupiah name gets the 25% tier",
          (out[1]["LimitUpPrice"], out[1]["LimitDownPrice"]),
          ("1250", "750"))
    check("the venue lands in the output", out[0]["Venue"], "JKT-MAIN")
    reasons = {e.reason: e.rows for e in excl}
    check("a name under every tier is reported",
          reasons["no band tier for price 10"], ["TINY.JK"])
    check("so is a name kdb had no price for",
          reasons["no reference price"], ["NOPX.JK"])

    print("\nduplicate bloomberg codes")
    dup = [{"#ReutersCode": "A.JK", "BloombergCode": "X IJ"},
           {"#ReutersCode": "B.JK", "BloombergCode": "X IJ"},
           {"#ReutersCode": "C.JK", "BloombergCode": "Y IJ"}]
    kept, removed = dedupe(dup)
    check("the first of each code survives",
          [r["#ReutersCode"] for r in kept], ["A.JK", "C.JK"])
    check("and the loser is named", removed, ["B.JK"])

    print("\nvalidating before publication")
    good = [{"#ReutersCode": "A", "BloombergCode": "X IJ",
             "LimitDate": "2026-09-01", "LimitUpPrice": "135",
             "LimitDownPrice": "65", "FidessaCode": "A.ID",
             "Venue": "JKT-MAIN"}]
    check("a good file has nothing to say", validate(good), [])
    check("an empty file is never published",
          validate([]), ["output is empty"])
    bad = [dict(good[0], LimitUpPrice="60")]
    check("an inverted band is fatal",
          validate(bad), ["A: LimitUpPrice 60 <= LimitDownPrice 65"])
    bad = [dict(good[0], LimitDownPrice="-1")]
    check("so is a negative price",
          validate(bad), ["A: LimitDownPrice -1 is not positive"])
    bad = [dict(good[0], LimitUpPrice="")]
    check("so is a blank one",
          validate(bad), ["A: LimitUpPrice '' is not a number"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1
```

- [ ] **Step 2: Run it to verify the checks fail**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: `NotImplementedError`

- [ ] **Step 3: Implement the three pure functions**

```python
def price_universe(cfg, rows, refs):
    out = []
    by_reason = {}
    today = dt.date.today().isoformat()

    def drop(reason, ric):
        by_reason.setdefault(reason, []).append(ric)

    for r in rows:
        venue = cfg.venues[r.venue_id]
        ref = refs.get(r.ric)
        if ref is None:
            drop("no reference price", r.ric)
            continue
        tick = ticks.tick_for(cfg.ticks[r.venue_id], ref)
        if tick is None:
            drop("no tick tier for the reference price", r.ric)
            continue
        try:
            up, down = bands.compute(cfg.bands[r.venue_id], r.ticker, ref,
                                     tick, venue.min_price, venue.rounding)
        except bands.BandError as e:
            drop(e.reason, r.ric)
            continue
        out.append({"#ReutersCode": r.ric,
                    "BloombergCode": r.bbg,
                    "LimitDate": today,
                    "LimitUpPrice": _plain(up),
                    "LimitDownPrice": _plain(down),
                    "FidessaCode": r.fidessa_code,
                    "Venue": r.venue_id})

    return out, [crosscode.Excluded(reason=k, rows=v)
                 for k, v in sorted(by_reason.items())]


def _plain(d: Decimal) -> str:
    """No exponent, no trailing zeros: 1E+3 would be read as text by the
    ATS loader."""
    d = d.normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return format(d, "f")


def dedupe(out_rows):
    seen = set()
    kept, removed = [], []
    for r in out_rows:
        code = r["BloombergCode"]
        if code in seen:
            removed.append(r["#ReutersCode"])
        else:
            seen.add(code)
            kept.append(r)
    return kept, removed


def validate(out_rows):
    problems = []
    if not out_rows:
        return ["output is empty"]
    for r in out_rows:
        ric = r["#ReutersCode"]
        vals = {}
        for col in ("LimitUpPrice", "LimitDownPrice"):
            raw = r.get(col, "")
            try:
                vals[col] = Decimal(raw)
            except Exception:                        # noqa: BLE001
                problems.append(f"{ric}: {col} {raw!r} is not a number")
        if len(vals) < 2:
            continue
        for col, v in vals.items():
            if v <= 0:
                problems.append(f"{ric}: {col} {v} is not positive")
        if vals["LimitUpPrice"] <= vals["LimitDownPrice"]:
            problems.append(
                f"{ric}: LimitUpPrice {vals['LimitUpPrice']} <= "
                f"LimitDownPrice {vals['LimitDownPrice']}")
    return problems
```

- [ ] **Step 4: Run the self-test to verify it passes**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Add the CLI, `--demo`, and `local_settings.py.example`**

```python
#  placeholders: override in local_settings.py beside this file
KDB_HOST = "CHANGEME"
KDB_PORT = 5010
CROSSCODE_PATH = r"CHANGEME\CrossCode.csv"
TSR_DIR = r"CHANGEME"
OUT_TEMP = r"CHANGEME\limitUpDown.csv"
OUT_TEST = r"CHANGEME\Test\limitUpDown.csv"
OUT_PILOT = r"CHANGEME\Pilot\limitUpDown.csv"
OUT_PROD = r"CHANGEME\Prod\limitUpDown.csv"
SMTP_HOST = "CHANGEME"
EMAIL_FROM = "CHANGEME"
EMAIL_TO = []


def _fetch_refs(conn, cfg, rows):
    """One round trip per reference-price source, never per symbol."""
    today = dt.date.today().isoformat()
    wanted = {"close_print": [], "last_trade": []}
    for r in rows:
        wanted[cfg.venues[r.venue_id].ref_price].append(r.ric)
    refs = {}
    refs.update(kdbsource.close_prices(conn, today, wanted["close_print"]))
    refs.update(kdbsource.last_prices(conn, wanted["last_trade"]))
    return refs


def run(args) -> int:
    here = Path(__file__).resolve().parent
    cfg = marketcfg.load(here / "config", Path(TSR_DIR))
    now = dt.datetime.now().time()
    rows, excluded = crosscode.load(CROSSCODE_PATH, cfg.venues, now)

    import kdbsource
    conn = kdbsource.connect(KDB_HOST, KDB_PORT)
    refs = _fetch_refs(conn, cfg, rows)

    out, more = price_universe(cfg, rows, refs)
    excluded += more
    out, dropped = dedupe(out)

    problems = validate(out)
    if problems:
        for p in problems[:20]:
            print(f"  FATAL {p}", file=sys.stderr)
        return 1

    write_csv(OUT_TEMP, out)
    print(f"{len(out)} rows -> {OUT_TEMP}")
    for e in excluded:
        print(f"  excluded {len(e.rows):6d}  {e.reason}")
    if dropped:
        print(f"  deduped  {len(dropped):6d}  duplicate BloombergCode")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("envs", nargs="?", default="",
                   help='pipe separated, e.g. "Test|Pilot|Prod"')
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--demo", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.demo:
        return demo()
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
```

`demo()` builds the same fixture the self-test uses, runs `price_universe`, and writes the CSV to stdout — no kdb, no files:

```python
def demo() -> int:
    import io
    from datetime import time
    venue = marketcfg.Venue(
        country="Indonesia", venue_id="JKT-MAIN", bbg_venue="IJ",
        bbg_composite="IJ", cutoff=time(7, 59), ref_price="close_print",
        tick_source="config", min_price=Decimal("50"), rounding="inward")
    cfg = marketcfg.Config(
        venues={"JKT-MAIN": venue},
        bands={"JKT-MAIN": [bands.Tier("pct", "", Decimal("50"),
                                       Decimal("0.35"), Decimal("0.35"))]},
        ticks={"JKT-MAIN": [(Decimal("0"), Decimal("1"))]})
    rows = [crosscode.Row("BBCA.JK", "BBCA IJ", "BBCA", "BBCA.ID",
                          "JKT-MAIN", "Equity")]
    out, excluded = price_universe(cfg, rows, {"BBCA.JK": Decimal("8000")})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OUT_HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(out)
    print(buf.getvalue(), end="")
    for e in excluded:
        print(f"  excluded {len(e.rows)}  {e.reason}", file=sys.stderr)
    return 0
```

- [ ] **Step 6: Run demo and self-test**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --demo && python limit_up_down.py --self-test`
Expected: a one-row CSV on stdout (`BBCA.JK,BBCA IJ,<today>,10800,5200,BBCA.ID,JKT-MAIN`), then `all checks passed`

- [ ] **Step 7: Commit**

```bash
git add Nova/LimitUpDown/limit_up_down.py Nova/LimitUpDown/local_settings.py.example
git commit -m "feat(luld): orchestrator, output validation and --demo"
```

---

### Task 7: Environment copy and failure email (`mailer.py`)

**Files:**
- Create: `Nova/LimitUpDown/mailer.py`
- Modify: `Nova/LimitUpDown/limit_up_down.py` — add `copy_to_envs`, wire alerts into `run`

**Interfaces:**
- Consumes: `OUT_TEMP`, `OUT_TEST`, `OUT_PILOT`, `OUT_PROD`
- Produces:
  - `mailer.send(subject: str, body: str, host, sender, to: list) -> None`
  - `limit_up_down.parse_envs(spec: str) -> list[str]`
  - `limit_up_down.copy_to_envs(temp, envs: list[str], targets: dict) -> list[str]` — returns failures

- [ ] **Step 1: Add failing self-test checks for `parse_envs` and `copy_to_envs`**

```python
    print("\nparsing the environment argument")
    check("the R job's pipe separated form",
          parse_envs("Test|Pilot|Prod"), ["Test", "Pilot", "Prod"])
    check("one environment", parse_envs("Pilot"), ["Pilot"])
    check("empty means publish nowhere - a dry run",
          parse_envs(""), [])
    check("whitespace and blanks are ignored",
          parse_envs(" Test | | Prod "), ["Test", "Prod"])
    check("an unknown environment is refused, not skipped",
          parse_envs("Test|Staging"), ValueError)

    print("\ncopying to environments")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "temp.csv"
        src.write_text("a,b\n1,2\n", encoding="utf-8")
        targets = {"Test": Path(d) / "t" / "out.csv",
                   "Prod": Path(d) / "p" / "out.csv"}
        check("no failures on a good copy",
              copy_to_envs(src, ["Test", "Prod"], targets), [])
        check("and the content arrived",
              targets["Test"].read_text(encoding="utf-8"), "a,b\n1,2\n")
        check("an environment with no configured target is a failure",
              copy_to_envs(src, ["Pilot"], targets),
              ["Pilot: no output path configured"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: FAIL on the new checks

- [ ] **Step 3: Implement**

```python
VALID_ENVS = ("Test", "Pilot", "Prod")


def parse_envs(spec: str):
    out = [p.strip() for p in (spec or "").split("|") if p.strip()]
    bad = [e for e in out if e not in VALID_ENVS]
    if bad:
        raise ValueError(
            f"unknown environment(s) {bad}; expected any of {VALID_ENVS}")
    return out


def copy_to_envs(temp, envs, targets):
    import shutil
    failures = []
    for env in envs:
        target = targets.get(env)
        if not target:
            failures.append(f"{env}: no output path configured")
            continue
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temp, target)
        except OSError as e:
            failures.append(f"{env}: {e}")
    return failures
```

The self-test's `ValueError` check needs a helper that captures the exception type:

```python
    def check_raises(name, fn, exc):
        nonlocal ok
        try:
            fn()
            got = "no exception"
        except Exception as e:                       # noqa: BLE001
            got = type(e)
        good = got is exc
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {exc!r}"))
```
and the check becomes `check_raises("an unknown environment is refused, not skipped", lambda: parse_envs("Test|Staging"), ValueError)`.

- [ ] **Step 4: Write `mailer.py`**

```python
#!/usr/bin/env python3
"""Failure and exclusion-report email.

Plain text, one message, no templates.  The XML MailConfigurationList the
R job used carried nine named templates; every one of them said "something
went wrong, here is what" and the run already knows how to say that.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


def send(subject: str, body: str, host: str, sender: str, to) -> None:
    if not to:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    with smtplib.SMTP(host) as s:
        s.send_message(msg)
```

- [ ] **Step 5: Wire alerting into `run()`**

Wrap the body of `run` so every fatal path emails and returns non-zero, and a successful run emails the exclusion report only if there was something to report:

```python
def run(args) -> int:
    host_args = (SMTP_HOST, EMAIL_FROM, EMAIL_TO)
    try:
        envs = parse_envs(args.envs)
        here = Path(__file__).resolve().parent
        cfg = marketcfg.load(here / "config", Path(TSR_DIR))
        now = dt.datetime.now().time()
        rows, excluded = crosscode.load(CROSSCODE_PATH, cfg.venues, now)

        import kdbsource
        conn = kdbsource.connect(KDB_HOST, KDB_PORT)
        refs = _fetch_refs(conn, cfg, rows)
    except Exception as e:                           # noqa: BLE001
        mailer.send("LimitUpDown FAILED", f"{type(e).__name__}: {e}",
                    *host_args)
        print(f"FATAL {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    out, more = price_universe(cfg, rows, refs)
    excluded += more
    out, dropped = dedupe(out)

    problems = validate(out)
    if problems:
        body = "Output failed validation, nothing published:\n\n" + \
               "\n".join(problems[:200])
        mailer.send("LimitUpDown FAILED validation", body, *host_args)
        print(body, file=sys.stderr)
        return 1

    try:
        write_csv(OUT_TEMP, out)
    except OSError as e:
        mailer.send("LimitUpDown FAILED to write", str(e), *host_args)
        return 1

    targets = {"Test": OUT_TEST, "Pilot": OUT_PILOT, "Prod": OUT_PROD}
    failures = copy_to_envs(OUT_TEMP, envs, targets)
    if failures:
        mailer.send("LimitUpDown FAILED to publish", "\n".join(failures),
                    *host_args)
        print("\n".join(failures), file=sys.stderr)
        return 1

    report = [f"{len(out)} rows published to {', '.join(envs) or 'nowhere'}"]
    for e in excluded:
        report.append(f"  excluded {len(e.rows):6d}  {e.reason}")
    if dropped:
        report.append(f"  deduped  {len(dropped):6d}  duplicate BloombergCode")
    text = "\n".join(report)
    print(text)
    if excluded or dropped:
        mailer.send(f"LimitUpDown report - {len(out)} rows", text, *host_args)
    return 0
```

Add `import mailer` and the `local_config`-style override at the top of the module:

```python
import mailer

_local = Path(__file__).resolve().parent / "local_settings.py"
if _local.is_file():
    _ns = {}
    exec(compile(_local.read_text(encoding="utf-8"), str(_local), "exec"),
         {}, _ns)
    for _k, _v in _ns.items():
        if not _k.startswith("_"):
            if _k not in globals():
                raise SystemExit(
                    f"{_local} sets {_k}, which is not a setting this script "
                    f"has. A name that does nothing is worse than one that "
                    f"errors.")
            globals()[_k] = _v
```

- [ ] **Step 6: Run the self-test**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: `all checks passed`

- [ ] **Step 7: Commit**

```bash
git add Nova/LimitUpDown/mailer.py Nova/LimitUpDown/limit_up_down.py
git commit -m "feat(luld): environment copy and failure alerting"
```

---

### Task 8: `--compare` cutover instrument

**Files:**
- Modify: `Nova/LimitUpDown/limit_up_down.py`

**Interfaces:**
- Produces: `compare(old_rows: list[dict], new_rows: list[dict]) -> list[str]`

- [ ] **Step 1: Add failing self-test checks**

```python
    print("\ncomparing against the R job's output")
    old = [{"#ReutersCode": "A.JK", "Venue": "JKT-MAIN",
            "LimitUpPrice": "135", "LimitDownPrice": "65"},
           {"#ReutersCode": "B.JK", "Venue": "JKT-MAIN",
            "LimitUpPrice": "200", "LimitDownPrice": "100"}]
    check("identical files have nothing to report", compare(old, old), [])
    check("a name only the old file has",
          compare(old, old[:1]),
          ["JKT-MAIN: 2 old, 1 new", "only in old: B.JK"])
    check("a name only the new file has",
          compare(old[:1], old),
          ["JKT-MAIN: 1 old, 2 new", "only in new: B.JK"])
    new = [dict(old[0], LimitUpPrice="140"), old[1]]
    check("a price that moved",
          compare(old, new),
          ["A.JK LimitUpPrice: old 135, new 140"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: FAIL on the new checks

- [ ] **Step 3: Implement**

```python
def compare(old_rows, new_rows):
    """Differences between the R job's file and ours, worst first: venue
    row counts, names present in one only, then prices that moved."""
    out = []
    old = {r["#ReutersCode"]: r for r in old_rows}
    new = {r["#ReutersCode"]: r for r in new_rows}

    venues = sorted({r["Venue"] for r in old_rows} |
                    {r["Venue"] for r in new_rows})
    for v in venues:
        o = sum(1 for r in old_rows if r["Venue"] == v)
        n = sum(1 for r in new_rows if r["Venue"] == v)
        if o != n:
            out.append(f"{v}: {o} old, {n} new")

    for ric in sorted(set(old) - set(new)):
        out.append(f"only in old: {ric}")
    for ric in sorted(set(new) - set(old)):
        out.append(f"only in new: {ric}")

    for ric in sorted(set(old) & set(new)):
        for col in ("LimitUpPrice", "LimitDownPrice"):
            a, b = old[ric].get(col), new[ric].get(col)
            if a != b and Decimal(a) != Decimal(b):
                out.append(f"{ric} {col}: old {a}, new {b}")
    return out
```

Add the CLI flag and a reader:

```python
    p.add_argument("--compare", metavar="OLD_CSV",
                   help="diff today's output against the R job's file")
```
```python
    if a.compare:
        with open(a.compare, newline="", encoding="utf-8-sig") as fh:
            old_rows = list(csv.DictReader(fh))
        with open(OUT_TEMP, newline="", encoding="utf-8-sig") as fh:
            new_rows = list(csv.DictReader(fh))
        diffs = compare(old_rows, new_rows)
        for d in diffs:
            print(d)
        print(f"\n{len(diffs)} difference(s)")
        return 0
```

- [ ] **Step 4: Run the self-test**

Run: `cd Nova/LimitUpDown && python limit_up_down.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/limit_up_down.py
git commit -m "feat(luld): --compare against the R output for cutover"
```

---

### Task 9: Populate the tick tables — DATA TASK, BLOCKS PRODUCTION

The `config/ticks.csv` committed in Task 3 holds **placeholder single-tier values**. Every one is wrong for a market with a tiered tick ladder, and wrong ticks produce silently wrong prices.

**Files:**
- Modify: `Nova/LimitUpDown/config/ticks.csv`

- [ ] **Step 1: Check whether the ATS already has the tables**

```bash
ls "CHANGEME/" | grep -i tsr
```
`spol_JKT.tsr` is known to exist. If siblings exist for KOE/KSC/KLS/TAI/PHS/SHA/SHH/SSC/SZA/SHZ/SZC, prefer them: set that venue's `TickSource` to the filename in `markets.csv` and delete its `ticks.csv` rows. The ATS file is authoritative and cannot drift from the trading system.

- [ ] **Step 2: For every venue with no `.tsr`, enter the exchange's published tick ladder**

One row per tier, ascending. Example shape (values must be sourced, not copied from here):

```csv
FidessaVenueID,FloorFrom,Tick
TAI-MAIN,0,0.01
TAI-MAIN,10,0.05
TAI-MAIN,50,0.10
TAI-MAIN,100,0.50
TAI-MAIN,500,1.00
TAI-MAIN,1000,5.00
```

- [ ] **Step 3: Have a second person check every ladder against the exchange's own rule page**

This is the step that catches the error `--self-test` cannot: a table that is internally consistent and factually wrong.

- [ ] **Step 4: Verify the loader accepts them**

Run: `cd Nova/LimitUpDown && python marketcfg.py --self-test && python limit_up_down.py --demo`
Expected: `all checks passed`, then a CSV

- [ ] **Step 5: Commit**

```bash
git add Nova/LimitUpDown/config/ticks.csv Nova/LimitUpDown/config/markets.csv
git commit -m "feat(luld): real tick ladders for KR/MY/TW/CN/PH"
```

---

### Task 10: Parallel run and cutover

- [ ] **Step 1: Fill in `local_settings.py` from `config_cash.xml`**

Paths are in the XML, verified: `CrossCode.csv` under `CHANGEME\`; Temp under `CHANGEME\`; Test/Pilot/Prod under `CHANGEME\{env}CHANGEME\`.

- [ ] **Step 2: Verify assumption — RIC join**

Confirm `crosscode.RicCode` values appear as `close_print.sym`. If they do not, a normalisation step is needed before Task 10 can proceed.

```bash
cd Nova/LimitUpDown && python -c "import kdbsource; c=kdbsource.connect(HOST,PORT); print(list(kdbsource.close_prices(c,'2026-09-01',['BBCA.JK'])) )"
```

- [ ] **Step 3: Dry run — produce a file, publish nowhere**

Run: `cd Nova/LimitUpDown && python limit_up_down.py ""`
Expected: a row count, an exclusion report, a file at `OUT_TEMP`, nothing copied.

- [ ] **Step 4: Compare against the R output**

Run: `python limit_up_down.py "" && python limit_up_down.py --compare <path-to-R-limitUpDown.csv>`
Expected: differences confined to Japan, Thailand and India (out of scope) plus names the R job could not price. **Any price mismatch inside the six markets must be explained before proceeding.**

- [ ] **Step 5: Run both jobs daily for one to two weeks, publishing from R**

- [ ] **Step 6: Resolve spec §10.1 (China ST names) before China reaches Prod**

Test and Pilot may proceed without it. Prod may not.

- [ ] **Step 7: Cut over Test, then Pilot, then Prod**

Keep `LimitUpDown.r` runnable throughout.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §3 output contract | 6 (`OUT_HEADER`, `write_csv`), 7 (env copy) |
| §4 components | 1–7 |
| §5.1 markets.csv | 3 |
| §5.2 bands.csv | 3 |
| §5.3 ticks.csv | 3 (placeholder), 9 (real) |
| §5.4 FidessaVenueID key | 3 (loader keys on it, self-test asserts it) |
| §6 data flow | 6 (`run`, `_fetch_refs`) |
| §6 cutoff semantics | 4 |
| §7 band computation | 1, 2 |
| §7 Decimal / ROUND_HALF_UP | 2 |
| §8 error handling | 4 (exclusions), 6 (validate), 7 (email, copy) |
| §9 Kind=abs for Japan | 2 (`raw_band`, self-test covers `abs`) |
| §9 exclusion hook | 4 — sited between the venue and cutoff filters |
| §10.1 China ST | 10 step 6, gated before Prod |
| §10.2 tick tables | 9 |
| §10.3 RIC join assumption | 10 step 2 |
| §11 `--self-test` | every task |
| §11 `--demo` | 6 |
| §11 `--compare` | 8 |
| §11 cutover | 10 |

No spec section is unimplemented.

**Placeholder scan:** the only "placeholder" is `config/ticks.csv` in Task 3, which is deliberate, labelled, and has Task 9 as its resolution with a production gate. `local_settings.py.example` ships `CHANGEME` values by design — that is the pattern `kdb-queries/scripts/lib/local_config.py` documents.

**Type consistency:** `Tier` is defined in Task 2 and used in 3 and 6. `Excluded` is defined in Task 4 and reused by Task 6's `price_universe`. `Tiers` (list of `(Decimal, Decimal)`) is produced by Task 1 and consumed in 3 and 6. `Venue` and `Config` are defined in Task 3 and consumed in 4 and 6. `venue_id` is the field name throughout; `FidessaVenueID` appears only as a CSV column header. Reference prices are `Decimal` from `kdbsource` through `bands.compute` to `_plain()` at the CSV boundary.
