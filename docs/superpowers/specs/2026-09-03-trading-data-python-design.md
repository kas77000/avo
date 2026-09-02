# TradingData in Python — design

**Date:** 2026-09-03
**Status:** approved
**Replaces:** nothing. This is the first Python for TradingData.
**Source of truth for behaviour:** `no_git/CreateTradingDataENT.r` (676 lines,
hand-transcribed from photographs — see `docs/trading-data-handoff.md` for
provenance and why any surprising character in it is suspect).

## Goal

Produce `TradingData.csv` — the instrument reference file the Nova ATS loads —
from kdb instead of Bloomberg, for the fields we can source with confidence, and
leave the rest blank rather than guess.

The six fields that matter most, in the user's words: **Close, Beta, Volatility,
Index info, MarketStatus, MarketCap.**

### Non-goals

- Replacing the R job. This runs alongside it until `--compare` says it agrees.
- Japan-specific handling. Out of scope, as it was for LimitUpDown.
- `qatt.cond` as a substitute for `TRADING_CONDITIONS_1`. Explicitly ruled out.
- Any use of `target_stock`. Explicitly ruled out, and circular besides — it is
  populated *from* `TradingData.csv`.

## The central discovery

`equity_master` is not a kdb-native table. It is the Bloomberg Data Licence feed
already loaded into kdb — the same feed the R job reads off disk as
`EquitiesDataLicence.rds`. Two pieces of evidence:

1. It carries `TICKER_AND_EXCH_CODE`, the exact key the R job joins the RDS on
   at `:90`.
2. Of the 16 columns the R job explicitly drops from the RDS at `:84-88`, 13 are
   present (4 under an `EQY_` prefix). Only the three yield fields are absent.

This matters because it reverses the earlier conclusion that removing Bloomberg
was "a reference-data sourcing project, not a script rewrite". Somebody has
already done the sourcing project. `equity_master` replaces the RDS file *and*
the `R_bdp` calls together.

## Guiding principle

**Every reference file is optional. Present means used; absent means the columns
it feeds go blank and the run says so.**

This turns "fill it if you can, otherwise leave it blank" from a per-column
judgement into a structural rule. A desk that later supplies `msci_mapping.csv`
gets the `Msci*` columns with no code change.

The corollary, inherited from LimitUpDown: **report, never silently drop.** Every
excluded row carries a reason and is counted, printed and emailed. The R job
filtered rows away invisibly; this one does not.

## Inputs

| Input | Required | Feeds |
|---|---|---|
| `CrossCode.csv` | yes | the row set |
| `equity_master` (kdb) | yes | the six fields, plus ISIN and Sector |
| `msci_mapping.csv` | no | the four `Msci*` columns |
| `OpenAuctionAggressiveLevel.csv` | no | `OpenAggressivityPct` |
| HKEX dico list | no | `Segment` for HKG-MAIN / HKG-GEM |
| India NSE / BSE CAS lists | no | `Segment` for NSI-MAIN / BSE-MAIN |
| `config/markets.csv` (ours) | yes | the short-sell market lists |

### CrossCode.csv

Seven columns, exactly those the R job selects at `:526`:

```
#FidessaCode, RicCode, Type, BloombergCode, BloombergSecurityType,
FidessaMarket, Currency
```

`LimitUpDown/v1/crosscode.py` already reads five of these. The reader here needs
the two extra (`BloombergSecurityType`, `Currency`) and must accept the leading
`#` on the first header cell, which `fread` preserves and `csv.DictReader` will
too.

`BloombergSecurityType == "REIT"` sets the `IsREIT` flag used by
`RespectShortSellPrice` (`:308`).

### equity_master

Queried by `date` and `sym`, one round trip for the whole universe.

- **`sym`** is the Bloomberg ticker joined to its Bloomberg exchange code with a
  dot: `005930.KS`, `2330.TT`, `BHP.AU`, `600000.C1`. Built from the crosscode's
  `BloombergCode` (`"005930 KS"`) by replacing the space.
- **`date`** starts at `.z.D-1` and walks back to the most recent date that
  actually has rows, because `.z.D-1` lands on a Sunday every Monday and on
  every holiday. The run reports the date requested and the date used.

Columns consumed:

| equity_master | Output column |
|---|---|
| `PX_LAST` | `Close` |
| `EQY_BETA` | `Beta` — the uppercase Bloomberg field, not the lowercase `beta` |
| `volatility` | `Volatility10D` |
| `REL_INDEX` | `Index`, and seeds `ICBIndex` |
| `CUR_MKT_CAP`, `fx_last` | `MarketCap`, and `Capi` derives from it |
| `ID_ISIN` | `ISIN` |
| `INDUSTRY_SECTOR` | `Sector` |
| `MARKET_STATUS` | validation and reporting only — see below |
| `CRNCY` | reported alongside `MarketCap` |

## MarketStatus is not an output column

Worth stating plainly, because it is on the must-fill list. `MARKET_STATUS`
appears nowhere in the 20 columns. In the R job it is used once, at `:183-186`,
to filter *MSCI index tickers* to those that are `ACTV` with a non-null price
updated within 60 days.

So it is treated as a validation input and a reported figure: the run prints the
`MARKET_STATUS` distribution across the universe and counts rows that are not
`ACTV`. If `equity_master` turns out to carry index-level rows as well as equity
rows, the same filter can gate the msci_mapping the way the R job does; if it
does not, that validation step is reported as skipped.

## Output columns

Twenty columns, selected and renamed as at `:459-472`, sorted
`(FidessaMarket, RicCode)` per `:606`.

### Always filled

| Column | Source |
|---|---|
| `#FidessaCode` | crosscode |
| `Type` | crosscode |
| `Close` | `PX_LAST` |
| `Beta` | `EQY_BETA` |
| `Volatility10D` | `volatility` ⚠ |
| `Index` | `REL_INDEX` |
| `ICBIndex` | `REL_INDEX` seed at `:137`, then the propagation below |
| `MarketCap` | `CUR_MKT_CAP × fx_last` ⚠ |
| `Capi` | buckets on `MarketCap` ⚠ |
| `ISIN` | `ID_ISIN` |
| `Sector` | `INDUSTRY_SECTOR` ⚠ |
| `NoShortSell` | market list, `:314-320` |
| `RespectShortSellPrice` | market list + ETF/REIT rule, `:302-312` |
| `SubscribeFeedAtStartup` | always `FALSE` — bug preserved, see below |

### Filled only when the optional file is present

| Column | Needs |
|---|---|
| `MsciCountryIndex` | msci_mapping |
| `MsciSectorCountryIndex` | msci_mapping |
| `MsciSectorIndex` | msci_mapping |
| `MsciSectorRegionIndex` | msci_mapping |
| `OpenAggressivityPct` | the override CSV |
| `Segment` (HKG, NSI, BSE) | the dico and CAS lists |

`Segment` for ASX and CN is pure logic and always fills: ASX buckets
alphabetically on the ticker's first character (A-B / C-F / G-M / N-R / S-Z) with
ETFs forced to `A-B` (`:375-401`); CN forces ETFs on SHA/SHH/SSC to `NO_CAS`
(`:445-456`). The default is `"Default"` per `:576`.

`Segment` for HK ETFs is the one genuinely unavailable field — it comes from
`TRADING_CONDITIONS_1` via `load_BbgIntraday` at `:357`, an intraday Bloomberg
call with no equivalent in `equity_master`. Those rows keep the value the dico
list gives them, or `"Default"`.

### The three ⚠ flags

Each prints a banner in the run report. None is silently trusted.

1. **`Volatility10D`** — `equity_master.volatility` may not be the 10-day figure
   Bloomberg's `VOLATILITY_10D` returns. Filled on the user's instruction, with
   the definition marked unverified. Confirm before cutover.
2. **`MarketCap` / `Capi`** — assumes `fx_last` is a local→USD rate matching what
   `load_FXdatas` returned. Direction unverified. Buckets are the R job's:
   `MICRO ≤ 300m`, `SMALL > 300m`, `MID > 2bn`, `BIG > 10bn` (`:165-168`).
3. **`Sector`** — `get.sector` at `:295` prefers `GICS_SECTOR_NAME` and falls
   back to `INDUSTRY_SECTOR`. `equity_master` has no `GICS_SECTOR_NAME`, so every
   row takes the fallback and will differ from the current file wherever GICS had
   a value. The `,` → `|` substitution at `:89` is applied.

## The MSCI ladder

`msci_mapping.csv` has four columns — `IndexName`, `FidessaMarket`,
`GICS_SECTOR_NAME`, `INDUSTRY_SECTOR` — partitioned by which fields are blank
into five lookup tables (`:190-212`). `IndexName` resolves most specific first:

1. exact on `(GICS_SECTOR_NAME, FidessaMarket, INDUSTRY_SECTOR)`
2. `COUNTRY_SectorIndex` on `(INDUSTRY_SECTOR, FidessaMarket)`
3. `REGION_SectorIndex` on `INDUSTRY_SECTOR`, itself falling back to
   `REGION_GICSIndex` on `GICS_SECTOR_NAME`
4. `FB_GICS_SectorIndex` on `(GICS_SECTOR_NAME, FidessaMarket)`
5. `MSCI_COUNTRY_INDEX` — `substr(COUNTRY_SectorIndex, 1, 4)` per FidessaMarket,
   with `MXTW` overridden to `TAMSCI` (`:225-229`)

Then `MsciSectorIndex`, `MsciSectorCountryIndex` ← `IndexName`;
`MsciSectorRegionIndex` ← `REGION_SectorIndex`; `MsciCountryIndex` ←
`MSCI_COUNTRY_INDEX`.

**Known degradation:** steps 1, 3-fallback and 4 key on `GICS_SECTOR_NAME`, which
`equity_master` does not have. Those paths go dead and resolution proceeds
through the `INDUSTRY_SECTOR` and country paths only. The columns still fill, but
more coarsely than the R job. The run reports a fill rate per `Msci*` column so
the size of the gap is a number, not a guess.

## The ICB propagation

`ICBIndex` is seeded unconditionally from `REL_INDEX` at `:137` — this is the R
job's own behaviour, not a substitution. It is then refined at `:263-269`:

- `EXT_RIC` = everything after the last `.` in `RicCode`
- `EXT_BBG` = everything after the last space in `BloombergCode`
- rows with a known `ICBIndex`, excluding `EXT_RIC` in `("NoRIC", "TWO")` and
  `FidessaMarket` in `("SZA-MAIN", "SZC-MAIN")`, are grouped by
  `(EXT_BBG, EXT_RIC, FidessaMarket)` and their unique `ICBIndex` propagated to
  rows in the same group that lack one

The SZA/SZC exclusion is because those two markets share a RIC extension with two
possible ICB values and cannot be told apart (`:264`).

Needs only `RicCode`, `BloombergCode` and `FidessaMarket` — all from the
crosscode. No Bloomberg, no kdb.

## Bugs preserved verbatim

Both are reproduced exactly and reported. Neither is silently fixed — the R job
is the reference, and a port that quietly diverges is worse than one that
diverges loudly.

1. **`:113`, `if (length(idx) == 0)`.** `idx` is the set of rows *missing*
   `CUR_MKT_CAP` / `EQY_BETA` / `VOLATILITY_10D` / `INDUSTRY_SECTOR`, so the
   top-up block runs only when there is nothing to fix, on an empty frame.
   `MKT_CAP_LAST_TRD`, `BETA_ADJ_OVERRIDABLE` and `INTERVAL_VOLATILITY` have
   never populated anything. Almost certainly should be `> 0`.
   *In the port this is moot* — there is no second Bloomberg call to gate — but
   the run reports how many rows would have entered the top-up, which is the
   number that tells the desk whether fixing it in R would change anything.
2. **`:599-600`, `SubscribeFeedAtStartup`.** Sets every row to `F`, then sets the
   India rows to `F` again. The commented-out original at `:562-563` used `T`.
   The column is therefore always `FALSE`, which the sample output row confirms.

## Modules

Small, single-purpose, testable in isolation.

| Module | Does | Depends on |
|---|---|---|
| `crosscode.py` | read CrossCode.csv, 7 columns, report exclusions | file |
| `equitymaster.py` | date rollback, sym construction, one kdb round trip | kdb |
| `msci.py` | mapping reader and the five-step ladder | file |
| `columns.py` | Capi buckets, short-sell lists, ASX/CN segment, sector, ICB propagation | nothing |
| `trading_data.py` | orchestrate, validate, write, report, email | the above |

`columns.py` takes values and returns values with no I/O, so every market rule is
testable with no kdb, no licence and no shares outstanding. `pykx` is imported
inside `connect()` so `--self-test` and `--demo` run on any machine.

Config lives in `config/*.csv` so the desk can edit it in Excel:
`markets.csv` carries the short-sell and segment market lists that are hardcoded
in the R at `:302-320`.

`local_settings.py` beside the script holds the kdb host/port, file paths and
SMTP. Gitignored, and strict — an unknown name in it is a hard error, so a
typo'd `EMAIL_T0` fails loudly instead of silently sending nowhere.

## Output and failure

Write to a temp path, validate, then copy to the destination (`:611-613`).
Nothing is ever partially published. `write.csv` semantics are matched:
`row.names=F`, `na=""`, `quote=FALSE`.

Validation before the copy: row count against the crosscode, the six must-fill
columns' fill rates, and a hard failure if `Close` or the row count is zero.

On failure, email with the log attached, as `SendingEmail` does at `:477-498`.

## Modes

| Mode | Purpose |
|---|---|
| `--self-test` | embedded `self_test()` per module, no pytest, no kdb |
| `--demo` | whole pipeline on canned data, no kdb |
| `--compare OLD.csv` | per-column agreement against the R job's output |
| `--date YYYY.MM.DD` | override the `.z.D-1` default |

`--compare` is the cutover instrument. It reports, per column: exact matches,
near matches for the numeric ones, and the rows that differ — so
`Volatility10D`'s unverified definition becomes a measured spread on the first
run rather than an argument.

## Second deliverable

`docs/bloomberg-fields.txt` — every field the R job requests from Bloomberg,
listing for each: the call site, the output column it feeds, and whether
`equity_master` covers it. Plain text, as requested.

## Testing

Embedded `self_test()` in every module, run as `python <module>.py --self-test`,
following `kdb-queries/scripts/lib/price_bands.py`. No pytest anywhere in the
project.

What gets tested:

- `columns.py` — bucket boundaries at exactly 300m / 2bn / 10bn, the ETF/REIT
  short-sell rule, ASX bucketing including the ETF override, CN's SHA/SHH/SSC
  rule, the ICB propagation including both exclusions
- `crosscode.py` — the `#` header cell, missing columns, the REIT flag
- `msci.py` — each rung of the ladder, the `MXTW`→`TAMSCI` override, and
  resolution with `GICS_SECTOR_NAME` absent
- `equitymaster.py` — sym construction, date rollback over a weekend, and a fake
  connection object so no kdb is needed
- `trading_data.py` — `--demo` end to end, and `--compare` against a known diff

## Open questions

Tracked, not blocking. Each has a defined behaviour today.

1. Is `equity_master.volatility` the 10-day figure? Filled and flagged until
   answered.
2. Is `fx_last` local→USD, and does it match `load_FXdatas`? Assumed and flagged.
3. Does `equity_master` carry index-level rows, so the MSCI `ACTV` validation can
   run? Reported as skipped if not.
4. Which server hosts `equity_master`? It is in neither the `:5010`/`:5012` order
   pair nor the `:5011`/`:5013` qatt pair. Goes in `local_settings.py`.
5. Is there any source for `GICS_SECTOR_NAME`? Without it `Sector` takes the
   fallback and three rungs of the MSCI ladder stay dead.
