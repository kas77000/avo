# LimitUpDown — how the Python version works

How `Nova/LimitUpDown` builds `limitUpDown.csv`, the daily price-band file the
Nova ATS uses to bound orders.

For *why* it is built this way, see
[`superpowers/specs/2026-09-01-limit-up-down-python-design.md`](superpowers/specs/2026-09-01-limit-up-down-python-design.md).
This document is the mechanism.

---

## Entry points

```
python limit_up_down.py "Test|Pilot|Prod"   real run, publish
python limit_up_down.py ""                  real run, publish nowhere (dry run)
python limit_up_down.py --demo              whole pipeline, canned data, no kdb
python limit_up_down.py --self-test         arithmetic only
python limit_up_down.py --compare OLD.csv   diff the last output against R's
```

Only the first two touch kdb or the network shares. `--demo` and `--self-test`
run on a machine with no kdb, no pykx, no q licence and no network shares,
which is what makes the market rules reviewable on a laptop.

Before a real run, `_apply_local_settings()` reads `local_settings.py` beside
the script and overwrites the module-level `CHANGEME` constants. It is
**strict**: a name in that file the script does not define is a hard error, so
`EMAIL_T0` with a zero fails loudly instead of quietly mailing nobody.

---

## The pipeline

```
  config/markets.csv ─┐
  config/bands.csv ───┤  marketcfg.load()      ── validate, or refuse to start
  config/*.tsr ───────┘        │
                               ▼
                     Config(venues, bands, ticks)
                               │
  CrossCode.csv ───────────────┤  crosscode.load(path, venues, now)
                               │     filter: Type in {Equity, ETF}
                               │     filter: FidessaMarket is configured
                               │     filter: venue cutoff has passed
                               ▼
                        [Row, ...]  +  [Excluded, ...]
                               │
  kdb ─────────────────────────┤  _fetch_refs()
     close_print.price         │     one query per source, NOT per symbol
     qatt.lastPrice            │
                               ▼
                        {ric: Decimal, ...}
                               │
                               │  price_universe()
                               │     ticks.tick_for()   (only if rounding != none)
                               │     bands.compute()
                               ▼
                        [7-column dicts]  +  more [Excluded, ...]
                               │
                        dedupe()      duplicate BloombergCode, first wins
                        validate()    ── fatal problems stop everything here
                               │
                        write_csv(OUT_TEMP)
                               │
                        copy_to_envs() ──> Test / Pilot / Prod
                               │
                        report to stdout + email
```

---

## Stage 1 — Config load (`marketcfg.load`)

Reads `markets.csv` into `Venue` objects **keyed by `FidessaVenueID`**, then
`bands.csv` into a list of `Tier`s per venue.

It refuses to start on any of:

- an unknown `Rounding` or `RefPrice`
- a `Time` that is not `HH:MM:SS`
- a duplicate venue
- a `bands.csv` row naming a venue `markets.csv` never defined
- a venue with no band tiers
- `Rounding=none` with a `TickSource` set, or a rounding mode with it blank

Tick tables load **only for venues that round**. `ticks.csv` is not even opened
unless a venue names `config` as its `TickSource` — none does today, so the only
ladder in the system is Indonesia's `spol_JKT.tsr`.

### Why `FidessaVenueID` and not the Bloomberg exchange code

`BBGVenueCode` is not unique. China's `CG` maps to both `SHA-MAIN` and
`SHH-MAIN`; `CS` maps to both `SZA-MAIN` and `SHZ-MAIN`. `FidessaVenueID` is
unique, is on every crosscode row, and is what lands in the output `Venue`
column.

`KOE-MAIN → KQ` and `KSC-MAIN → KP` look swapped and are not — verified against
`config_cash.xml`.

---

## Stage 2 — Universe (`crosscode.load`)

Reads `CrossCode.csv` and applies three filters, in order:

| Filter | Drops |
|---|---|
| `Type in {Equity, ETF}` | warrants, rights, bonds |
| `FidessaMarket` is in `markets.csv` | every venue outside the six markets |
| `now >= venue.cutoff` | markets whose data is not ready yet |

It also computes `ticker` — the Bloomberg code minus its last word
(`600001 CG` → `600001`) — which is what `SymPrefix` matches against later.
Same split as `CreateTradingDataENT.r:261`.

### The cutoff is cumulative by time of day

Each run rewrites the **entire** file with only the venues whose `Time` has
passed:

```
run at 07:59  ->  KR, MY, TW, ID           (China absent from the file)
run at 09:03  ->  KR, MY, TW, ID, CN, PH   (republishes the first four)
```

Inherited from `LimitUpDown.r:93-98` and kept deliberately. Changing it
silently would strand a market.

---

## Stage 3 — Reference prices (`_fetch_refs`, `kdbsource`)

Rows are bucketed by their venue's `RefPrice`, then **two queries total**,
regardless of universe size:

```q
close_print:  {[d;s] select last price by sym from close_print where date=d, sym in s}
qatt:         {[s] select last lastPrice by sym from qatt where sym in s, not null lastPrice}
```

`_as_map` converts whatever pykx returns into `{sym: Decimal}` and drops
anything that is not a positive finite number — nulls, zeros, negatives, NaN.
A dropped symbol is simply absent from the map and the next stage reports it as
unpriced. Nothing is zero-filled.

`target_stock` is deliberately **not** used: its `orgclose`/`adjclose` are
cached values, while `close_print` is the print itself.

`pykx` is imported *inside* `connect()`, which is what lets every other mode run
without it.

---

## Stage 4 — Pricing (`price_universe` → `bands.compute`)

Per row:

```
ref = refs.get(ric)                        none? -> drop "no reference price"
tick = tick_for(...) if rounding != none   none? -> drop "no tick tier"

select_tier:   filter tiers to the LONGEST matching SymPrefix,
               then take the greatest FloorFrom <= ref
raw_band:      pct -> ref*(1+up), ref*(1-down)
               abs -> ref+up, ref-down            (Japan later)
min_price:     down = max(down, min_price)        BEFORE rounding
round_band:    none -> unchanged;  inward / outward / nearest -> to tick
assert         up > down > 0
```

Prefix filtering happens **before** the floor lookup: a STAR name must walk
STAR's own ladder rather than fall back onto the main board's.

Worked examples:

```
600001.SS  ref 12.34  SHA-MAIN  prefix ''    ->  ±10%  ->  13.574 / 11.106  (none)
688001.SS  ref 12.34  SHA-MAIN  prefix '688' ->  ±20%  ->  14.808 / 9.872   (none)
BBCA.JK    ref 8000   JKT-MAIN  tier ≥5000   ->  ±20%  ->  9600 / 6400
                                                  tick 25, inward, both exact
```

`_plain()` formats each price with no exponent and no trailing zeros — `1E+3`
would be read as text by the ATS loader.

### Decimal, never float

`floor(1.15 / 0.05)` is 23 in decimal and 22 in binary float. Tick rounding is
exactly where that bites, so every quantity in this path is a `Decimal`.
`nearest` uses `ROUND_HALF_UP`, not Python's default banker's rounding.

### Most markets do not round

`Rounding=none` is the normal setting. Only Indonesia rounds, which is what the
R job does — tick rounding appears once in 516 lines of R, for the one market
the R job computed rather than read from Bloomberg.

Rounding inward never changed which orders the band admits anyway:
`floor(raw/tick)*tick` is the largest valid tick price ≤ `raw`, so an order on a
tick passes the rounded band exactly when it passes the raw one. Korea at 71,300
with 100 KRW ticks: raw limit up 92,690, rounded 92,600, and the only ticks
nearby are 92,600 and 92,700 — nothing falls between them. Rounding makes the
published number match what the exchange prints; it does not make the check
stricter.

---

## Stage 5 — Publish

`dedupe()` keeps the first of each repeated `BloombergCode`, as
`LimitUpDown.r:154` does.

`validate()` returns fatal problems: an empty file, a non-numeric price, a
non-positive price, an inverted band.

Then **write to temp, validate, and only then copy**. A bad run leaves
yesterday's file in place in Test/Pilot/Prod rather than half of today's.

---

## Error handling — two tiers

**Row-level exclusions** do not stop the run. Each carries a reason, and every
one is counted, printed and emailed:

```
1234 rows -> CHANGEME
published to Test, Pilot
  excluded     18  no reference price
  excluded      3  no band tier for price 45
  excluded    920  cutoff not reached
  deduped       2  duplicate BloombergCode
```

The R job filtered unpriceable names out with a `dplyr::filter` and nobody ever
saw the list.

**Fatal errors** publish nothing and exit non-zero: kdb unreachable, crosscode
missing, config invalid, validation failed, write or copy failed. Each sends
mail.

Partial coverage is normal and pre-existing — the R job already drops any name
it cannot price (`LimitUpDown.r:260`), so Nova tolerates missing rows. A market
excluded for a data problem is safe; a *wrong* row is not.

---

## Why the modules are split this way

| Module | Knows about |
|---|---|
| `bands.py` | numbers only |
| `ticks.py` | numbers only |
| `marketcfg.py` | that the config is CSV |
| `crosscode.py` | that the universe is a CSV |
| `kdbsource.py` | that kdb exists — the only one |
| `mailer.py` | SMTP |
| `limit_up_down.py` | all of the above, and nothing else does |

The dependency direction is strictly one way, and the two pure modules depend
on nothing. That is why 24 band checks run in milliseconds with no database,
and why a market rule can be reviewed by someone who cannot reach kdb.

Every module carries its own `--self-test`:

```
python ticks.py --self-test        python crosscode.py --self-test
python bands.py --self-test        python kdbsource.py --self-test
python marketcfg.py --self-test    python mailer.py --self-test
```

---

## Outstanding before Prod

1. **China ST / \*ST names get ±10% instead of ±5%.** ST status is not
   derivable from the ticker and we have no source for the flag. The error is
   in the dangerous direction — too *wide* means Nova accepts an order the
   exchange rejects. See §10.1 of the spec.
2. **Parallel run.** Run both jobs daily and reconcile with `--compare` before
   switching Test, then Pilot, then Prod.
3. **Verify the RIC join.** `crosscode.RicCode` is assumed to match
   `close_print.sym`. If it does not, a normalisation step is needed.
