# QattTransfer — the previous day's qatt, carried to another machine

Two processes, on two machines:

```
machine with kdb                              machine with the tick store
────────────────                              ───────────────────────────
qatt_export.py                                qatt_dispatch.py qatt-20260924.zip
  NewCrosscode.csv ─┐                           NewCrosscode.csv ─┐
  equity_master ────┼─► qatt-20260924.zip ───►  composites.csv ───┼─► OUTPUT_DIR/<code>/raw-<code>-20260924.csv
  qatt (one day) ───┘      one file             markets.csv ──────┘
```

The files it produces are the ones `../Historical/historical_ticks.py`
writes: same folders, same names, same CSV.

## 1. `qatt_export.py` — on the kdb machine

```
python qatt_export.py                     the previous day
python qatt_export.py --date 2026-09-22   the newest qatt day on or before this
python qatt_export.py --log export.log
```

- **The previous day** is the newest qatt partition before today. A Monday
  run exports Friday, and a run after a holiday exports the last trading day.
- **The universe is `NewCrosscode.csv`.** Every code is resolved to a qatt
  sym the way historical_ticks does it: `equity_master` in three passes,
  with `config/markets.csv` as the fallback. Only those syms are read.
- **One zip, `EXPORT_DIR/qatt-YYYYMMDD.zip`:**

  | member | holds |
  |---|---|
  | `ticks.csv` | `sym,time,price,size,cond,ex`, every print, in **kdb's clock** |
  | `master.csv` | the `equity_master` rows for the crosscode codes |
  | `manifest.csv` | the day, kdb's time zone, the time column, counts |

  `master.csv` makes the trip because the other machine has no kdb.
  qatt is keyed on the composite (`7203.JP`) and the crosscode on the
  primary listing (`7203 JT`), and `equity_master` is the only link between
  them.

Zipped CSV rather than Parquet. We measured both on tick-shaped data at 1M,
3M and 10M prints: Parquet was a steady ~27% smaller at every size. The gap
doesn't grow with volume, and CSV needs nothing beyond the standard library
on the dispatch machine.

## 2. `qatt_dispatch.py` — on the tick-store machine

```
python qatt_dispatch.py D:\drop\qatt-20260924.zip
python qatt_dispatch.py D:\drop\qatt-20260924.zip --log dispatch.log
```

- **Names each file** from `NewCrosscode.csv`, `config/composites.csv` and
  the zip's `master.csv`, using `universe.build`, the same code
  historical_ticks uses. `7203 JT`, `JE` and `JI` make one `7203 JP` file,
  and `RIO AT` is written as `RIO AU`.
- **Converts the clock** from kdb's zone (from the manifest) into each
  market's own zone (from `config/markets.csv`), per date, so daylight
  saving is followed.
- **Creates the folder** when a name has none.
- **Skips a file that already exists.** A second run of the same zip writes
  nothing.
- A name with no prints gets no file. A sym in the zip that this crosscode
  doesn't carry is counted and dropped.

## Settings

Each machine has its own `local_settings.py` and fills in only what its
script uses. `CROSSCODE_PATH` has the same name on both machines and holds a
different path on each.

| setting | export | dispatch |
|---|---|---|
| `EQUITY_MASTER_SERVER`, `QATT_SERVER` | ✓ | |
| `EXPORT_DIR` | ✓ | |
| `CROSSCODE_PATH` | ✓ | ✓ |
| `OUTPUT_DIR` | | ✓ |
| `KDB_TIMEZONE`, `SYM_CHUNK`, `MASTER_CHUNK` | optional | |

```
copy local_settings.py.example local_settings.py
pip install pykx       export machine only
pip install tzdata     dispatch machine (Windows has no tz database)
```

## Checks

```
python qatt_export.py --self-test      python qatt_dispatch.py --self-test
python settings.py --self-test
```

`qatt_dispatch.py --self-test` builds a real zip with the export's writer
and dispatches it into a temp folder. It checks the collapse, the composite
rename, the Tokyo and Sydney clocks, the skip, and the refusal of a zip whose
sym rows are split apart.

## Copied from Historical

`crosscode.py`, `marketcfg.py`, `universe.py`, `ticksfile.py`,
`qattsource.py`, `logs.py` and `config/` are copies of `../Historical`'s, so
this folder stands on its own. **A change made there has to be made here
too.** The one that matters most is `qattsource.TIME_FIELD`: once
`qatt_time_probe.py` settles it, set it in both folders.
