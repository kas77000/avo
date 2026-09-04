# How a run works, end to end

Written to be checked against. Every stage says what it reads, what it
decides, what it can get wrong, and how to see it for yourself.

**Nothing here is a host or a port on a command line.** Servers and paths come
from `local_settings.py`. **Nothing outside `OUTPUT_DIR` is ever written.**

---

## The shape of it

```
CrossCode.csv ──► 1 read ──► 2 resolve ──► 3 collapse ──► 4 pick days ──► 5 fetch ──► 6 write
                             (equity_master)   (universe)     (partitions)   (qatt)     CSV + miss cache
```

Six stages, two kdb servers, one output directory. A run does the whole
universe; `--trace` does one name and prints every stage.

---

## 1. Read the crosscode

**Reads** `CROSSCODE_PATH`.

Two columns are load-bearing — `BloombergCode` and `FidessaMarket` — and a
file missing either is refused by name. Everything else (`RicCode`, `Type`,
`Currency`, `BloombergSecurityType`, `BloombergStatus`, and the first column
under **either** spelling `#FidessaCode` or `FidessaCode`) is a diagnostic,
read when present and reported when absent.

> That tolerance is deliberate. The two crosscode readers already in this repo
> disagree — TradingData wants `#FidessaCode`, LimitUpDown v1/v2 want
> `FidessaCode` — and every column they differ on is one this job never uses.

**Can get wrong:** a row with no `BloombergCode` is dropped (nothing to
resolve it with) and named in the log.

**Check it:** the log's first line is the row count and the path.

---

## 2. Resolve every code to a qatt sym

**Reads** `equity_master` on `EQUITY_MASTER_SERVER`. **One round trip per
`MASTER_CHUNK` codes**, never one per name.

The date is rolled back to the newest `equity_master` row on or before today,
because `.z.D-1` lands on a Sunday every Monday and on every holiday.

Then **three passes**, each run only on what the last could not answer:

| pass | matches | example |
|---|---|---|
| 1 | `sym_bpipe` | `7203.JT` |
| 2 | `sym_mbpipe` | `7203 JT EQUITY` |
| 3 | `sym` | `600000.CH` |

The third exists for China: a Shanghai line's exchange code is `C1` and its
composite is `CH`, and both `equity_master` and `qatt` key it on the
composite — so `600000.C1` matches nothing while `600000.CH` matches.

**Can get wrong:** a name in none of the three passes falls back to building
`<ticker>.<BBGComposite>` from `config/markets.csv`, and if that market is
unlisted the name **drops out entirely** and is named in the log.

**Check it:** `matched N of M (sym_bpipe …, sym_mbpipe …, sym …)`. If pass 3
is carrying nearly everything, passes 1 and 2 are looking at the wrong
columns.

---

## 3. Collapse, rename, filter

Pure — no I/O. Three things happen, all needing stage 2's answer.

**Collapse.** `7203 JT`, `7203 JE` and `7203 JI` are three crosscode rows
(Tokyo, JNX, Chi-X Japan) and **one** `7203.JP` in qatt. They make one file,
not three. Missing this writes the same file three times a run, each write
racing the last, with plausible tick counts in every one.

**Name.** The file takes the **primary listing** — the row whose exchange code
equals `EQY_PRIM_EXCH_SHRT` — so Toyota is `raw-7203 JT-…`, never `JE`, never
the composite `JP`.

**Rename, for China only.** Driven by the MIC, not the Fidessa market:

| MIC | file gets |
|---|---|
| `XSHG` Shanghai | `<ticker> CG` |
| `XSHE` Shenzhen | `<ticker> CS` |

So one Chinese stock is spelt three ways, and all three are correct:

```
600000 C1     in the crosscode
600000.CH     the only shape equity_master and qatt answer to
600000 CG     on disk, which is what the consumer reads
```

**Filter.** `EXCLUDED_MICS` is **empty** — nothing is dropped today. The
machinery is kept and tested, one tuple away from switching a market off.

**Can get wrong:** a name resolved by the `markets.csv` fallback carries **no
MIC**, so it cannot be renamed. For a Chinese name that lands the file under
`C1`/`C2` — the wrong code. The log says `CHINA, NOT RENAMED: n` and that
number should be zero.

---

## 4. Decide which days

**Reads** the partition list from `QATT_SERVER`, and the output directory,
and the miss cache.

**One rule:** *the last `BACKFILL_DAYS` partitions that exist, minus every
date already tried.* Both populations fall out of that one subtraction:

| already tried | fetches |
|---|---|
| nothing | the whole window — a backfill |
| all of it | nothing — a no-op re-run |
| all but today | today — the daily run |
| a gap in the middle | the gap — an interrupted backfill self-heals |

"Tried" means **a file on disk OR a line in the miss cache**. Both are plain
CSV and editable by hand: delete a file and it comes back, delete a cache line
and the name is asked again.

The plan is then **inverted from per-name to per-date**, because qatt is
partitioned by date: one query per date serves every name that wants it. A
60-day backfill of 40,000 names is 60 × chunks of reads, not 2,400,000.

**Can get wrong:** `--backfill 90` against a 60-day store quietly yields 60 —
the log prints the partition range so that is visible.

---

## 5. Fetch

One query per date per `SYM_CHUNK` syms:

```q
{[d;s] select sym,tradeTime,price,size,cond,ex from qatt
       where date=d, sym in `$s, price>0, size>0}
```

`price>0, size>0` is the whole test for "this row is a print" — every qatt row
is a transaction carrying the quote that stood at the time, so there are no
quote-only rows to exclude.

> **`tradeTime` is a placeholder.** qatt has five time columns and `qatt.time`
> is the *plant's* clock (HKT), not the exchange's. Run `qatt_time_probe.py`
> and set `qattsource.TIME_FIELD`. A wrong answer here produces files of the
> right length, with the right prices and volumes, and **every timestamp
> shifted by hours**.

---

## 6. Write

| outcome | what lands |
|---|---|
| prints came back | `raw-<code>-<YYYYMMDD>.csv` in `OUTPUT_DIR` |
| kdb answered, empty | one line in the miss cache — **not** an empty CSV |
| the query raised | nothing; the run stops |

That last row is the safety of the whole cache: a failure is not a fact about
the data, so an outage can never be recorded as "this name has no history".

The CSV is Bloomberg's shape, reproduced rather than improved — six data
columns and a seventh *header* cell naming the timezone:

```
#Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern Standard Time
09:31:33,0.105,1000,T,T,XASX
```

The label comes from `config/markets.csv`'s `TimeZone` column and **ships
blank**, writing a six-cell header, because what that clock is is exactly what
the probe has yet to establish.

---

## Everything a run writes

| | |
|---|---|
| `OUTPUT_DIR/raw-*.csv` | one per name per day |
| `OUTPUT_DIR/_no_data.csv` | the miss cache (or `MISS_CACHE_PATH`) |
| `--log FILE` | appended, if given |

Nothing else. No temp files outside those, nothing in the repo, nothing on the
kdb servers — **every query is a `select`**.

---

## How to convince yourself

Four levels, cheapest first:

```
python historical_ticks.py --self-test    ~200 checks, no kdb, no files
python historical_ticks.py --demo         the whole pipeline on canned kdb
python historical_ticks.py --dry-run      real reads, decides everything, writes NOTHING
python historical_ticks.py --trace "7203 JT" --date 2026-09-02
```

`--trace` is the one for "does this actually work". One name, one day, every
stage printed with its real values — the crosscode rows, the three candidate
shapes, which pass answered, the resolved sym and MIC, the collapse, the file
code, the chosen date, whether a real run would have skipped it, **the exact q
sent**, the rows back, the time span, and the first lines of the file written.

It calls the same stage functions the real run calls. A trace that walked a
parallel path would only tell you the parallel path works.

Two things to know about it: it **ignores** the output directory and the miss
cache and rewrites the file (it is a diagnostic, not a run), and it accepts
either spelling of a Chinese name — `600000 C1` or `600000 CG`.

Add `--dry-run` to trace everything and still write nothing.

---

## The log

```
12:04:41  ..  crosscode               41,208 rows   \\share\CrossCode.csv
12:04:52  ..  matched                 40,933 of 41,208   sym_bpipe 38,102, …
12:04:52  !!  CHINA, NOT RENAMED: 3 .CH syms had no MIC …
```

| | |
|---|---|
| `..` | what happened |
| `ok` | a stage finished and the number was right |
| `!!` | worth a human's attention; the run continues |
| `XX` | the run is stopping |

A run ends by saying how many warnings it produced. `--quiet` drops the
commentary but **never** a warning. `--log FILE` appends a timestamped record.

---

## What is still unsettled

1. **`TIME_FIELD`** — the one that yields *wrong* data rather than missing
   data. Settle it with the probe before any real run.
2. **The `TimeZone` labels** are blank until 1 is answered.
3. **qatt holds only subscribed names.** A crosscode name it never carried
   gets a miss-cache line, not a file. Expect a non-zero `no prints` count on
   the first run and check it is not most of the universe.
4. **HDB retention depth** is unknown until the partition listing says.
5. **A deleted output file is re-fetched** — the self-healing property, but
   worth knowing before pruning to save space.
