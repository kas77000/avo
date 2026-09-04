# Historical — tick data out of kdb, not Bloomberg

Lands the raw trade prints another process turns into a volume curve. One
CSV per name per day, in the shape the Bloomberg add-in was writing:

```
raw-EAU AU-20260817.csv
#Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern Standard Time
09:31:33,0.105,1000,T,T,XASX
09:58:58,0.105,1000,T,T,XASX
```

## Why this exists

The job it replaces made a Bloomberg `HistoricalTickDataRequest`. **Our
B-PIPE entitlement is real-time only** — `../LimitUpDown/other/bpipe_history.py`
is the probe that established it — so the prints have to come from the
plant's own store, `qatt`.

That store already holds what the request was asking for:

| CSV column | qatt |
|---|---|
| `#Time` | one of five time columns — **not yet settled**, see below |
| `Last` | `price` |
| `Volume` | `size` |
| `Condition` | `cond` |
| `Exchange` | `ex` |
| `MicCode` | not in qatt — `equity_master.ID_MIC_PRIM_EXCH` |

`price>0, size>0` is the whole test for "this row is a print": every qatt row
is a transaction carrying the quote that stood at the time, so there are no
quote-only rows to exclude. That is `kdb-queries/queries/liquidity_profile`'s
finding, relied on here rather than re-derived.

## One rule, not two populations

Every name wants **the last `BACKFILL_DAYS` partitions, minus whatever has
already been tried.** The backfill and the daily top-up both fall out of that
one subtraction. "Tried" means two things:

| | |
|---|---|
| a file in `OUTPUT_DIR` | `raw-<code>-<YYYYMMDD>.csv`, one per name per day |
| a line in the miss cache | kdb was asked and had nothing |

There is no manifest and no state file beyond those two, and both are
readable and editable by hand. Delete a file and it comes back; delete a
cache line and the name is asked again.

An earlier version branched on "does this name have any files" and gave
anything non-empty only the newest day. That is subtly wrong once misses
count as tried: a first run interrupted after recording one miss left the
name looking known, and **its backfill was never finished by any later run.**
Subtracting from a window cannot do that.

## The miss cache is why this finishes

`qatt` holds only the names we subscribe to. A crosscode name it has never
carried has no prints on **any** date, ever — and with no record of having
asked, every run asks again, for every day of the backfill, forever. On tens
of thousands of names that is the difference between a job that finishes and
one that does not.

```
_no_data.csv
BloombergCode,Sym,Date,FirstTried
ZZZ SP,ZZZ.SP,2026-09-02,2026-09-03
```

A name with no prints gets a **line here, not an empty CSV**, so the output
directory holds only real data.

**What counts as a miss is the whole safety of it: kdb answered, and the
answer was empty.** A query that raised, a server that was down, a partition
that could not be read — none of those reach the cache. They are failures,
not facts about the data, and caching them would turn a five-minute outage
into a permanent hole nobody ever sees.

A cache of absence can still go stale — a name gets subscribed, a vendor
backfills a day. `--retry-misses` ignores the file for one run and rebuilds
it from what that run actually finds. `FirstTried` is never bumped, so the
file tells you a name has been missing for six months rather than since the
last run.

## The three things that make this harder than it looks

**1. qatt is keyed on the composite, the crosscode carries the primary.**
Toyota is `7203.JP` in qatt and `7203 JT` in CrossCode.csv. `equity_master`
supplies the composite authoritatively — matched on `sym_bpipe` (`7203.JT`),
then on `sym_mbpipe` (`7203 JT EQUITY`) for whatever the first pass missed —
and `config/markets.csv` is the fallback for a name it has no row for. The
run tallies which route each name took, because a fallback that starts
carrying real traffic is a fact worth seeing.

**2. Three crosscode rows are one file.** `7203 JT`, `7203 JE` and `7203 JI`
are Tokyo, JNX and Chi-X Japan — three rows, one `7203.JP`, **one file**.
Miss this and the same file is written three times a run, each write racing
the last, with plausible tick counts in every one.

Which row names the file is then a real question, and the answer is the
primary listing: the row whose exchange code equals
`equity_master.EQY_PRIM_EXCH_SHRT`. So Toyota's file is `raw-7203 JT-…`,
never `raw-7203 JE-…` and never the composite.

**3. A Chinese stock is spelt three ways, and all three are right.**

| | |
|---|---|
| `600000 C1` | in the crosscode |
| `600000.CH` | the only shape `equity_master` and `qatt` answer to |
| `600000 CG` | on disk, which is what the consumer reads |

The middle one is why `equity_master` is asked a **third** way: matching
`sym_bpipe` would look for `600000.C1` and find nothing, so a third pass
matches the `sym` column itself with ticker-plus-composite. It runs only on
what the first two passes missed, and the tally says how many it caught.

The last one is driven by the **MIC**: `XSHG` takes `CG`, `XSHE` takes `CS`.
Not by the Fidessa market — `SHA`/`SHH`/`SSC`/`SZA`/`SHZ`/`SZC` do not say on
their face which side of the border they are, and guessing wrong mislabels
every Chinese file with nothing in the output to show for it. A name whose
MIC never came back keeps its `C1`/`C2` code and is **counted loudly**,
because that file is under the wrong name.

Nothing is excluded today. `EXCLUDED_MICS` is empty and the machinery around
it is kept, tested, and one tuple away from switching a market back off.

## Settle the clock before the first real run

`qatt` has five time columns and the sample file is stamped in **exchange
local** time — its header says so in words. But `qatt.time` is the **plant's**
clock, running eight hours ahead of UTC for every name in the store. For an
Australian name that column is two hours out; for a Japanese one, one hour.

Nothing about the wrong answer looks wrong. The file is the right length, the
prices are right, the volumes are right, and every timestamp is shifted. A
volume curve built on it puts the open auction in the wrong bucket for every
market except Hong Kong.

```
python qatt_time_probe.py 7203.JP --session 09:00-15:00
```

The server comes from `local_settings.py`, like everything else — nothing
here takes a host or a port on the command line. The probe needs only
`QATT_SERVER`, so the file is usable for it before the rest is filled in.

It pulls one name for one day, lays all five columns side by side, and
compares each one's span against the session hours you give it. The
exchange's clock is the column that lands on the session; the others are off
by whole hours. Then set `qattsource.TIME_FIELD` — it ships as `tradeTime`
because the name says so, **not because it has been checked**.

Pick a liquid name, and one *outside* Hong Kong: a HK name cannot separate
the plant's clock from the exchange's, because there they are the same clock.

`config/markets.csv` carries a `TimeZone` column for the seventh header cell.
It ships blank on every row and writes a six-cell header until filled in,
because what that clock is is exactly what the probe has yet to establish.

## Running

```
python historical_ticks.py --self-test     checks, no kdb, no files
python historical_ticks.py --demo          the whole pipeline, canned data
python historical_ticks.py --dry-run       real reads, decides everything, writes nothing
python historical_ticks.py                 the daily run
python historical_ticks.py --backfill 90   a deeper first run
python historical_ticks.py --date 2026-09-02   as if that were today
python historical_ticks.py --only "7203 JT"    one name
python historical_ticks.py --retry-misses      ask again about the empties
python historical_ticks.py --log run.log       tee the log to a file
python historical_ticks.py --quiet             warnings and failures only
```

**To check the process actually works, trace one name on one day:**

```
python historical_ticks.py --trace "7203 JT" --date 2026-09-02
```

Every stage with its real values — the crosscode rows, the three candidate
shapes, which `equity_master` pass answered, the resolved sym and MIC, the
collapse, the file code, whether a real run would have skipped the date, **the
exact q sent**, the rows back, the time span, and the head of the file
written. It calls the same stage functions a real run calls. Add `--dry-run`
to see all of it and write nothing.

**[`WORKFLOW.md`](WORKFLOW.md) walks the whole run stage by stage** — what
each reads, what it decides, what it can get wrong, and how to see it.

`--self-test` and `--demo` need nothing but Python. Every module has its own:

```
python settings.py --self-test       python universe.py --self-test
python logs.py --self-test           python ticksfile.py --self-test
python marketcfg.py --self-test      python misscache.py --self-test
python crosscode.py --self-test      python qattsource.py --self-test
python qatt_time_probe.py --self-test
```

## First run

```
pip install pykx
copy local_settings.py.example local_settings.py
```

Fill in `EQUITY_MASTER_SERVER`, `QATT_SERVER`, `CROSSCODE_PATH` and
`OUTPUT_DIR`. **`QATT_SERVER` must be the HDB**, the one partitioned by date —
the RDB holds today only, and every day this job asks for has finished.
The two servers are not the same box: in `kdb-queries`' layout `equity_master`
sits on the order side (`:5010`) and `qatt` on its own (`:5011`).

## Where things live

| | |
|---|---|
| `settings.py` | local_settings.py, strictly. Every script reads servers and paths from here. |
| `logs.py` | one place that decides what a line of output looks like |
| `qattsource.py` | session, queries, type coercion. The only module that imports pykx. |
| `misscache.py` | the long-term record of (name, date) pairs kdb had nothing for |
| `crosscode.py` | CrossCode.csv → rows. Pure. |
| `marketcfg.py` | markets.csv → composite and timezone label. Pure. |
| `universe.py` | **resolve, collapse, filter** — the three hard things above. Pure. |
| `ticksfile.py` | what a file is called, what is on disk, the CSV itself. Pure. |
| `historical_ticks.py` | orchestration, the plan, the report |
| `qatt_time_probe.py` | which time column is the exchange's clock |

`universe.py` keeps every row it drops, with a reason, and the run prints the
counts. A universe that quietly shrinks is the failure this job is most
exposed to and the one hardest to notice from the output.

## Known risks, unresolved

- **`TIME_FIELD` is a placeholder.** Above. This is the one that produces
  wrong data rather than missing data.
- **qatt holds only names we subscribe to**, not the exchange's full list. A
  new crosscode name may have no history at all. It gets a miss-cache line
  rather than being retried every run, and the count is reported.
- **HDB retention depth is unknown** until the partition listing says. The
  backfill is capped by it, so a `--backfill 90` against a 60-day store
  quietly yields 60 — the run prints the partition range so that is visible.
- **Names resolved by the `markets.csv` fallback carry no MIC**, so their
  files cannot be renamed. For a Chinese name that means the file lands as
  `C1`/`C2` instead of `CG`/`CS` — the wrong name. The run counts these
  separately under `CHINA, NOT RENAMED`; if that number is ever non-zero,
  fix it before trusting the output.
- **A deleted output file is re-fetched.** That is the self-healing the
  output directory was chosen for, but it means pruning old files to save
  space will pull them back on the next run unless the window has moved past
  them.
