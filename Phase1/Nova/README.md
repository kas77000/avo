# Phase1 on Nova: the R jobs

AB exports one zip a day, after 18:30 HKT, `phase1-YYYYMMDD.zip`, with
everything kdb knows about that trading day. Nova has no kdb and no
B-PIPE; these three R jobs turn the zip into the files B-PIPE used to feed:

| Job | Writes |
|-----|--------|
| `historical.r` | the Historical tick files, a one-line file for each name that only quoted, NoTradingDay rows |
| `limit_up_down.r` | `limitUpDown.csv` for the first cutoff (Japan and Korea), copied to Test, Pilot and Prod |
| `trading_data.r` | `TradingData.csv` |

`run_phase1.cmd` runs all three, in that order - or, for a zip AB made
with `--for`, only the jobs it was made for (see below).

All three cover the same universe as AB's extract: the CrossCode rows whose
Bloomberg exchange code (`7203 JT` -> `JT`) is one of the 18 codes in
`config/close_conditions.csv`, whatever their Type. `historical.r` and
`trading_data.r` log the rest as one `..  N rows dropped: exchange code not
ours` line; `limit_up_down.r` covers only the first cutoff's venues anyway.

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
   | `KDB_TIMEZONE` | The Windows timezone id kdb stamps its prints in. Left out or `""`, it is `China Standard Time`. It must match the zip's manifest (`kdb timezone`); if they differ, `historical.r` stops with `XX` and writes nothing, since a tick file is never rewritten. |
   | `WORKERS` | Optional. How many R processes `historical.r` writes the tick files with. Left out or `""`: one fewer than the PC's cores, at most 4. `1` writes from the job's own process. If the workers cannot start, the log says `!!` and the job writes from its own process. |
   | `TICK_BLOCK` | Optional. How many rows of `ticks.csv` `historical.r` reads at a time; the file is never held whole. Left out or `""`: 500000. Lower it if the PC runs short of memory. |
   | `LULD_OUT_TEMP` | `limitUpDown.csv` is written here first, then copied to each environment. |
   | `LULD_OUT_TEST`, `LULD_OUT_PILOT`, `LULD_OUT_PROD` | Where each environment reads `limitUpDown.csv`. An environment the run asks for (all three by default) whose path is blank FAILS the job with `XX`; the others still get the file. Leave one blank only if you always name the environments, for example `"Test|Prod"`. |
   | `INDIA_NSE_STRA`, `INDIA_BSE_STRA` | Not read in Phase1 (India is not at the first cutoff). Leave them `""`. |
   | `TD_OUTPUT_PATH` | The full path of `TradingData.csv`. |
   | `MSCI_MAPPING_PATH` | The MSCI mapping CSV. `""` leaves the four `Msci*` columns blank. |
   | `OPEN_AUCTION_OVERRIDE_PATH` | The open-auction override (`RicCode`, `OpenAggressivityPct`). `""` leaves `OpenAggressivityPct` blank. |
   | `HKEX_CAS_LIST_PATH` | HKEX's CAS list, one stock code a line. `""`: no Hong Kong row is marked `NO_CAS`. |
   | `INDIA_NSE_CAS_LIST_PATH`, `INDIA_BSE_CAS_LIST_PATH` | The NSE and BSE CAS lists of ISINs. `""`: no India row is marked `CAS`. |
   | `SMTP_HOST` | Optional. The mail server the recap is sent through (port 25, no login), e.g. `"smtp.example.invalid"`. `""`: no mail. |
   | `EMAIL_FROM` | Optional. The recap's sender. `""`: no mail. |
   | `EMAIL_TO` | Optional. The recap's recipients, a character vector: `c("ops@example.invalid", "desk@example.invalid")`. `""`: no mail. |

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
   Rscript mail_report.r --self-test
   ```

   Each ends with `all checks passed` or `SOME CHECKS FAILED`.

## The daily run

1. After 18:30 HKT, once AB has exported the day, copy the zip from AB (its
   `EXPORT_DIR`) to the Nova PC.
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

Last, once the zip was accepted (all done, a job failed, or jobs skipped),
`run_phase1.cmd` runs `mail_report.r`, which writes the recap and mails it;
see [The recap](#the-recap). If the recap itself fails, the window says
`!!  mail_report.r failed; no recap.` and nothing else changes: the last
line and the exit code are still the jobs'.

A job can be run on its own the same way:
`Rscript historical.r C:\path\to\phase1-YYYYMMDD.zip`.

**A zip made for some jobs only.** AB's `extract.py --for` makes a zip for
some of `luld` (`limit_up_down.r`), `td` (`trading_data.r`) and `ticks`
(`historical.r`), e.g. `phase1-YYYYMMDD-luld-td.zip`, and its manifest says
which under `for`. A zip with no `for` (every zip made before `--for`) is
for all three. `run_phase1.cmd` asks each zip first
(`Rscript common.r --zip-has <use> <zip>`) and runs only its jobs; the
others are skipped, a `..  historical.r skipped: this zip was not made for
ticks` line each, and the run ends `ok  jobs done; skipped, not in this
zip: historical.r`. A skipped job is not a failure. A job run by hand on a
zip not made for it stops before unzipping, with

```
XX  this zip was made for luld|td; historical.r needs a zip made with --for ticks
```

Running the same zip again is safe. `historical.r` leaves a tick file that
already exists alone and does not repeat a NoTradingDay row;
`limit_up_down.r` and `trading_data.r` overwrite their file.

So a `historical.r` run that stopped part way (a closed window, a lost
share) is finished by running the same zip again: it skips every file
already there, before formatting any of its rows, logs `..  N files
already there, skipped`, and writes the rest. A file is written as
`<name>.csv.part` and renamed when complete, so a file under its real name
is always whole; a `.part` left behind is overwritten.

`historical.r` shows it is alive: the console says when it unzips and
counts `ticks.csv` (a large file takes a while), then step 2 logs one line
per block of `ticks.csv`:

```
..  block 3  rows 1500000/3000000 (50%)  files written 9870  skipped 0  41s  36585 rows/s
```

and ends with the seconds spent reading and writing, the rows a second and
the workers used. The log FILE (not the console, there are tens of
thousands) also has one line for every file: `..  wrote <code>/raw-<code>-YYYYMMDD.csv  N rows`
(`(quote-only)` for a quote-only file), or `..  skip <code>/... (exists)`.

Each job unzips only the members it reads (`limit_up_down.r` and
`trading_data.r` never extract `ticks.csv`) and checks them before
anything else: a member that does not unzip cleanly, or a `ticks.csv`,
`closes.csv` or `quote_only.csv` whose row count is not the manifest's
(`prints`, `syms asked`, `quote only`), stops the job with an error and
nothing is written. The zip was cut short on the way; copy it again.

### An old day

`limit_up_down.r` refuses a zip whose LimitDate (the next weekday after
its trade date) is before today, and `trading_data.r` one whose trade date
is more than 4 calendar days before today; both fail with `XX` and write
nothing. Their files are for the next trading day only.

So a backfill of an old day only needs `historical.r`:

```
Rscript historical.r C:\path\to\phase1-YYYYMMDD.zip
```

`run_phase1.cmd` on an old zip writes the tick files, then stops at
`limit_up_down.r` with `XX`.

### One market only

`historical.r` takes `--market=` with one market, or several joined by `|`,
each named by its FidessaMarket (`NZE-MAIN`) or its Bloomberg exchange code
(`NZ`), to redo just those markets, for example after fixing a venue's
TimeZone in `config/hist_markets.csv`:

```
Rscript historical.r C:\path\to\phase1-YYYYMMDD.zip --market=NZE-MAIN
Rscript historical.r C:\path\to\phase1-YYYYMMDD.zip "--market=NZE-MAIN|HKG-MAIN"
```

Every other name is left out of that run: no file, no NoTradingDay row.
Files already there are still skipped. A name that is neither a
FidessaMarket of the CrossCode's rows nor a code in
`config/close_conditions.csv` stops the run.

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
  `config/close_conditions.csv`. Only a name AB asked kdb about (its sym
  has a row in the zip's `closes.csv`) gets one: a row there is permanent,
  and a name only Nova's CrossCode has may well have traded.
- Nothing for a name whose market has no `TimeZone` in
  `config/hist_markets.csv`: its prints are not written in kdb's clock.
  Once the market is set, running the zip again writes it.

A name's kdb sym is the one `master.csv` gives its BloombergCode, else
`<ticker>.<BBGComposite>` from `config/hist_markets.csv`, as AB resolves
it. `limit_up_down.r` and `trading_data.r` find each name's close in
`closes.csv` the same way.

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
in the extract's universe (above) with a BloombergCode, the reference
columns from `equity.csv` and the `Close`, sorted by market then RicCode.
It is not written if no row has a Close.

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

What stops a job with `XX` (or an R `Error`, before the log is open):

| Job | Why |
|-----|-----|
| all | The zip does not unzip cleanly, or a member's rows are not the manifest's count. Copy the zip again. |
| all | `this zip was made for <uses>; <job> needs a zip made with --for <use>`: make that zip on AB. |
| historical | The manifest's `kdb timezone` is not `KDB_TIMEZONE`. Nothing is written. |
| historical | `ticks.csv is not grouped by sym`: a sym's rows are split. AB never writes that; the zip is not one it wrote. |
| limit_up_down | `<env>: LULD_OUT_<ENV> is blank`: an environment asked for has no path. The others still get the file. |
| limit_up_down | The LimitDate is before today: an old zip. Nothing is published. |
| limit_up_down | A row fails validation, or the output is empty. Nothing is published. |
| trading_data | The trade date is more than 4 days before today: an old zip. |
| trading_data | Not one row has a Close. |

What each `!!` means:

| Job | `!!` line | Meaning |
|-----|-----------|---------|
| historical | `N excluded: no equity_master row and no configured composite` | Names AB could not resolve to a kdb sym. They get no file. |
| historical | `N excluded: same file on disk as ...` | Two names map to one file name; the first in code order keeps it. |
| historical | `N names had no master row and took hist_markets.csv's composite` | Written, but with a blank MicCode. |
| historical | `<code>: no TimeZone for <market> in hist_markets.csv; no file` | Set the market's `TimeZone` (or the name's `FidessaMarket`) and run the zip again. After 20 such lines, one more gives the count of the rest. |
| historical | `<code> not in the extract, nothing written` | A name in Nova's CrossCode that AB did not ask kdb about: no file and no NoTradingDay row. The CrossCodes on AB and Nova differ. After 20 such lines, one more gives the count of the rest. |
| historical | `N syms in ticks.csv are not in the universe` | The zip has prints for names not in this CrossCode. They are dropped. The CrossCodes on AB and Nova differ. |
| historical | `could not start N workers (...); writing in one process` | The run goes on, slower. The reason is on the line. |
| historical | `<code>: a quote with no bid or ask; no file` | A quote-only name with nothing to price. |
| historical | `<code>: no Country for <ext> in close_conditions.csv` | No NoTradingDay row could be written for it. |
| limit_up_down | `N excluded, <token>: <reason> (...)` | Names with no limit in the file. See the tokens below. |
| trading_data | `<KEY> <path> does not exist; ...` | An optional input is set but missing; its columns are blank. |
| trading_data | `the HKEX list is EMPTY` | No Hong Kong row is marked `NO_CAS`. |
| trading_data | `NOT ONE RicCode on the override matched the crosscode` | The open-auction override is probably the wrong file. |
| trading_data | `the override has no OpenAggressivityPct column`, `N duplicate RicCode in the override` | The override file is malformed; the last row of a duplicate wins. |

The `limit_up_down.r` exclusion tokens:

| Token | Why the name has no limit |
|-------|---------------------------|
| `close` | No close in the zip: no print at all that day, and equity_master's PX_LAST was blank or not positive. |
| `ladder` | kdb has no tick ladder for it. |
| `tick-tier` | Its tick ladder has no tier for its close. |
| `band-tier` | `config/luld_bands.csv` has no tier for its close. |
| `min-price` | The venue's MinPrice is at or above the computed up limit. |
| `other` | Anything else, such as a close that is not positive; the reason is on the line. |

The close itself is decided on AB: the closing print; else, for a name
that traded, its last traded price (reason `last-trade`); else
equity_master's PX_LAST (`no-trades`). AB logs each PX_LAST close and each
missing one as a `!!  close` line; the zip's `closes.csv` carries the source
and reason for every sym. The Nova jobs read only its `close` column.

## The recap

Each job leaves a small summary beside the log,
`LOG_DIR/phase1-YYYYMMDD-<job>.summary.csv` (`historical`,
`limit_up_down`, `trading_data`), two columns `key,value`: `status`
(`ok` or `failed`), `started`, `ended`, `error` when it failed, then its
counts:

| Job | Counts |
|-----|--------|
| historical | names, names excluded (and by reason), files written, prints, files skipped as existing, quote-only files, no-TimeZone names, not-in-extract names, NoTradingDay rows (and per country), `market` for a `--market=` run |
| limit_up_down | environments asked, names, names with a close, names excluded (and by token), rows published, environments copied, environments failed |
| trading_data | names, each optional input (used, skipped, not set, or skipped, missing), the fill rates, rows with Close, rows written |

A job writes its summary as `running` before it unzips anything, so one
that dies part way leaves `running` (read as failed), never an older
run's `ok`.

`mail_report.r` then builds the recap from the summaries and the day's log:

- **Subject**: `[Phase1] Nova YYYY-MM-DD: OK`, `: FAILED - <job>`, or,
  for a zip made for some jobs, `: OK (skipped: historical - zip for luld|td)`.
- **Body**: a line per job with its state and main counts, the zip's name,
  and how many `!!` and `XX` lines the run logged.
- **Attached**: `LOG_DIR/phase1-YYYYMMDD-nova-report.html`, one page
  with the Run (zip, the manifest's date, for and source, start and end,
  the machine), a section per job with all its counts, and every `!!` and
  `XX` line of this run.

"This run" in the log: the log keeps every run of the day, each opening
with `=== stamp ===` and, on its next line, the job's script name. For
each job that ran, the recap takes the last section that names it. A job
that failed before its log opened (a zip that does not unzip) has no
section; its summary's `error` is shown as its `XX` line.

The mail is sent with Windows PowerShell's `Send-MailMessage`, when
`SMTP_HOST`, `EMAIL_FROM` and `EMAIL_TO` are all set. Otherwise nothing
is sent and the log says `..  no mail: ... not set in settings.r`; the
HTML is written all the same. A mail that cannot be sent is one line,
`!!  mail not sent: <why>`, and changes no exit code.

To send the recap again, or to see it without mailing it:

```
Rscript mail_report.r C:\path\to\phase1-YYYYMMDD.zip
Rscript mail_report.r C:\path\to\phase1-YYYYMMDD.zip --no-mail
```

Run by hand, it goes by the summaries alone: a job with one is as it
says, a job the zip was not made for is skipped, any other is `not run`.

## Known unverified items

These are assumptions nobody has checked against the live systems yet.
Check them on the first real days.

- **TIME_FIELD.** AB reads each print's time from the kdb column named in
  `qattsource.TIME_FIELD` (`time`, qatt's plant clock, HKT). The zip's
  manifest shows it as
  `time column`. If it is the wrong column, every tick file's times are
  wrong by the same rule, and nothing fails.
- **Close codes present in qatt.** The close is the last print carrying one
  of the market's codes in `close_conditions.csv` (`e`/`ES` for Japan, `GC`
  for Korea, `CA` for most others). If qatt does not carry those codes, every
  traded name closes at its last trade instead, and AB's summary table shows
  the market with mostly `last-trade`.
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
