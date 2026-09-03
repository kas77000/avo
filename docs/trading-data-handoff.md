# TradingData — handoff notes

Context carried over from another Claude session (nova-18) on 2026-09-02 and
verified against the working tree. Read this before starting the Python port.

**There is no Python for TradingData yet.** Only a hand-transcribed R script and
the analysis below. The blocker is resolved as of 2026-09-03 — the reference
table is `equity_master`. Design in
`docs/superpowers/specs/2026-09-03-trading-data-python-design.md`.

## What exists

| Path | What |
|---|---|
| `no_git/CreateTradingDataENT.r` | 676 lines, the only TradingData artefact (gitignored) |
| `no_git/TradingData/IMG_4772..4782.HEIC` | photos of the VS Code screen the script was transcribed from |
| `no_git/TradingData/IMG_4784.HEIC` | a sample of the output `TradingData.csv` |

Provenance: the `.r` file was transcribed by hand from 12 iPhone photos, not
recovered from git or a share. Line numbers in the photos overlap continuously
(1-67, 66-132, ... 614-679), so coverage is complete, but treat any surprising
character as OCR-suspect. Brackets and string literals were verified to balance.
R is not installed here, so the file has never been parsed.

To re-read a photo: `pillow_heif` + `PIL` are installed under
your local Python. Convert to PNG, crop and upscale 2-3x for
anything ambiguous.

`LimitUpDown/v1` is a completed Python port of the sibling R job and is the
template for this one (`v2` adds a bpipe source; `other/` holds bpipe probes). See "Conventions" below and
`docs/limit-up-down-how-it-works.md`.

## What the script does

Builds `TradingData.csv`, the instrument reference file the Nova ATS loads. One
row per instrument, 20 columns (selected at `:459`, renamed at `:467-470`):

`#FidessaCode, Type, Sector, Capi, Index, ICBIndex, MsciCountryIndex,
MsciSectorCountryIndex, MsciSectorIndex, MsciSectorRegionIndex, Segment, Beta,
Close, Volatility10D, NoShortSell, RespectShortSellPrice, OpenAggressivityPct,
MarketCap, ISIN, SubscribeFeedAtStartup`

Sample row (ASX):
`10X.AX,Equity,Materials,MICRO,AS51,AS51,MXAU,MXAU0MT,MXAU0MT,MXAP0MT,A-B,0.135,83.64,FALSE,,,3122920,AU0000430449,FALSE`

### Inputs

- args: job ids, e.g. `AlgoEnterpriseProd AlgoEnterprisePilot AlgoEnterpriseTest`,
  or `SorEnterprise{Test,Pilot,Prod}`
- `r_home` = CHANGEME
- XML config: `<r_home>cfg\TradingDataENT\TradingData_ENT_Config.xml`
- Data Licence file: `get_config_value("BUFFER") + /analytics_data/EquitiesDataLicence.rds`
- log dir: `<r_home>/log/`

XML keys (parsed by `getJobInfo` `:20-80`): `MailConfiguration`, `HkexCASList`,
`IndiaNseCasList`, `IndiaBseCasList`, `TempPathFile`, `MsciMapping`,
`OpenAuctionAggressiveLevel`, and per-job `CrossCodePath` / `OutputPath`.

### Outputs

- writes `job.info$TempPathFile` (`write.csv`, `row.names=F`, `na=""`, `quote=FALSE`)
- then `fs::file_copy` to `<OutputPath>TradingData.csv` (`:611-613`)
- failure email via `SendingEmail` (`:477-498`), template
  `GenerationTradingDataENTFailed`, log attached
- log `<logPath>TradingDataENT_<timestamp>.log`

### Bloomberg surface (verified call sites)

| Line | Call | Supplies |
|---|---|---|
| `:83` | `readRDS(EquitiesDataLicence.rds)` — bulk file, no terminal | `PX_LAST`, `EQY_BETA`, `CUR_MKT_CAP`, `REL_INDEX`, `INDUSTRY_SECTOR`, `ID_ISIN` |
| `:103` | `R_bdp` | `VOLATILITY_10D`, `GICS_SECTOR_NAME` |
| `:120` | `R_bdp` | `CUR_MKT_CAP`, `MKT_CAP_LAST_TRD`, `EQY_BETA`, `BETA_ADJ_OVERRIDABLE`, `INTERVAL_VOLATILITY`, `INDUSTRY_SECTOR` — **dead code, see gotcha 1** |
| `:181` | `R_bdp` | `PX_LAST`, `LAST_UPDATE_DT`, `MARKET_STATUS` on MSCI *index* tickers, for validation at `:186` |
| `:273` | `R_bdp` | `REL_INDEX` top-up where `ICBIndex`/`REL_INDEX` is NA |
| `:357` | `load_BbgIntraday` | `TRADING_CONDITIONS_1`, HK ETF closing auction |
| `:145` | `load_FXdatas` | FX rates — Sibyl, probably not Bloomberg; unverified |

The RDS join happens at `:90` on `BloombergCode = TICKER_AND_EXCH_CODE`; a pile
of `DVD_*` / `PX_HIGH` / `PX_LOW` / lot-size columns are dropped at `:84-88`.

Non-Bloomberg inputs: crosscode CSV, msci_mapping CSV, HKEX dico CAS file,
India NSE/BSE CAS files, OpenAuctionAggressiveLevel CSV.

### Function inventory

| Line | Function | Notes |
|---|---|---|
| `:20` | `getJobInfo` | XML -> `job.info` |
| `:82` | `get.DataLicenseInfoAndBBG` | RDS join + the two `R_bdp` calls |
| `:142` | `get.capi` | FX, `MarketCap*FX`, buckets: MICRO<=300m, SMALL>300m, MID>2bn, BIG>10bn |
| `:173` | `get.msciInfo` | msci_mapping joins, index validation, `MSCI_COUNTRY_INDEX` `substr(1,4)`, MXTW -> TAMSCI at `:225-229`, ICB workaround via `EXT_RIC`/`EXT_BBG` `:259-280` |
| `:295` | `get.sector` | `GICS_SECTOR_NAME` else `INDUSTRY_SECTOR` |
| `:302` | `respectShortSellPrice` | hardcoded market list |
| `:314` | `marketAllowShortSell` | hardcoded market list |
| `:322` | `get.OpenAuctionAggressiveLevel` | joins override CSV on `RicCode` |
| `:330` | `get.segment.hkg` | dico file for stocks, BBG for ETFs |
| `:375` | `get.segment.asx` | alphabetical A-B/C-F/G-M/N-R/S-Z on ticker first char; ETFs forced to `A-B` |
| `:402` | `get.segment.india` | CAS list matched on `ID_ISIN` |
| `:445` | `get.segment.cn` | ETFs on SHA/SHH/SSC -> `NO_CAS` |
| `:457` | `set.TradingDataColumns` | select + rename to the 20 columns |
| `:477` | `SendingEmail` | |
| `:500` | `generateTradingData` | orchestrator |
| `:628-675` | `main` | |

### How it runs

```
Rscript CreateTradingDataENT.r AlgoEnterpriseProd AlgoEnterprisePilot AlgoEnterpriseTest
```

`commandArgs(trailingOnly=TRUE)` at `:636`; empty args -> `stop("ERROR: You must
list the job names")`. Loops job ids at `:656`, one `generateTradingData` per
job, emails per-job failures, prints elapsed seconds. The scheduler entry has
not been seen.

Python side: nothing exists. Python 3.13 is installed locally.
`pykx` is not installed here and no kdb is reachable, so import it lazily inside
`connect()` the way the LimitUpDown port does, and keep `--self-test` / `--demo`
runnable anywhere.

## Blocker — RESOLVED 2026-09-03

The reference table is `equity_master`, and its schema is at
`kdb-queries/no_git/kdb/equity_master.csv` (157 columns, captured 2026-09-03).
The earlier search missed it because the file was not yet on disk, and because
it is named `equity_master`, not `equity`.

It is the Bloomberg Data Licence feed already loaded into kdb — the same feed the
R job reads as `EquitiesDataLicence.rds`. It carries `TICKER_AND_EXCH_CODE` (the
R job's join key at `:90`), and 13 of the 16 columns the R drops at `:84-88`.
So it replaces the RDS file and the `R_bdp` calls together, which reverses
gotcha 3 below.

Query it by `date` and `sym`: `sym` is the Bloomberg ticker dot-joined to its
Bloomberg exchange code (`005930.KS`, `2330.TT`, `BHP.AU`, `600000.C1`), and
`date` starts at `.z.D-1` rolled back to the last date with rows.

Covers: `PX_LAST`, `EQY_BETA`, `volatility`, `REL_INDEX`, `CUR_MKT_CAP`,
`fx_last`, `ID_ISIN`, `INDUSTRY_SECTOR`, `MARKET_STATUS`, `CRNCY`.
Does not cover: `GICS_SECTOR_NAME`, `TRADING_CONDITIONS_1`.

Design follows in `docs/superpowers/specs/2026-09-03-trading-data-python-design.md`.

## Gap analysis

Scoped to the only 13 kdb schemas we hold (`kdb-queries\no_git\kdb\*.csv`):
ORDER_SERVER RT/HIST `:5012`/`:5010` — `target`, `target_stock`, `target_state`,
`target_client`, `workorder`, `workorder0`, `workorder0_hist`, `execution`,
`goal`, `alerts`; QATT_SERVER RT/HIST `:5013`/`:5011` — `qatt`, `close_print`,
`open_print`. The ref DB is outside this set.

**Can come from kdb:** `Close` -> `close_print.price`; `Volatility` computable
from `close_print` history (10d realised — *not* the same definition as
`VOLATILITY_10D`); `Beta` computable from returns vs an index series if one
exists in kdb (also will not match `EQY_BETA`); market status inferable as "has
printed recently", which is enough since it only validates MSCI index tickers.

**Cannot, from those two servers:** `MarketCap` (needs shares outstanding),
`Index`/`ICBIndex` (membership), `Sector` (classification), `ISIN`, `Capi`
(follows MarketCap).

**Not Bloomberg already:** `#FidessaCode`, `Type` (crosscode); `NoShortSell`,
`RespectShortSellPrice`, `SubscribeFeedAtStartup` (hardcoded lists — should
become config); `OpenAggressivityPct` (CSV); the four `Msci*` columns (the
mapping CSV does the work, Bloomberg only validates the index tickers); Segment
for ASX/India/CN. The only Bloomberg part of Segment is HK ETFs via
`TRADING_CONDITIONS_1` at `:357` — `qatt` has a `cond` column that might replace
it, unverified.

## Gotchas

1. **`:113` reads `if (length(idx) == 0) {`.** `idx` (`:108-111`) is the set of
   rows *missing* `CUR_MKT_CAP` / `EQY_BETA` / `VOLATILITY_10D` /
   `INDUSTRY_SECTOR`, so the block only runs when there is nothing to fix, on an
   empty frame. `MKT_CAP_LAST_TRD`, `BETA_ADJ_OVERRIDABLE` and
   `INTERVAL_VOLATILITY` are dead and the fallback has never populated anything.
   Almost certainly should be `> 0`. Confirmed against the full-resolution photo
   and preserved verbatim. Flag it; do not silently fix it in a port.
2. **`target_stock` is a trap.** It looks perfect (sector, isin, segment, beta,
   marketcap, volatility, fxlast, currency, country, etf, industry, region,
   ticksize) but it is keyed `(date, id_server, id_target)` — one row per order,
   so only names that traded — and it is populated *from* `TradingData.csv`.
   Using it makes today's file derive from yesterday's. The user separately and
   explicitly said "you will not use target_stock".
3. **"Bloomberg" here is four different things** and they are not equally
   removable: a bulk RDS file on disk (no terminal, no rate limit, and the
   source of most reference columns), live `R_bdp` calls, `load_BbgIntraday`,
   and `load_FXdatas`. Killing the RDS is a reference-data sourcing project, not
   a script rewrite.
4. `sub("..", "", x)` appears in the sibling R script — `..` is regex "any two
   chars", not literal dots. Read regexes in these files carefully.
5. MSCI country index is `substr(COUNTRY_SectorIndex, 1, 4)`, then MXTW is
   overridden to TAMSCI (`:225-229`).
6. Output goes to a temp path and is only then `file_copy`'d to the job's output
   dir (`:611-613`). Preserve that — never publish partially.

## Conventions to mirror from LimitUpDown

- Pure arithmetic modules taking numbers and returning numbers, no I/O, so
  market rules are testable with no kdb / licence / shares.
- Embedded `self_test()` run as `python <module>.py --self-test`, following
  `kdb-queries\scripts\lib\price_bands.py`. No pytest anywhere.
- Also `--demo` (whole pipeline on canned data) and `--compare OLD.csv` (diff
  against the R job's output, for a parallel-run cutover).
- `pykx` imported lazily inside `connect()`.
- `local_settings.py` beside the script for servers/paths/SMTP, gitignored,
  strict — an unknown name in it is a hard error, so a typo'd `EMAIL_T0` fails
  loudly.
- Report, never silently drop: row-level exclusions carry a reason and are
  counted, printed and emailed. The R job filtered them away invisibly.
- Write to temp -> validate -> copy.
- Config as CSV so the desk can edit it in Excel.
- Docs: spec in `docs/superpowers/specs/`, plan in `docs/superpowers/plans/`,
  how-it-works in `docs/`.

## Scope the user asked for

> "for the trading data i just want a script that will fill the close beta,
> volatility, index info, market status and market cap, the rest normally can be
> filled without bloomberg right ?"

Six fields, not a full 20-column port unless they say so. The answer given back,
**not yet responded to**: mostly right, but three columns they did not list are
also Bloomberg today — `Sector` (`GICS_SECTOR_NAME`/`INDUSTRY_SECTOR`), `ISIN`
(`ID_ISIN`), and the HK-ETF part of `Segment` (`TRADING_CONDITIONS_1`).

Also asked for: Python instead of R; "replace the use of bloomberg with kdb
**preferably**" — they know it may not be fully possible.

Vetoed/constrained: no `target_stock`; and they push back hard on
over-engineering (a tick-ladder subsystem built for LimitUpDown was deleted
after "why are you talking about tick ladders i don't understand"). Build the
minimum thing that matches what the R job actually does; do not generalise a
one-market special case into a framework.

## Open questions

1. Where do `MarketCap` and index membership come from if not Bloomberg?
   Superseded by the ref-DB `equity` lead — get that schema.
2. Should the script emit a small enrichment file keyed by symbol, or the whole
   `TradingData.csv`? Unanswered.
3. Does `CrossCode.csv` carry an ISIN column? If so, `ISIN` stops being a
   Bloomberg dependency. The file has never been seen.
