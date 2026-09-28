# Phase1 AB: the daily extract for Nova

`extract.py` runs on AB, the machine with kdb. Each evening it writes one zip,
`phase1-YYYYMMDD.zip`, holding everything Nova needs to build the Historical
tick files, `limitUpDown.csv` and `TradingData.csv` without B-PIPE. A person
copies the zip to Nova, where `Phase1/Nova/run_phase1.cmd <zip>` does the rest.

```
AB (Python, after 18:00 HKT)          copied by hand        Nova (R 3.2.2)
extract.py -> phase1-YYYYMMDD.zip  ---------------------->  run_phase1.cmd <zip>
```

## Setup

1. Python 3.13 with `pykx` (it brings pandas and numpy):

   ```
   pip install pykx
   ```

2. Copy `local_settings.py.example` to `local_settings.py` in this folder
   and fill it in. `local_settings.py` is gitignored. A name the scripts do
   not know is an error, so a typo fails loudly.

   | setting | what |
   |---|---|
   | `EQUITY_MASTER_SERVER` | `host:port` of equity_master (and the tick ladders) |
   | `QATT_SERVER` | `host:port` of the qatt **HDB**, read with `--date` |
   | `QATT_RDB_SERVER` | `host:port` of the qatt **RDB**, read for today |
   | `EXPORT_DIR` | where the zip is written, e.g. `C:\path\to\phase1_export` |
   | `CROSSCODE_PATH` | `NewCrosscode.csv` on this machine |
   | `KDB_TIMEZONE` | optional, default `China Standard Time`; the zone kdb stamps prints in, copied to the manifest |
   | `SYM_CHUNK`, `MASTER_CHUNK` | optional, default 200 and 5000; syms per qatt read, codes per equity_master read |

3. Check that the install works, with no kdb:

   ```
   python extract.py --self-test
   ```

   It ends with `all checks passed`. Every other module here has its own
   `--self-test` too.

## Running it

```
python extract.py                            today, from the RDB
python extract.py --date 2026-09-25          that day, from the HDB
python extract.py --log C:\path\to\logs\extract.log
python extract.py --fresh                    the day again, from scratch
```

- **No argument:** the trade date is today, and qatt and quote are read from
  the RDB (`QATT_RDB_SERVER`).
- **`--date YYYY-MM-DD`:** the newest qatt partition on or before that
  date, from the HDB (`QATT_SERVER`). Use it to redo a past day.
- **`--log FILE`:** also appends the log to FILE. Each run starts with a
  `=== YYYY-MM-DD HH:MM:SS ===` line.
- **`--fresh`:** deletes the day's staging folder first, so everything is
  read again (see below). Combine it with `--date` for a past day.
- equity_master and the tick ladders always come from `EQUITY_MASTER_SERVER`,
  at the newest equity_master date on or before the trade date.

**When:** after 18:00 HKT, once every market in the universe has closed and
its closing trades are in qatt. A run before a market closes finds no
closing trade for it, and every name there falls back to equity_master.

The exit status is 0 when the zip was written, 1 when the run stopped, and 2
for a bad argument or setting.

## Market by market, and resuming

The day is built in a staging folder, `EXPORT_DIR/phase1-YYYYMMDD/` (the
trade day), one file at a time, each as soon as it is ready:

1. `master.csv`, `equity.csv`, `ladders.csv`, once each;
2. then, market by market in code order, `ticks-<MKT>.csv`,
   `closes-<MKT>.csv` and `quote_only-<MKT>.csv`. A market is the Bloomberg
   exchange code of a sym's primary CrossCode row (equity_master's
   `EQY_PRIM_EXCH_SHRT`), so `7203 JT` and `7203 JE` are both in `JT`. A big
   market is still read `SYM_CHUNK` syms at a time;
3. then the zip is assembled from the folder. The folder is left in place.

Every file is written as `<name>.part` and renamed when complete. A market's
ticks file is renamed last, so a market counts as done only when all three
of its files exist.

**If a run stops** (kdb drops, the machine restarts, Ctrl+C), run the same
command again. The rerun reads `master.csv` and `equity.csv` back instead of
asking equity_master, skips every market already done (the log says
`HK done already, skipped`), redoes the market it stopped in, and carries on.
Leftover `.part` files are simply overwritten. The zip is the same as an
uninterrupted run would have written.

**`--fresh`** deletes the day's folder and starts over. Use it when the
CrossCode changed, or after a no-argument run made too early: the RDB keeps
filling during the day, and a rerun of today would otherwise keep the
markets an earlier run already finished.

## What the zip contains

`EXPORT_DIR/phase1-YYYYMMDD.zip`. Every member is CSV, UTF-8, with a header.

| member | columns | what |
|---|---|---|
| `manifest.csv` | `key,value` | `date`, `source` (`rdb`/`hdb`), `equity_master date`, `time column`, `kdb timezone`, `syms asked`, `prints`, `syms with prints`, `quote only`, `closes from qatt`, `closes from equity_master`, `no close`, `exported at` |
| `master.csv` | `BloombergCode,sym,EQY_PRIM_EXCH_SHRT,COMPOSITE_EXCH_CODE,ID_MIC_PRIM_EXCH` | each CrossCode code's equity_master row: the link from `7203 JT` to the qatt sym `7203.JP` |
| `ticks.csv` | `sym,time,price,size,cond,ex` | every qatt print, each sym's prints together, time `HH:MM:SS` in **kdb's clock**; several conditions are joined with `@`, an empty one is `#N/A N.A.` |
| `closes.csv` | `sym,close,source,reason` | one row per sym asked; see below |
| `quote_only.csv` | `sym,time,bid,ask,cond` | a sym with no print but a quote: its last quote, `cond` the first close code of its market |
| `equity.csv` | `BloombergCode,sym,PX_LAST,EQY_BETA,volatility,REL_INDEX,CUR_MKT_CAP,fx_last,ID_ISIN,INDUSTRY_SECTOR,LONG_COMP_NAME` | equity_master's fields per CrossCode code |
| `ladders.csv` | `BloombergCode,sym,price,ticksize` | the raw tick ladder per CrossCode code; kdb's `price` is an exclusive upper bound, and R converts it |

For `equity.csv` and `ladders.csv` each code tries, in order, India's
suffixes (`.IS` then `.IN` for NSE, `.IN` for BSE), `ticker.ext`,
`ticker.composite`, then the sym equity_master resolved it to. The `sym`
column says which one was found. A code none of them finds has no row.

`ticks.csv`, `closes.csv` and `quote_only.csv` are the markets' files joined
under one header, in market order, and each sym's prints are still
together. The zip is written as `phase1-YYYYMMDD.zip.part` and renamed at
the end, so a zip under its real name is always complete. A run that stops
leaves no zip, only what it staged.

## The close

A sym's close is the price of its **last** qatt print whose condition carries
one of its market's close codes, from `config/close_conditions.csv`. The
market is the Bloomberg exchange code of each CrossCode row that resolves to
the sym, so `7203 JT` and `7203 JE` pool their codes. It is the last match
because Japan's `e` also marks the morning close.

Without such a print, the close is equity_master's `PX_LAST`, and
`closes.csv` says why:

| reason | meaning |
|---|---|
| `no-trades` | no qatt print at all that day |
| `no-closing-trade` | prints, but none with a close code |
| `no-close-codes` | the market has no row in `close_conditions.csv` |

A `PX_LAST` that is null or `<= 0` does not count. A sym with neither kind of
close is written `sym,,,no-close`.

| `source` | `reason` | close |
|---|---|---|
| `qatt` | blank | the closing print |
| `equity_master` | one of the three above | `PX_LAST` |
| blank | `no-close` | blank |

## Reading the log

Each line is `HH:MM:SS  lvl  text`. `..` is commentary, `ok` a stage that
finished well, `!!` something a person should look at (the run goes on), and
`XX` a failure (the run stops, no zip).

The run goes through numbered steps: 1 crosscode, 2 qatt (the day, the
columns and the staging folder), 3 equity_master (the date and how many
codes it matched), 4 reference data, 5.1, 5.2, ... one per market, and 6
the zip. A market's step looks like this:

```
..  --- 5.3. market NZ, 1 syms -----------------------------------
..  read 1  1/1 syms  2 prints  0.0s
..  no print                0   0 with a quote
!!  close  AIA.NZ  NZ  no-closing-trade  -> equity_master 6.13
..  NZ done                 1 syms   2 prints, qatt 0, equity_master 1, no close 0, quote-only 0
```

A market finished by an earlier run shows `NZ done already, skipped`
instead, and its `!!` lines are in that earlier run's log. On a rerun,
steps 3 and 4 say `read back from master.csv` / `equity.csv`.

The `!!` lines to expect:

```
!!  close  AIA.NZ  NZ  no-closing-trade  -> equity_master 6.13
!!  close  8889.HK  HK  no-trades  -> no close, PX_LAST null or <= 0
!!  quote  QQQ.XX  XX  no close codes for its market, quote-only row skipped
!!  no tick ladder for any of 41,208 codes (...); ladders.csv is empty
!!  200 syms was too much for qatt (...); reconnecting and asking 100 at a time
```

There is one `close` line per fallback: the sym, its exchange codes, the
reason, and the price taken or `no close`. The run ends with a table per
market, counted from the staged files, so it covers the markets a rerun
skipped too:

```
market      syms   ticks    qatt  no-trades  no-closing-trade  no-close-codes  no-close  quote-only
HK             2       0       0          1                 0               0         1           1
JT             1       1       1          0                 0               0         0           0
all            3       1       1          1                 0               0         1           1
```

- `syms`: syms counted under that code (its primary exchange when the
  CrossCode carries it);
- `ticks`: syms with at least one print;
- `qatt`: closes from a closing print;
- `no-trades`, `no-closing-trade`, `no-close-codes`: closes taken from
  equity_master, by reason;
- `no-close`: no close at all;
- `quote-only`: syms written to `quote_only.csv`.

A market whose `no-closing-trade` equals its `ticks` has closing trades
under a code that is not in `close_conditions.csv`.

The run stops with `XX` and writes no zip when kdb fails, when qatt has no
partition on or before `--date`, or when equity_master returns no row at all
for the reference fetch.

## Before the first live run

1. **The time column.** `qattsource.TIME_FIELD` ships as `tradeTime`, a
   guess from its name. Check it with Phase0's probe,
   `Phase0/AB/Historical/qatt_time_probe.py` (it is not copied here; it
   reads `QATT_SERVER` from its own `local_settings.py`):

   ```
   python qatt_time_probe.py 7203.JP --session 09:00-15:00
   ```

   If it names another column, set `TIME_FIELD` in `Phase1/AB/qattsource.py`
   to it. The manifest's `time column` records what each zip used.

2. **The close codes.** Run once with `--date` on a normal trading day and
   read the summary. Every market should have most of its `ticks` in `qatt`.
   A market with a large `no-closing-trade` count has closing trades whose
   condition is not in `config/close_conditions.csv`: look at that market's
   last prints in `ticks.csv` and compare their `cond` with the file.
