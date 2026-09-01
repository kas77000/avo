# Limit Up/Down feed — Python rewrite

**Date:** 2026-09-01
**Status:** implemented; tasks 1-8 of the plan complete, parallel run outstanding
**Replaces:** `Nova/LimitUpDown/LimitUpDown.r`

---

## 1. Why

`LimitUpDown.r` produces `limitUpDown.csv`, the daily price-band file the Nova ATS
uses to bound orders. It has three problems:

1. **It depends on Bloomberg.** `R_bdp` supplies `PX_MAX_LIMIT` / `PX_MIN_LIMIT`
   for most markets. That is a paid, rate-limited, failure-prone dependency for
   data that six of our markets publish as an arithmetic rule off a reference
   price.
2. **The rules that already exist are hidden.** Indonesia is computed from a
   tier table (`Indo_maping_limit_up.csv`) and a tick file (`spol_JKT.tsr`) —
   the right design — but it is written as a special case inside a function
   named for something else, and no other market can use it.
3. **It is R**, which nothing else in the surrounding toolchain is.

The rewrite computes every band from rules, uses kdb for one thing only (the
reference price), and makes each market a row in a config file rather than a
branch in the code.

### The insight this rests on

Every market in scope caps the daily move as a percentage of a reference price.
Indonesia's existing implementation is already the correct shape:

```
band = f(reference price, tier table)
```

Generalising that one function to all six markets removes Bloomberg entirely.
Indonesia additionally rounds the result to a tick; that part stays specific
to Indonesia (§5.3).

---

## 2. Scope

**In:** Indonesia, Malaysia, Korea, Philippines, China, Taiwan.

**Out for now, by config later:** Japan, Thailand, India. These are in
`config_cash.xml` today and will lose their rows in `limitUpDown.csv` when the
Python job takes over. The design must make re-adding them a config change, not
a code change (§9).

**Not a goal:** changing the output contract, changing Nova, or touching
`CreateTradingDataENT.r`.

---

## 3. Output contract — unchanged

Seven columns, one row per instrument per venue:

```
#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue
3BBLACKB.BO,3BBLACKB IB,2026-08-28,1625.7,1083.8,3BBLACKB.IN,BSE-MAIN
```

Written to a temp path, validated, then copied to whichever of Test / Pilot /
Prod the run was asked for. Same command-line contract as the R job:

```
python limit_up_down.py "Test|Pilot|Prod"
```

**Partial coverage is normal and pre-existing.** The R job already drops any
name it cannot price (`LimitUpDown.r:260`), so Nova tolerates missing rows. A
market excluded for a data problem is therefore safe; a *wrong* row is not.

---

## 4. Components

```
Nova/LimitUpDown/
  limit_up_down.py     args, orchestration, validation, env copy
  bands.py             tier lookup + band maths + tick rounding   <- pure
  ticks.py             tick tier lookup, for the markets that round <- pure
  marketcfg.py         load and validate the config files
  crosscode.py         read and filter CrossCode.csv
  kdbsource.py         reference price from kdb (pykx, lazy import)
  mailer.py            failure alerts
  config/markets.csv
  config/bands.csv
  local_settings.py    gitignored: kdb host:port, paths, SMTP, recipients
  LimitUpDown.r        retained, unrun, as the cutover reference
```

`bands.py` and `ticks.py` take numbers and return numbers — no kdb, no files,
no clock. That is what makes every market rule testable on a laptop with no
database and no q licence.

---

## 5. Configuration

Two CSVs, replacing `config_cash.xml` and generalising
`Indo_maping_limit_up.csv`. CSV so the desk can edit them in Excel, which is
how `Indo_maping_limit_up.csv` is maintained today. A third, `ticks.csv`, is
read only if some venue asks for it — none does today.

### 5.1 `markets.csv` — one row per venue

```csv
Country,FidessaVenueID,BBGVenueCode,BBGComposite,Time,RefPrice,TickSource,MinPrice,Rounding
Korea,KOE-MAIN,KQ,KS,07:30:00,close_print,,,none
Korea,KSC-MAIN,KP,KS,07:30:00,close_print,,,none
Malaysia,KLS-MAIN,MK,MK,07:59:00,last_trade,,,none
Taiwan,TAI-MAIN,TT,TT,07:59:00,close_print,,,none
Indonesia,JKT-MAIN,IJ,IJ,07:59:00,close_print,spol_JKT.tsr,50,inward
China,SHA-MAIN,CG,CH,09:03:00,close_print,,,none
China,SHH-MAIN,CG,CH,09:03:00,close_print,,,none
China,SSC-MAIN,C1,CH,09:03:00,close_print,,,none
China,SZA-MAIN,CS,CH,09:03:00,close_print,,,none
China,SHZ-MAIN,CS,CH,09:03:00,close_print,,,none
China,SZC-MAIN,C2,CH,09:03:00,close_print,,,none
Philippines,PHS-MAIN,PM,PM,09:03:00,close_print,,,none
```

| Column | Meaning |
|---|---|
| `FidessaVenueID` | **The key.** Matches `crosscode.FidessaMarket` and the output `Venue`. |
| `BBGVenueCode` | Bloomberg exchange code. Attribute only — see §5.4. |
| `BBGComposite` | Bloomberg composite code. Carried from the XML; not used in logic. |
| `Time` | Cutoff. The venue is published only once this local time has passed. |
| `RefPrice` | `close_print` or `last_trade`. |
| `TickSource` | A `.tsr` filename, or `config` meaning rows in `ticks.csv`. **Blank when `Rounding=none`.** |
| `MinPrice` | Floor applied to the down limit before rounding. Blank = none. |
| `Rounding` | `none` (the usual) \| `inward` \| `outward` \| `nearest`. |

### 5.2 `bands.csv` — one row per venue per tier

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
SZA-MAIN,pct,300,0,0.20,0.20
SZA-MAIN,pct,,0,0.10,0.10
```

| Column | Meaning |
|---|---|
| `Kind` | `pct` — `Up`/`Down` are fractions of the reference price. `abs` — they are absolute amounts (for Japan later). |
| `SymPrefix` | Optional ticker prefix. Longest match wins; blank is the venue default. Distinguishes STAR (688) and ChiNext (300). |
| `FloorFrom` | Lower bound of the tier, on the **reference price**. |
| `Up` / `Down` | Independent, so an asymmetric market is expressible. |

Percentages are stored as decimals, **not** as the R file's multiplier. `0.35`
reads as ±35%; the old `1.35` required knowing the convention.

**Indonesia's first tier starts at 50, not 0.** A name below Rp 50 matches no
tier. Today that silently yields `NA` and the row is dropped at
`LimitUpDown.r:327`. The new job excludes it *and reports it*.

### 5.3 Rounding is optional, and off for most markets

`Rounding=none` needs no tick table at all. The band is `ref × (1 ± pct)` and
that number is published as computed.

**Only Indonesia rounds**, exactly as `LimitUpDown.r:323` does — because
Indonesia is the one market the R job *computed* rather than read from
Bloomberg. Every other market's limits arrived already struck by the exchange
and were published unrounded. Japan will round when it arrives.

Two reasons this is right, not a shortcut:

1. **It matches the job being replaced.** Tick rounding appears once in 516
   lines of R, for one market.
2. **Inward rounding does not change which orders the band admits.**
   `floor(raw/tick)*tick` is the largest valid tick price ≤ `raw`, so for any
   order price `m` that is itself on a tick, `m ≤ rounded` exactly when
   `m ≤ raw`. Korea at 71,300 with 100 KRW ticks: raw limit up 92,690, rounded
   92,600, and the only ticks nearby are 92,600 and 92,700 — nothing falls
   between them. Rounding makes the published number match what the exchange
   prints; it does not make the check stricter.

A venue that rounds names a `TickSource` (a `.tsr` filename, or `config` plus a
`ticks.csv` of `FidessaVenueID,FloorFrom,Tick` rows). A venue that does not must
leave it blank. Both mismatches are config errors, not silent defaults.

Indonesia reads `spol_JKT.tsr` from the ATS, so its ladder cannot drift from
the trading system.

### 5.4 Why `FidessaVenueID` and not the Bloomberg exchange code

`BBGVenueCode` is **not unique**. From `config_cash.xml`:

| FidessaVenueID | BBGVenueCode |
|---|---|
| SHA-MAIN | CG |
| SHH-MAIN | **CG** |
| SSC-MAIN | C1 |
| SZA-MAIN | CS |
| SHZ-MAIN | **CS** |
| SZC-MAIN | C2 |

`CG` and `CS` each map to two venues, so a rule keyed on the Bloomberg code
could not address them separately. `FidessaVenueID` is unique, is already on
every crosscode row, and is what lands in the output `Venue` column.

Also recorded because it is counterintuitive and was verified against the XML:
**`KOE-MAIN` → `KQ`** and **`KSC-MAIN` → `KP`**.

---

## 6. Data flow

```
markets.csv ──┐
bands.csv ────┼─> validated config
ticks.csv ────┘        │
                       │  in-scope venues, cutoffs passed
CrossCode.csv ─────────┤  filter Type in {Equity, ETF}
                       │  filter FidessaMarket in configured venues
                       ▼
                   universe          <── exclusion hook (§9) goes here
                       │
kdb ───────────────────┤  close_print.price   (previous official close)
                       │  qatt.lastPrice      (last trade)
                       ▼
              reference price per sym
                       │
                 bands.compute()
                 ticks.tick()
                       ▼
              validate → temp CSV → Test / Pilot / Prod
```

One kdb round trip per reference-price source, not per symbol. kdb is used for
the reference price and nothing else — `target_stock` is not queried.

### Cutoff semantics — preserved exactly

Each run rewrites the **whole** file with only the venues whose `Time` has
passed. An 07:59 run publishes KR/MY/TW/ID; the 09:03 run republishes those and
adds CN/PH. This is cumulative-by-time-of-day and is existing behaviour
(`LimitUpDown.r:93-98`). It must not change by accident.

---

## 7. Band computation

Per universe row:

```
ref = reference price for sym, from the venue's RefPrice source
if ref is null or ref <= 0:                  exclude, report

rows = bands rows for this venue
rows = rows whose SymPrefix is blank or is a prefix of the ticker
rows = keep only those with the LONGEST matching SymPrefix   # prefix first,
                                                             # then floor
tier = the remaining row with the greatest FloorFrom <= ref
if no tier:                                  exclude, report   # e.g. IDR < 50

if tier.Kind == 'pct':
    raw_up   = ref * (1 + tier.Up)
    raw_down = ref * (1 - tier.Down)
else:                                        # abs
    raw_up   = ref + tier.Up
    raw_down = ref - tier.Down

if MinPrice:  raw_down = max(raw_down, MinPrice)      # before rounding

if Rounding == 'none':                       publish raw_up, raw_down
                                             # no tick table is consulted

tick = ticks tier with the greatest FloorFrom <= ref  # keyed on ref, not on
                                                      # the limit price
if no tick:                                  exclude, report

inward:   up = floor(raw_up / tick) * tick   down = ceil(raw_down / tick) * tick
outward:  up = ceil (raw_up / tick) * tick   down = floor(raw_down / tick) * tick
nearest:  both rounded to nearest, ties away from zero (ROUND_HALF_UP —
          not Python's default banker's rounding)

assert up > down > 0
```

Two details carried deliberately from the R implementation:

- The **tick is chosen from the reference price**, not from the limit being
  rounded (`LimitUpDown.r:315`).
- **`MinPrice` is applied before rounding**, not after (`LimitUpDown.r:296`
  precedes `:323`).

**Arithmetic must use `decimal.Decimal`, not float.** Tick rounding on binary
floats produces off-by-one-tick errors — `floor(1.15 / 0.05)` is not reliably
23. Every price in this path is a decimal quantity and is treated as one.

---

## 8. Error handling

Principle, inherited from the R job's `WrongLimitBBG` alert: **report, never
silently drop.** Nothing is partially published — the CSV is written to temp,
validated (row count plausible, no NaN, no negative or inverted bands), and
only then copied to the environments.

| Condition | Action |
|---|---|
| kdb unreachable | email, exit non-zero, write nothing |
| `CrossCode.csv` missing or empty | email, exit non-zero, write nothing |
| config fails validation | email, exit non-zero, write nothing |
| symbol has no reference price | exclude row, list in report |
| reference price <= 0 | exclude row, list in report |
| price matches no band tier | exclude row, list in report |
| venue has no usable tick table | exclude **venue**, list in report |
| `FidessaMarket` not in config | exclude row, list — this is how a new market surfaces |
| duplicate Bloomberg code | de-duplicate as `LimitUpDown.r:154` does, list in report |
| computed band inverted or negative | assertion failure, abort the run |
| write or env copy fails | email, exit non-zero, leave the previous file in place |

Alerting reuses one `mailer.py`; the XML mail templates are not carried over.

---

## 9. Designed-for extensions (not built now)

| Extension | What it needs | Code change? |
|---|---|---|
| **Thailand** | one `bands.csv` row (`SET-MAIN,pct,,0,0.30,0.30`) + one `markets.csv` row | none |
| **Japan** | ~33 `bands.csv` rows with `Kind=abs` from the exchange step table | none — this is why `Kind` exists |
| **India** | the static-limit exclusion below | one loader |
| **Static-limit exclusion** | India's `in-nse_drv.stra` / `in-bse_drv.stra` list names whose limits are configured in the ATS strategy file; those names must be excluded from the feed. Space-delimited, 8-line preamble, field `V2`, matched against `Mnemo` with the first two characters stripped (`LimitUpDown.r:112-152`). | one loader + an optional `ExcludeFile` column, hooked at the marked point in §6 |

---

## 10. Open issues

### 10.1 China ST / \*ST names get too wide a band — UNRESOLVED

Shanghai and Shenzhen cap **ST** and **\*ST** names at **±5%**, against ±10%
for the main board. ST status is a *flag on the listing*, not something
derivable from the ticker — unlike STAR (`688…`) and ChiNext (`300…`), which
`SymPrefix` handles.

With no source for the flag, an ST name will be published at **±10% instead of
±5%**.

**The error is in the dangerous direction.** A band that is too *wide* means
Nova accepts an order the exchange will reject. Too narrow would merely be
conservative.

Options, none yet chosen:

1. **Source the flag.** If any accessible system carries an ST marker
   (crosscode field, kdb, a downloadable exchange list), add it and give ST its
   own `bands.csv` rows. Correct, and cost depends entirely on availability.
2. **Manual exclusion list.** A desk-maintained file of ST tickers, excluded
   from the feed so Nova has no band for them rather than a wrong one. Cheap,
   correct-by-omission, but goes stale silently — ST status changes on a
   schedule nobody here watches.
3. **Exclude China until resolved.** Safest, but loses six venues.
4. **Accept knowingly.** Publish ±10% and document the exposure. Only
   defensible if the desk confirms ST names are not traded through Nova.

**This must be decided before China goes to Prod.** Test and Pilot can proceed.
Recommendation: (1) if a flag can be found within the implementation window,
otherwise (2) with a scheduled review, and (4) only with explicit desk sign-off.

### 10.2 ~~Tick tables for five markets~~ — RESOLVED, no longer an issue

Superseded by §5.3. KR / MY / TW / CN / PH use `Rounding=none` and need no
tick table, matching what `LimitUpDown.r` publishes for them today. The only
tick ladder in the system is Indonesia's, and it already exists on the ATS
share.

### 10.3 Assumptions to verify during implementation

1. **`crosscode.RicCode` joins to kdb `close_print.sym` / `qatt.sym`.** The
   `.HK` / `.AU` / `.SP` patterns in `limit_up_down_v2.q` say `sym` is
   RIC-shaped, but the crosscode side has not been confirmed to match exactly.
   If it does not, the join needs a normalisation step.
2. **`close_print` holds the previous session's official close** for these
   markets on the run date, and is populated before the earliest cutoff
   (07:30 for Korea).
3. **Malaysia genuinely wants `last_trade`** rather than the previous close.

---

## 11. Testing

Three modes, following the conventions in the sibling `kdb-queries` scripts:

- **`--self-test`** — `bands.py` and `ticks.py` against golden cases: Indonesian
  tier boundaries (49/50/51, 199/200/201, 4999/5000/5001), the Rp 50 floor,
  rounding direction at an exact tick multiple, `SymPrefix` longest-match, zero
  and negative reference prices, unknown venue, `Kind=abs`. No kdb, no files.
- **`--demo`** — a synthetic crosscode and canned kdb rows through the entire
  pipeline to a CSV on stdout. Runs with no pykx and no q licence.
- **`--compare <old.csv>`** — diff against the R job's output: per-venue row
  counts, symbols present in one and not the other, and price mismatches beyond
  a tick. This is the cutover instrument.

### Cutover

1. Implement, `--self-test` and `--demo` green.
2. Run both jobs daily for one to two weeks, comparing with `--compare`,
   publishing from R.
4. Resolve §10.1 before China reaches Prod.
5. Switch Test, then Pilot, then Prod. Keep `LimitUpDown.r` runnable throughout.
