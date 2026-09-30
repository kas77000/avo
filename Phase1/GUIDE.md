# Phase1 guide: from kdb on AB to the Nova files

Nova no longer has B-PIPE. Every evening, AB extracts the day from kdb into
one zip. Someone copies the zip to Nova, and three R jobs turn it into the
Historical tick files, `limitUpDown.csv` and `TradingData.csv`.

```
AB (Python, kdb)                    copy the zip            Nova (R 3.2.2)
extract.py  ->  phase1-YYYYMMDD.zip  ------------------->  run_phase1.cmd <zip>
                                                              historical.r
                                                              limit_up_down.r
                                                              trading_data.r
```

The two folders' READMEs (`Phase1/AB/README.md`, `Phase1/Nova/README.md`)
hold every detail. This guide covers the steps, in order.

---

## 1. Set up AB (once)

AB is the machine that can reach kdb.

1. **Get the code.** Clone or `git pull` the repository. Everything AB needs
   is in `Phase1/AB/`.
2. **Python and pykx.** Install Python 3 and pykx (it brings pandas and
   numpy):
   ```
   pip install pykx
   ```
3. **Settings.** In `Phase1/AB/`, copy `local_settings.py.example` to
   `local_settings.py` and fill it in. `local_settings.py` is never
   committed. A setting the script does not know is an error, so a typo
   fails loudly.

   | Setting | What it is |
   |---|---|
   | `EQUITY_MASTER_SERVER` | `host:port` of equity_master (it also serves the tick ladders) |
   | `QATT_SERVER` | `host:port` of the qatt **HDB** (past days, used with `--date`) |
   | `QATT_RDB_SERVER` | `host:port` of the qatt **RDB** (today) |
   | `QUOTE_SERVER` | `host:port` of the process with the `quote` table, for `--date` runs. Blank: the qatt HDB |
   | `QUOTE_RDB_SERVER` | the same, for today's runs. Blank: the qatt RDB |
   | `EXPORT_DIR` | where the zip, and the day's working folder, are written |
   | `CROSSCODE_PATH` | the CrossCode CSV on AB |
   | `KDB_TIMEZONE` | optional, default `China Standard Time`: the clock kdb stamps prints in |
   | `SYM_CHUNK`, `MASTER_CHUNK` | optional, default 200 and 5000: how many stocks per kdb read |

   The `quote` table is **not** on the qatt RDB. Set `QUOTE_RDB_SERVER` (and
   `QUOTE_SERVER`) to the process that has it, or the run stops at step 2
   and names the setting.
4. **Check the install** (no kdb needed):
   ```
   cd Phase1\AB
   python extract.py --self-test
   ```
   It ends with `all checks passed`.

---

## 2. Set up Nova (once)

Nova runs R 3.2.2. Nothing is installed there beyond R and dplyr.

1. **Copy the code.** Copy the whole `Phase1/Nova/` folder to the Nova PC,
   for example `X:\path\to\phase1\`. Keep `config\` beside the scripts;
   `tests\` is only needed for the self-tests.
2. **Check R:**
   ```
   "C:\path\to\R-3.2.2\bin\x64\Rscript.exe" -e "R.version.string; packageVersion('dplyr')"
   ```
   The jobs are written for R 3.2.2 with dplyr 0.5.0.
3. **Settings.** Copy `settings.r.example` to `settings.r` in the same folder
   and fill it in. Forward slashes work.

   | Setting | What it is |
   |---|---|
   | `CROSSCODE_PATH` | Nova's CrossCode CSV |
   | `OUTPUT_DIR` | the Historical folder; each stock gets a folder here |
   | `NOTRADINGDAY_DIR` | where the `NoTradingDay <Country>.csv` files live |
   | `LOG_DIR` | every job appends to `LOG_DIR\phase1-YYYYMMDD.log` |
   | `KDB_TIMEZONE` | must match the zip's (default `China Standard Time`) |
   | `LULD_OUT_TEMP` | `limitUpDown.csv` is written here first |
   | `LULD_OUT_TEST`, `LULD_OUT_PILOT`, `LULD_OUT_PROD` | where each environment reads `limitUpDown.csv`. An environment asked for with a blank path fails the job |
   | `TD_OUTPUT_PATH` | the full path of `TradingData.csv` |
   | `MSCI_MAPPING_PATH`, `OPEN_AUCTION_OVERRIDE_PATH`, `HKEX_CAS_LIST_PATH`, `INDIA_NSE_CAS_LIST_PATH`, `INDIA_BSE_CAS_LIST_PATH` | optional inputs to TradingData; `""` leaves those columns blank |
   | `WORKERS` | optional: R processes writing tick files (default: cores − 1, at most 4) |
   | `TICK_BLOCK` | optional: lines of ticks read at a time (default 500000) |

4. **The launcher.** Open `run_phase1.cmd` in a text editor and set `RSCRIPT`
   on its first `set` line to R 3.2.2's `Rscript.exe`.
5. **Check the install** (optional, writes only to a temp folder):
   ```
   Rscript common.r --self-test
   Rscript historical.r --self-test
   Rscript limit_up_down.r --self-test
   Rscript trading_data.r --self-test
   ```

---

## 3. The daily run

### Step 1: extract on AB (after 18:30 HKT)

Wait until every market has closed. India's closing session ends at
18:30 HKT. Then:

```
cd Phase1\AB
python extract.py --log C:\path\to\logs\extract.log
```

With no argument, the day is **today**, read from the RDB. The extract goes
market by market. Each market's files are saved as soon as they are done in
`EXPORT_DIR\phase1-YYYYMMDD\`. At the end the zip is built:

```
EXPORT_DIR\phase1-YYYYMMDD.zip
```

The log ends with a table per market: stocks, stocks with ticks, closes
from a closing print (`qatt`), closes from the last trade (`last-trade`: it
traded, but no print carried a close code), closes from equity_master
(`no-trades`: it did not trade; `none-before-cutoff`: it traded only after
its market's `LastTradeBefore`), and quote-only names. For Korea's KOSDAQ
(`KQ`) the last trade is the last print at or before 14:30:00 HKT (15:30 in
Seoul), so after-market prints are not a close; that time is the
`LastTradeBefore` column of `Phase1\AB\config\close_conditions.csv`, in HKT,
settable for any market (blank = the day's last print). A closing print
always wins over it. A market with mostly
`last-trade` means its close condition codes are wrong, so look at it
before using the zip.

**If it is interrupted,** run the same command again: finished markets are
skipped.

Other ways to run it:

| Command | What it does |
|---|---|
| `python extract.py --date 2026-09-25` | that day, from the HDB (a past day) |
| `python extract.py --date 2026-09-25 --rdb` | that day, from the RDB (e.g. just after midnight, while the RDB still holds it). Check the `RDB date` line in the log: if it differs from the date asked, do not use that zip |
| `python extract.py --market=NZE-MAIN` | redo only those markets (FidessaMarket names or Bloomberg codes, e.g. `"NZ\|HKG-MAIN"`, in quotes). The zip is rebuilt once every market is done |
| `python extract.py --fresh` | forget the day's working folder and start over |
| `python extract.py --for "luld\|td"` | a zip for `limit_up_down.r` and `trading_data.r` only: no ticks read, so much faster. `luld`, `td`, `ticks` in any mix; all three without `--for` |

The zip is named for what it is for: `phase1-YYYYMMDD.zip` for all three
jobs, else `phase1-YYYYMMDD-luld.zip`, `phase1-YYYYMMDD-luld-td.zip`,
`phase1-YYYYMMDD-td-ticks.zip` and so on. Its closes are the same either
way. A `--for ticks` run after a `--for "luld|td"` run of the same day
reads only the ticks it still lacks.

A no-date run on a weekend stops (use `--date`). A day with no prints at all
stops without a zip.

### Step 2: copy the zip to Nova

Copy `EXPORT_DIR\phase1-YYYYMMDD.zip` from AB to Nova, anywhere, e.g.
`X:\path\to\phase1\exports\`. Only the zip is needed. The working folder
`phase1-YYYYMMDD\` stays on AB and can be deleted once the zip exists.

### Step 3: run the three jobs on Nova

```
X:\path\to\phase1\run_phase1.cmd X:\path\to\phase1\exports\phase1-YYYYMMDD.zip
```

or drag the zip onto `run_phase1.cmd`. It runs, in order, each job the
zip was made for (all three for `phase1-YYYYMMDD.zip`; for a `--for` zip
the others are skipped and the window says so):

| Job | Writes |
|---|---|
| `historical.r` | `OUTPUT_DIR\<stock>\raw-<stock>-YYYYMMDD.csv` for every stock that traded; a one-line file for a stock that only quoted; a row in `NoTradingDay <Country>.csv` for a stock with neither |
| `limit_up_down.r` | `limitUpDown.csv` for the first cutoff (Japan, Korea), with `LimitDate` the next weekday, copied to Test, Pilot and Prod |
| `trading_data.r` | `TradingData.csv` |

It stops at the first job that fails, and the window stays open. The log is
`LOG_DIR\phase1-YYYYMMDD.log`: `..` is information, `ok` a step done, `!!`
something to look at (the run goes on), `XX` a failure (the job stops).

To publish `limitUpDown.csv` to some environments only, add them, quoted:

```
run_phase1.cmd X:\path\to\phase1-YYYYMMDD.zip "Test"
run_phase1.cmd X:\path\to\phase1-YYYYMMDD.zip "Test|Prod"
```

---

## 4. Running one job, or one market

Each job can run on its own with the same zip. LimitUpDown and TradingData
only read the zip, not the tick files:

```
"C:\path\to\R-3.2.2\bin\x64\Rscript.exe" X:\path\to\phase1\historical.r    <zip>
"C:\path\to\R-3.2.2\bin\x64\Rscript.exe" X:\path\to\phase1\limit_up_down.r <zip> "Test"
"C:\path\to\R-3.2.2\bin\x64\Rscript.exe" X:\path\to\phase1\trading_data.r  <zip>
```

Historical for some markets only (FidessaMarket names or Bloomberg codes):

```
"C:\path\to\R-3.2.2\bin\x64\Rscript.exe" X:\path\to\phase1\historical.r <zip> --market=NZE-MAIN
"C:\path\to\R-3.2.2\bin\x64\Rscript.exe" X:\path\to\phase1\historical.r <zip> "--market=NZE-MAIN|HKG-MAIN"
```

**Reruns are safe.** A tick file that already exists is never rewritten, so a
rerun after an interruption only writes what is missing, and NoTradingDay
rows are never added twice. The flip side: to rewrite a wrong tick file,
delete it first.

**Old zips:** `limit_up_down.r` refuses a zip whose LimitDate is already
past, and `trading_data.r` one more than 4 days old. To backfill an old day,
run `historical.r` alone.

---

## 5. What to check on the first real days

1. **The clock of qatt's `tradeTime`.** Phase1 assumes kdb stamps
   `tradeTime` in `KDB_TIMEZONE` (HKT) and shifts each market to its own
   clock on Nova. If `tradeTime` turns out to be the exchange's local time,
   every non-HK market would be shifted twice. Check one Tokyo stock: its
   first print in `raw-7203 JP-YYYYMMDD.csv` should be at 09:00:00, the
   Tokyo open. If it shows 10:00:00, tell the developer before using the
   files.
2. **The time column.** `TIME_FIELD` in `Phase1/AB/qattsource.py` is
   `tradeTime`. Phase0's `qatt_time_probe.py` confirms it on a real day.
3. **Close codes.** In the AB log's summary table, most of each market's
   stocks with ticks should be under `qatt` (a closing print). A market
   with mostly `last-trade` needs its codes fixed in
   `Phase1/AB/config/close_conditions.csv`: its stocks are closing at their
   last trade instead of at the closing print.
4. **Compare the outputs.** Run once with `"Test"` only, and compare
   `limitUpDown.csv` and `TradingData.csv` with the production files of the
   same day.

---

## 6. Known messages and what to do

| Message | Cause | What to do |
|---|---|---|
| AB `XX the quote table is not on QUOTE_RDB_SERVER (...)` | `quote` lives on another kdb process | set `QUOTE_RDB_SERVER` / `QUOTE_SERVER` |
| AB `!! staged folder was built from ...; starting the day fresh` | the working folder came from another source, date or CrossCode | nothing: it restarts by itself |
| AB `XX qatt returned no prints at all for <day>` | the RDB has rolled to the next day, or a holiday | rerun with `--date <day>` |
| Nova `settings.r does not exist` | step 2.3 not done | copy `settings.r.example` to `settings.r` |
| Nova `!! ... no TimeZone for <venue>` | the CrossCode's FidessaMarket is not in `config\hist_markets.csv` | add the venue there, then `historical.r <zip> --market=<venue>` |
| Nova `XX` on a zip count or unzip | the zip is damaged or incomplete | copy it again from AB |
| Nova `XX this zip was made for luld\|td; historical.r needs a zip made with --for ticks` | the zip was made with `--for` without that job's use | on AB, `extract.py --for ticks` (with the same `--date`), and run that zip |
| Nova `XX` from `limit_up_down.r` about a blank environment path | an environment asked for has no path in `settings.r` | fill the path, or pass only the environments you have |

## 7. Tests

The whole chain can be checked on this machine, with kdb simulated and the
real R jobs:

```
python Phase1/tests/integration.py          # correctness, about 2 minutes
python Phase1/tests/integration.py --big    # plus timings on ~3M prints
```

It ends with `all checks passed`.
