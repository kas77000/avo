# Phase1 on Nova: the R jobs

AB exports one zip a day, `phase1-YYYYMMDD.zip`, with everything kdb knows
about that trading day. Nova has no kdb and no B-PIPE; these three R jobs
turn the zip into the files B-PIPE used to feed:

| Job | Writes |
|-----|--------|
| `historical.r` | the Historical tick files, a one-line file for each name that only quoted, NoTradingDay rows |
| `limit_up_down.r` | `limitUpDown.csv` for the first cutoff (Japan and Korea), copied to Test, Pilot and Prod |
| `trading_data.r` | `TradingData.csv` |

`run_phase1.cmd` runs all three, in that order.

## Install

1. Copy the whole `Phase1/Nova` folder to the Nova PC, for example to
   `C:\path\to\Phase1\Nova`. Keep `config\` beside the scripts; `tests\` is
   only needed for the self-tests.
2. Check R. The jobs are written for R 3.2.2 with dplyr 0.5.0. In R, or
   with `Rscript -e`:

   ```
   R.version.string          # R version 3.2.2 ...
   packageVersion("dplyr")   # '0.5.0'
   ```

3. Copy `settings.r.example` to `settings.r` in the same folder and fill it
   in. Forward slashes work on Windows. `settings.r` is never committed.

   | Setting | What it is |
   |---------|------------|
   | `CROSSCODE_PATH` | Nova's `CrossCode.csv`. It must be the same universe AB exported from, or names go missing. |
   | `OUTPUT_DIR` | The Historical folder. Each name gets a folder here. |
   | `NOTRADINGDAY_DIR` | Where the `NoTradingDay <Country>.csv` files are. |
   | `LOG_DIR` | Every job appends to `LOG_DIR/phase1-YYYYMMDD.log`. |
   | `KDB_TIMEZONE` | The Windows timezone id kdb stamps its prints in, `China Standard Time`. It must match the zip's manifest (`kdb timezone`); if they differ, `historical.r` says so with `!!` and uses this one. |
   | `LULD_OUT_TEMP` | `limitUpDown.csv` is written here first, then copied to each environment. |
   | `LULD_OUT_TEST`, `LULD_OUT_PILOT`, `LULD_OUT_PROD` | Where each environment reads `limitUpDown.csv`. A blank one is skipped with `!!`. |
   | `INDIA_NSE_STRA`, `INDIA_BSE_STRA` | Not read in Phase1 (India is not at the first cutoff). Leave them `""`. |
   | `TD_OUTPUT_PATH` | The full path of `TradingData.csv`. |
   | `MSCI_MAPPING_PATH` | The MSCI mapping CSV. `""` leaves the four `Msci*` columns blank. |
   | `OPEN_AUCTION_OVERRIDE_PATH` | The open-auction override (`RicCode`, `OpenAggressivityPct`). `""` leaves `OpenAggressivityPct` blank. |
   | `HKEX_CAS_LIST_PATH` | HKEX's CAS list, one stock code a line. `""`: no Hong Kong row is marked `NO_CAS`. |
   | `INDIA_NSE_CAS_LIST_PATH`, `INDIA_BSE_CAS_LIST_PATH` | The NSE and BSE CAS lists of ISINs. `""`: no India row is marked `CAS`. |

   The optional paths are read when set and skipped when `""`, and the log
   says which. A path that is set but does not exist is skipped with `!!`.

4. Open `run_phase1.cmd` in a text editor and set `RSCRIPT` on its first
   `set` line to R 3.2.2's Rscript, for example
   `C:\path\to\R-3.2.2\bin\x64\Rscript.exe`.

5. Optional, to check the install: each job has a self-test that needs no
   settings and writes only to a temp folder.

   ```
   Rscript common.r --self-test
   Rscript historical.r --self-test
   Rscript limit_up_down.r --self-test
   Rscript trading_data.r --self-test
   ```

   Each ends with `all checks passed` or `SOME CHECKS FAILED`.

## The daily run

1. Copy the day's zip from AB (its `EXPORT_DIR`) to the Nova PC.
2. Run, from a command prompt or by dragging the zip onto the `.cmd`:

   ```
   run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip
   ```

   To publish `limitUpDown.csv` to some environments only, name them as the
   second argument, in quotes (a bare `|` means something else to cmd):

   ```
   run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip "Test|Prod"
   ```

The jobs run one after another. The first one that fails stops the run:
the window says `XX  <job> failed` and the jobs after it are not run. The
window stays open until a key is pressed, whatever happened.

A job can be run on its own the same way:
`Rscript historical.r C:\path\to\phase1-YYYYMMDD.zip`.

Running the same zip again is safe. `historical.r` leaves a tick file that
already exists alone and does not repeat a NoTradingDay row;
`limit_up_down.r` and `trading_data.r` overwrite their file.

## What each job writes

**`historical.r`**

- `OUTPUT_DIR/<code>/raw-<code>-YYYYMMDD.csv` for every name with prints,
  one line per print, in the market's own clock:

  ```
  #Time,Last,Volume,Condition,Exchange,MicCode,Tokyo Standard Time
  09:00:00,2850,412300,O,T,XTKS
  ```

  `<code>` is the name's BloombergCode, or its composite where
  `config/hist_composites.csv` says so (`7203 JT` is written as `7203 JP`).
- The same file with ONE line for a name that did not trade but quoted:
  the mid of the last bid and ask, `Volume` 0, the market's first close
  code as the condition.
- `NOTRADINGDAY_DIR/NoTradingDay <Country>.csv`, a `stock,date` row for a
  name with neither prints nor a quote. The country comes from
  `config/close_conditions.csv`.

**`limit_up_down.r`** writes `LULD_OUT_TEMP` and copies it to each
environment asked for:

```
#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue
7203.T,7203 JT,2026-09-28,3376,2376,7203.TYO,TYO-MAIN
```

Only the first cutoff's venues (07:30: TYO, JNX, CHJ, KOE, KSC). The limits
are for the next weekday, computed from the day's close and tick ladder with
the bands in `config/luld_bands.csv`. If any row fails validation, nothing
is published.

**`trading_data.r`** writes `TD_OUTPUT_PATH`: one row per CrossCode name
with a BloombergCode, the reference columns from `equity.csv` and the
`Close`, sorted by market then RicCode. It is not written if no row has a
Close.

## Reading the log

All three jobs append to `LOG_DIR/phase1-YYYYMMDD.log`, dated by the trade
day in the zip. Each job's run starts with `=== YYYY-MM-DD HH:MM:SS ===`,
then one line per event:

```
09:12:03  ..  files written           5   17 prints
09:12:04  !!  1 excluded, ladder: no tick ladder for this name (123450 KQ KOE-MAIN close 8420)
```

| Level | Meaning |
|-------|---------|
| `..` | Information: what was read, counts, steps. |
| `ok` | The job finished and wrote its output. |
| `!!` | Something was skipped or dropped. The run went on; look at it. |
| `XX` | The job failed. Its output was not written (or not published). |

What each `!!` means:

| Job | `!!` line | Meaning |
|-----|-----------|---------|
| historical | `the manifest says kdb's clock is ...` | The zip's `kdb timezone` is not `KDB_TIMEZONE`. Times may be shifted wrongly. |
| historical | `N excluded: no equity_master row and no configured composite` | Names AB could not resolve to a kdb sym. They get no file. |
| historical | `N excluded: same file on disk as ...` | Two names map to one file name; the first in code order keeps it. |
| historical | `N names had no master row and took hist_markets.csv's composite` | Written, but with a blank MicCode. |
| historical | `<market>: no TimeZone to convert to` | Its files keep kdb's clock and carry no timezone in the header. |
| historical | `N syms in ticks.csv are not in the universe` | The zip has prints for names not in this CrossCode. They are dropped. The CrossCodes on AB and Nova differ. |
| historical | `<code>: a quote with no bid or ask; no file` | A quote-only name with nothing to price. |
| historical | `<code>: no Country for <ext> in close_conditions.csv` | No NoTradingDay row could be written for it. |
| limit_up_down | `N excluded, <token>: <reason> (...)` | Names with no limit in the file. See the tokens below. |
| limit_up_down | `<env>: LULD_OUT_<ENV> is blank; not published there` | That environment did not get the file. |
| trading_data | `<KEY> <path> does not exist; ...` | An optional input is set but missing; its columns are blank. |
| trading_data | `the HKEX list is EMPTY` | No Hong Kong row is marked `NO_CAS`. |
| trading_data | `NOT ONE RicCode on the override matched the crosscode` | The open-auction override is probably the wrong file. |
| trading_data | `the override has no OpenAggressivityPct column`, `N duplicate RicCode in the override` | The override file is malformed; the last row of a duplicate wins. |

The `limit_up_down.r` exclusion tokens:

| Token | Why the name has no limit |
|-------|---------------------------|
| `close` | No close in the zip: no closing print, and equity_master's PX_LAST was blank or not positive. |
| `ladder` | kdb has no tick ladder for it. |
| `tick-tier` | Its tick ladder has no tier for its close. |
| `band-tier` | `config/luld_bands.csv` has no tier for its close. |
| `min-price` | The venue's MinPrice is at or above the computed up limit. |
| `other` | Anything else, such as a close that is not positive; the reason is on the line. |

The close fallbacks themselves (a close taken from equity_master rather than
a closing print) are decided on AB and logged there, one `!!  close` line
each; the zip's `closes.csv` carries the source and reason for every sym.

## Known unverified items

These are assumptions nobody has checked against the live systems yet.
Check them on the first real days.

- **TIME_FIELD.** AB reads each print's time from the kdb column named in
  `qattsource.TIME_FIELD` (`tradeTime`). The zip's manifest shows it as
  `time column`. If it is the wrong column, every tick file's times are
  wrong by the same rule, and nothing fails.
- **Close codes present in qatt.** The close is the last print carrying one
  of the market's codes in `close_conditions.csv` (`e`/`ES` for Japan, `GC`
  for Korea, `CA` for most others). If qatt does not carry those codes, every
  close falls back to equity_master's PX_LAST, and AB's log fills with
  `!!  close ... no-closing-trade`.
- **`volatility` and `CUR_MKT_CAP` units.** `trading_data.r` assumes
  equity_master's `volatility` is a fraction (0.24, written as
  `Volatility10D` 24) and `CUR_MKT_CAP` is in millions (multiplied by
  1,000,000, then by `fx_last`, for `MarketCap` and the `Capi` bucket). If
  either unit is different, those columns are off by a factor.
- **The quote-only Exchange letter.** A quote carries no exchange, so a
  quote-only line takes the most common `Exchange` among the day's prints of
  names with the same exchange code. On a day where no such name printed, it
  is blank.
- **15 significant digits.** Numbers the R jobs compute are written with at
  most 15 significant digits, as the legacy R job did. Phase0's Python wrote
  full precision, so a value like a large MarketCap can differ in its last
  digits.
