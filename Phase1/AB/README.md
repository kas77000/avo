# Phase1 AB: the daily extract for Nova

`extract.py` runs on AB, the machine with kdb. Each evening it writes one zip,
`phase1-YYYYMMDD.zip`, holding everything Nova needs to build the Historical
tick files, `limitUpDown.csv` and `TradingData.csv` without B-PIPE. A person
copies the zip to Nova, where `Phase1/Nova/run_phase1.cmd <zip>` does the rest.

```
AB (Python, after 18:30 HKT)          copied by hand        Nova (R 3.2.2)
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
   | `QUOTE_SERVER` | optional, `host:port` of the process with the `quote` table, used with `--date`; blank means `QATT_SERVER` |
   | `QUOTE_RDB_SERVER` | optional, the same for today's run; blank means `QATT_RDB_SERVER` |
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
python extract.py --date 2026-09-25 --rdb    that day, read from the RDB
python extract.py --log C:\path\to\logs\extract.log
python extract.py --fresh                    the day again, from scratch
python extract.py --market "NZ|HK"           only those markets, read again
python extract.py --for "luld|td"            only what limit_up_down.r and
                                             trading_data.r read: no ticks
```

- **No argument:** the trade date is today, and qatt and quote are read from
  the RDB (`QATT_RDB_SERVER`). On a Saturday or a Sunday it stops at once
  with `XX`: use `--date YYYY-MM-DD` for the day you want.
- **`--date YYYY-MM-DD`:** the newest qatt partition on or before that
  date, from the HDB (`QATT_SERVER`). Use it to redo a past day.
- **`--date YYYY-MM-DD --rdb`:** the zip and the staging folder are named
  for that date, but qatt and quote are read from the **RDB**
  (`QATT_RDB_SERVER`, `QUOTE_RDB_SERVER`, blank meaning qatt's) with the
  undated queries, exactly as a no-argument run reads them. There is no
  partition list and no weekend stop. Use it when the day is over but not
  yet in the HDB, for example just after midnight while the RDB still holds
  it, or when a no-argument run was missed.

  **The risk:** the RDB holds one day, whatever day that is, and those
  prints are filed under the date you gave. The run asks the RDB for its
  `.z.D` and logs it:

  ```
  ..  RDB date                2026-09-25   asked for 2026-09-25
  !!  the RDB is on 2026-09-26, not 2026-09-25: the prints are whatever day the RDB holds, filed under 2026-09-25
  ```

  A mismatch does not stop the run. Read the `!!` line: if the RDB has
  already rolled over, do not copy that zip to Nova. Run `--date` without
  `--rdb` once the HDB has the day; it starts the day fresh and replaces
  the zip. If the RDB holds no print at all, the run stops with
  `XX` (no zip) and says to read the HDB instead. Its staging is marked
  `rdb`, so a later `--date` run of the same day (HDB) starts that day
  fresh, and a rerun of the same `--date --rdb` resumes.
- **`--rdb` alone** is the same as no argument.
- **`--market NAMES`:** one market, or several joined by `|` (quote it:
  `--market "TYO-MAIN|HK"`), in any case, as in Nova's
  `historical.r --market`. A name is a Bloomberg exchange code of
  `close_conditions.csv` (`NZ`), or a FidessaMarket (`NZE-MAIN`), which
  stands for the exchange codes of the universe's CrossCode rows on that
  market. A name that is neither stops the run with `XX` before kdb is
  asked anything, and the message says both were tried. Only those markets
  are read, and each is **read again** even if already staged: its three
  files are set aside first (the ticks file first, so a crash leaves the
  market "not done"). The other markets are not touched. Everything before
  the markets (the universe, master, equity, px, ladders and `stage.csv`)
  still covers the full universe, so `--market` never restarts a staged
  day. It combines with `--date`, `--rdb` and `--fresh`. With `--fresh`,
  every market is reset and only the selected ones are read. Step 1 logs
  the selection, and what a FidessaMarket resolved to:

  ```
  ..  --market                NZ|HK   2 of 18 markets this run
  ..  --market                NZE-MAIN -> NZ   1 of 18 markets this run
  ```

  **No zip until every market is staged.** After the markets, the zip is
  written only if every market of the universe has its three files. If any
  are missing, the run ends (exit 0) with

  ```
  ..  16 markets still to do (AT, C1, ...); no zip yet - run without --market to finish them
  ```

  and a run without `--market` reads just those and writes the zip. On a
  fully staged day, `--market` re-reads those markets and rebuilds the
  zip, replacing the day's zip.
- **`--for USES`:** what the zip is for, one or more of `luld`
  (`limit_up_down.r`), `td` (`trading_data.r`) and `ticks`
  (`historical.r`), joined by `|` (quote it), in any case. Without it, all
  three, as before. The zip holds only what those jobs read, plus
  `manifest.csv`:

  | use | members |
  |---|---|
  | `luld` | `master.csv`, `closes.csv`, `equity.csv`, `ladders.csv` |
  | `td` | `master.csv`, `closes.csv`, `equity.csv` |
  | `ticks` | `master.csv`, `ticks.csv`, `closes.csv`, `quote_only.csv` |

  It is named for them, in the order luld, td, ticks:
  `phase1-YYYYMMDD-luld.zip`, `phase1-YYYYMMDD-luld-td.zip`,
  `phase1-YYYYMMDD-td-ticks.zip`; with all three it stays
  `phase1-YYYYMMDD.zip`. The manifest's `for` says which (`luld|td`), and
  step 1 logs it:

  ```
  ..  --for                   luld|td   -> phase1-YYYYMMDD-luld-td.zip
  ```

  **Without `ticks`, no print is read and no quote.** Each market's closes
  come from one short query per chunk of syms, a row per sym and
  condition: the highest price in that condition's latest second.
  The close rule then picks from those rows exactly as it does from the
  ticks, so the closes are the same as a full run's. Ladders are fetched
  only for `luld`. `equity.csv` and `px.csv` come from the same
  equity_master query as always, since the `no-trades` closes need
  `PX_LAST`. A market is done for `luld`/`td` once its `closes-<MKT>.csv`
  is staged, and for `ticks` once all three of its files are. So a
  `luld|td` run after a full run reuses its closes and reads nothing. A
  `ticks` run after a `luld|td` run reads, the full way, the markets that
  lack ticks, and rewrites their closes. `--market` redoes only what this
  run's uses need; with `--for luld` it rewrites `closes-<MKT>.csv` and
  leaves that market's ticks alone. The zip is built once every market is
  done for the uses asked.
- **`--log FILE`:** also appends the log to FILE. Each run starts with a
  `=== YYYY-MM-DD HH:MM:SS ===` line.
- **`--fresh`:** moves the day's staging folder aside first, so everything
  is read again (see below). Combine it with `--date` for a past day.
- equity_master and the tick ladders always come from `EQUITY_MASTER_SERVER`,
  at the newest equity_master date on or before the trade date.

**When:** after 18:30 HKT, once every market in the universe has closed and
its closing trades are in qatt. The last is India, whose closing session
runs 15:30-16:00 IST, 18:00-18:30 HKT. A run before a market closes finds no
closing trade for it: every name there closes at its last trade so far.

**A run that fails early still leaves its finished markets staged**, and a
rerun the same evening reuses them as they are. If the first run was too
early, rerun with `--fresh`.

**Run it the same evening.** The RDB holds today only, and after kdb's
midnight rollover it holds the new day. A no-argument run then finds no
print at all and stops:

```
XX  qatt returned no prints at all for 2026-09-29 on QATT_RDB_SERVER (kdb-host:5012); no zip written. After midnight the RDB holds the new day: use --date 2026-09-29
```

To redo a day the next morning, use `--date` with that day.

The exit status is 0 when the zip was written, 1 when the run stopped, and 2
for a bad argument or setting.

## The universe

The universe is the CrossCode (`CROSSCODE_PATH`), **filtered to our
markets**. A row is kept when its Bloomberg exchange code (the last word
of `BloombergCode`, `JT` in `7203 JT`) is a `BBGCode` of
`config/close_conditions.csv`: the 18 codes
`AT C1 C2 CG CS HK IB IJ IS JT KP KQ MK NZ PM SP TB TT`. Its `Type` does
not matter: warrants, ETFs, a blank Type and the rest all stay in (only
baskets are dropped, by the CrossCode reader).

The filter runs straight after the CrossCode is read, so equity_master,
the ladders, the ticks, the closes and the quotes all cover only these rows,
and there are at most 18 markets. Rows without a BloombergCode, and
baskets, are dropped before that, as always. To cover another market, add
its row to `close_conditions.csv`; a row with a blank `CloseCondCodes`
brings the market in with every close from its last trade
(`last-trade`), or `PX_LAST` for a name that did not trade.

Step 1 of the log says what was kept and why the rest was dropped:

```
..  universe                23,639 rows   of 58,759, the exchange codes of close_conditions.csv
..  dropped                 35,120 rows   exchange code not ours: US 12,480, LN 3,011, GR 2,654, ...
```

(The numbers are made up.) The CrossCode fingerprint in `stage.csv`
covers only the kept rows, so an edit to a row outside the universe does
not restart a staged day.

## Market by market, and resuming

The day is built in a staging folder, `EXPORT_DIR/phase1-YYYYMMDD/` (the
trade day), one file at a time, each as soon as it is ready:

1. `stage.csv` (what the folder was built from, see below), then
   `master.csv`, `px.csv` (every fetched `PX_LAST`, for the closes),
   `equity.csv`, `ladders.csv`, once each;
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
command again. The rerun reads `master.csv`, `px.csv` and `equity.csv` back
instead of asking equity_master, skips every market already done (the log
says `HK done already, skipped`), redoes the market it stopped in, and
carries on. Leftover `.part` files are simply overwritten. The zip is the
same as an uninterrupted run would have written. A market whose staged
`closes-<MKT>.csv` does not hold exactly this run's syms is redone, with a
`!!` line.

**The folder only resumes a run like the one that built it.** `stage.csv`
records the source (`rdb`/`hdb`), the equity_master date and a fingerprint
of the CrossCode's BloombergCodes. If any of them differs on a later run of
the same day, for example a failed no-argument run followed next morning by
`--date` for that day, or an edited CrossCode, the log says

```
!!  staged folder was built from source rdb, equity_master date 2026-09-24; starting the day fresh
```

and the run starts the day from scratch, as with `--fresh`.

**`--fresh`** starts the day over. Use it after a no-argument run made too
early: the RDB keeps filling during the day, and a rerun of today would
otherwise keep the markets an earlier run already finished.

Starting fresh (by `--fresh` or by the check above) never deletes and
recreates the folder under the same name: on a network share (SMB) a folder
just deleted stays "delete pending" for a moment, and creating it again
fails. The old folder is renamed to `phase1-YYYYMMDD.stale-HHMMSS`, a new
one is created, and the old one is then removed if possible:

```
..  old staging folder moved to phase1-20260928.stale-190412
!!  phase1-20260928.stale-190412 could not be removed; it is harmless, delete it by hand
```

A `.stale-` folder is never read by any run; delete it whenever you like.

**Disk space:** the staging folder holds the day's ticks **uncompressed**,
several times the zip's size. It is left in place so a rerun can resume;
once `phase1-YYYYMMDD.zip` exists it is no longer needed and can be
deleted.

**A read that is too big** (kdb drops the connection) is halved, and the
smaller size is kept for the rest of the run, across markets.

## What the zip contains

`EXPORT_DIR/phase1-YYYYMMDD.zip`, or `phase1-YYYYMMDD-<uses>.zip` for a
`--for` zip, which holds only its uses' members (see `--for`). Every member
is CSV, UTF-8, with a header.

| member | columns | what |
|---|---|---|
| `manifest.csv` | `key,value` | `date`, `source` (`rdb`/`hdb`), `for` (`luld\|td\|ticks`, or what `--for` asked), `equity_master date`, `time column`, `kdb timezone`, `syms asked`, `prints` (condensed `ticks.csv` lines; blank without ticks), `syms with prints`, `quote only` (blank without ticks), `closes from qatt` (closing prints), `closes from last trade`, `closes from equity_master`, `no close` (the four add up to `syms asked`), `exported at` |
| `master.csv` | `BloombergCode,sym,EQY_PRIM_EXCH_SHRT,COMPOSITE_EXCH_CODE,ID_MIC_PRIM_EXCH` | each CrossCode code's equity_master row: the link from `7203 JT` to the qatt sym `7203.JP` |
| `ticks.csv` | `sym,time,price,size,cond,ex` | one line per sym, second, price, cond and ex, with the volume of every qatt print behind it summed (see below); each sym's lines together, in time order, time `HH:MM:SS` in **kdb's clock**; several conditions are joined with `@`, an empty one is `#N/A N.A.` |
| `closes.csv` | `sym,close,source,reason` | one row per sym asked; see below |
| `quote_only.csv` | `sym,time,bid,ask,cond` | a sym with no print but a quote: its last quote, `cond` the first close code of its market |
| `equity.csv` | `BloombergCode,sym,PX_LAST,EQY_BETA,volatility,REL_INDEX,CUR_MKT_CAP,fx_last,ID_ISIN,INDUSTRY_SECTOR,LONG_COMP_NAME` | equity_master's fields per CrossCode code |
| `ladders.csv` | `BloombergCode,sym,price,ticksize` | the raw tick ladder per CrossCode code; kdb's `price` is an exclusive upper bound, and R converts it |

For `equity.csv` and `ladders.csv` each code tries, in order, India's
suffixes (`.IS` then `.IN` for NSE, `.IN` for BSE), `ticker.ext`,
`ticker.composite`, then the sym equity_master resolved it to. The `sym`
column says which one was found. A code none of them finds has no row.

**Ticks are condensed in q.** A `ticks.csv` line is not one print: the read is

```
0!select size:sum size by sym, tradeTime:tradeTime.second, price, cond, ex from qatt where ...
```

(with the configured time column), so every print in one second at one
price, condition and exchange is one line, `size` summed. The time comes
back as a q `second` and is written exactly as before. The manifest's
`prints` counts these lines. If qatt lacks `price`, `size`, `cond` or `ex`,
the read falls back to one line per print, and step 2 of the log says why.

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
because Japan's `e` also marks the morning close. Lines run in time
order; if two close-code lines share the day's last second, the one sorted
last (the higher price) is the close.

Without such a print, in this order:

1. **It traded that day:** the close is its **last traded price**, the
   price of its last print by time (with condensed lines, the last line of
   its last second; a line with no time is never taken as the last unless
   every line lacks one). Reason `last-trade`. This also covers a market
   with no close codes (no row in `close_conditions.csv`, or a blank
   `CloseCondCodes`): its names close at their last trade.
2. **No print at all** (the market was shut, or the name suspended):
   equity_master's `PX_LAST`, reason `no-trades`. A `PX_LAST` that is null
   or `<= 0` does not count.
3. **Neither:** `sym,,,no-close`.

| `source` | `reason` | close |
|---|---|---|
| `qatt` | blank | the closing print |
| `qatt` | `last-trade` | the last traded price |
| `equity_master` | `no-trades` | `PX_LAST` |
| blank | `no-close` | blank |

Nova reads only the `close` column, whatever the source.

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
..  read 1  1/1 syms  2 lines  0.0s
..  no print                0   0 with a quote
..  NZ done                 1 syms   2 prints, closing print 0, last trade 1, equity_master 0, no close 0, quote-only 0
```

AIA.NZ traded but had no closing print, so it closed at its last trade:
that is a count on the `done` line, not a `!!` line.

A market finished by an earlier run shows `NZ done already, skipped`
instead, and its `!!` lines are in that earlier run's log. On a rerun,
steps 3 and 4 say `read back from master.csv` / `equity.csv`.

The `!!` lines to expect:

```
!!  close  8888.HK  HK  no-trades  -> equity_master 3.4
!!  close  8889.HK  HK  no-trades  -> no close, PX_LAST null or <= 0
!!  XX has no close codes in close_conditions.csv: a name that traded closes at its last trade
!!  quote  QQQ.XX  XX  no close codes for its market, quote-only row skipped
!!  no tick ladder for any of 41,208 codes (...); ladders.csv is empty
!!  200 syms was too much for qatt (...); reconnecting and asking 100 at a time
```

There is one `close` line per name that took `PX_LAST` or has no close:
the sym, its exchange codes, the reason, and the price taken or
`no close`. The run ends with a table per
market, counted from the staged files, so it covers the markets a rerun
skipped too:

```
market      syms   ticks    qatt  last-trade  no-trades  no-close  quote-only
HK             2       0       0           0          1         1           1
JT             1       1       1           0          0         0           0
all            3       1       1           0          1         1           1
```

- `syms`: syms counted under that code (its primary exchange when the
  CrossCode carries it);
- `ticks`: syms with at least one print;
- `qatt`: closes from a closing print;
- `last-trade`: names that traded but had no closing print, closed at their
  last trade;
- `no-trades`: names with no print, closed at equity_master's `PX_LAST`;
- `no-close`: no close at all;
- `quote-only`: syms written to `quote_only.csv`.

A market whose `last-trade` is most of its `ticks` has its closing trades
under a code that is not in `close_conditions.csv`: its close codes are
wrong.

The run stops with `XX` and writes no zip when kdb fails, when qatt has no
partition on or before `--date`, when equity_master returns no row at all
for the reference fetch, when qatt has no print at all for the whole day
(the staging folder is left as it is), or when a no-argument run falls on a
Saturday or a Sunday:

```
XX  today, 2026-09-26, is a Saturday: the RDB holds no trading day. Use --date YYYY-MM-DD for the day you want
```

Before any market, step 2 checks that the `qatt` table is on the qatt
connection and the `quote` table on the quote connection (`tables[]`). If
one is missing, the run stops before staging anything:

```
XX  the quote table is not on QATT_RDB_SERVER (kdb-host:5012); set QUOTE_RDB_SERVER / QUOTE_SERVER to the process that has it
```

A kdb error during a market names the market, the read and the setting,
for example `XX  market JT, quote read on QUOTE_RDB_SERVER (kdb-host:5014): QError: ...`.
Fix the cause and run again: the markets already done are kept.

A market's file name is its exchange code with anything but
`A-Z a-z 0-9 _ -` replaced by `_` (an empty code is `NONE`). Step 3 lists
the exchange codes that do not look like one (not 2 to 4 capital letters
or digits), with an example BloombergCode each:

```
!!  2 odd exchange codes, filed under a safe name: '.' (ABC .), '' (XYZ)
```

Those usually come from a malformed BloombergCode in the CrossCode.

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

2. **The quote-only time.** A quote-only line takes its time from the
   `quote` table's `time` column, while the ticks use `TIME_FIELD`
   (`tradeTime`). On the first live day, check that both are in the same
   clock: compare a quote-only sym's time with the ticks of a sym that
   traded around then.

3. **The close codes.** Run once with `--date` on a normal trading day and
   read the summary. Every market should have most of its `ticks` in `qatt`.
   A market with mostly `last-trade` has its close codes wrong: its
   closing trades carry a condition that is not in
   `config/close_conditions.csv`. Look at that market's last prints in
   `ticks.csv` and compare their `cond` with the file.
