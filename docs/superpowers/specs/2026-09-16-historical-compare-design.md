# Historical compare — design

2026-09-16

## The job

The cutover instrument for Historical, the same role `--compare` already
plays for LimitUpDown and TradingData. Those two diff one file against one
file. This one diffs a **folder against a folder**, because Historical's
output is not a file — it is one CSV per name per day, up to a million of
them.

**One direction only.** The new process's output is the subject. Every file
the new process generated must exist in the old process's folder, and its
content must agree. A file the old folder has and the new one does not is
**not a finding and is never looked for**: the new run fetches only the days
it has not already tried, so the old tree is always the larger of the two and
the difference says nothing.

## Constraints, as established

| | |
|---|---|
| Where it runs | beside the other Historical modules, on the corporate machine |
| Inputs | two directories on disk. No kdb, no `local_settings.py`, no run |
| Layout | **identical both sides** — `<folder>/raw-<code>-<YYYYMMDD>.csv`, one folder per name |
| Scale | README's own figure: 59,013 names over 60 days, "upwards of a million files" |
| Sources differ | old is the Bloomberg add-in, new is `qatt`. Byte equality is not the bar |
| Language | Python 3.13, stdlib only, matching the rest of Historical |

## The decision that shapes everything

**Walk the new tree, never the old one.**

Everything follows from that. Work is proportional to what was generated —
a few hundred files after a daily top-up — not to the size of the archive
being compared against. Since the layouts are identical, the counterpart is
found by reusing the relative path verbatim: one `stat()` per generated
file, no index, flat memory.

The alternative considered was indexing the old tree by `(code, date)` so
that a foldering difference could never read as a missing file. Rejected on
cost: it builds a million-entry dict and walks the whole archive to compare
two hundred files. If a folder ever does differ, it surfaces as `missing`
and is investigated by hand — which is the correct amount of machinery for
something that is not known to happen.

The China case is what would have made it happen. The new process names a
folder with the **crosscode** code and the file inside it with the **MIC**
code — `600000 C1/raw-600000 CG-20260817.csv` — so the two codes differ for
Shanghai and Shenzhen. This is recorded here because it is the first thing
to suspect if Chinese names report as `missing` in bulk.

## Placement

`Historical/compare.py`. Standalone, with its own `--self-test`, like every
other module in the directory.

```
python compare.py OLD_DIR NEW_DIR
python compare.py OLD_DIR NEW_DIR --report somewhere/else.csv
python compare.py --self-test
```

`--report` defaults to `compare-report.csv` in the working directory. It is
always written, including on a clean run.

Nothing is added to `historical_ticks.py`. LimitUpDown and TradingData put
`compare()` in the orchestrator because there it runs the job first and
diffs the result; this one has no run to attach to.

## Which files are in scope

Every file under `NEW_DIR` whose name `ticksfile.parse_filename()` accepts.

That function is reused rather than re-derived, and the reason is in its own
docstring: a Bloomberg code can contain the separator, so `SCB-R TB` only
parses correctly when the date is matched first and the code is whatever
precedes it. A second regex here would be a second chance to get that wrong.

Anything it rejects is **skipped and counted**. `_no_data.csv` at the root
is the expected case — the miss cache is a new-process invention with no old
counterpart — but the count is reported so that a file which landed
somewhere unintended is visible rather than silently dropped.

## What "identical" means

A row normalises before anything is compared:

```
(time, Decimal(Last), Decimal(Volume), cond, ex, mic)
```

`Last` and `Volume` go through `Decimal`, so `3833` and `3833.0` are the
same print. This is the rule `LimitUpDown.compare` already uses and it is
load-bearing for the same reason: `ticksfile._num()` deliberately preserves
whatever scale the source carried, so the two sources agree on the value and
have no reason to agree on the scale. Without this, formatting alone would
report a difference on most rows of most files.

`Condition`, `Exchange` and `MicCode` compare as strings.

### Rows align by timestamp, as multisets

A timestamp is **not unique** — a liquid name prints many times in the same
second. So rows bucket by `#Time` and the two buckets compare as unordered
collections. Order within a second is not a difference, because the two
sources have no reason to sequence a second the same way.

Positional alignment was rejected for the same reason it is rejected in
every diff: one extra print early in the day shifts every row after it, and
a single insertion turns the rest of the file into noise.

### Leftovers are attributed to a column where possible

After the multisets cancel, a bucket may have unmatched rows. When exactly
one remains on each side, they pair and the difference is attributed to the
column that actually moved — which is what turns an opaque "row differs"
into `Last differ 2`. With more than one left on each side, no pairing is
guessed; they are reported as unpaired counts.

### The header is compared too

Including the trailing timezone label, since `ticksfile.write()` appends it
as a seventh field when present. A file whose header disagrees gets its own
status rather than being reported as a body difference.

## Output

One status per file. They are ordered, worst first, and a file reports the
first that applies — a missing file is never also a header difference,
because there is no header to compare it against:

| status | meaning |
|---|---|
| `missing` | the new process generated it; the old folder does not have it |
| `header` | the header row disagrees |
| `differs` | the body disagrees |
| `ok` | identical under the normalisation above |

### stdout

```
  files generated       812
  missing from old        3
  content differs        12
  identical             795
  skipped (not ours)      2

  3 missing, 12 differing -> compare-report.csv
```

Plus the first five findings inline, so a small run needs no second file.

### The report CSV

Only non-`ok` files get a row. A clean run writes a header and nothing else,
which is the readable way to say "nothing to look at".

```
folder,code,date,status,rows_old,rows_new,detail
600000 C1,600000 CG,20260817,missing,,1204,
7203 JT,7203 JT,20260817,differs,4812,4815,Last differ 2; times only in new 3
```

`folder` and `code` are separate columns precisely because of China: they
differ there and nowhere else, and a report that collapsed them would hide
the one case most likely to need explaining.

### Exit code

`1` when anything is `missing`, `header` or `differs`; `0` when every file
is `ok`. This is deliberately **unlike** LimitUpDown's `--compare`, which
returns `0` whatever it finds. The difference is that this one is meant to
gate a cutover step, and a check that cannot fail cannot gate anything.

## Memory

One file pair open at a time. Neither tree is ever held in memory, and
nothing accumulates across files except the tally and the report rows, which
are bounded by the number of *problems*, not by the number of files.

## Testing

`--self-test`, in the house style: build two temporary trees and assert one
status per case.

| case | expects |
|---|---|
| identical trees | every file `ok`, exit `0` |
| a file only the new tree has | `missing` |
| `3833` against `3833.0` | `ok` — the Decimal rule |
| a genuinely moved price | `differs`, attributed to `Last` |
| a print the old file does not carry | `differs`, counted as a time only in new |
| a second whose prints are reordered | `ok` — the multiset rule |
| two unmatched rows on each side of one bucket | `differs`, reported unpaired, no pairing guessed |
| a differing timezone label | `header` |
| `_no_data.csv` in the new tree | skipped, counted, not a finding |
| `SCB-R TB` | parses, folder and code intact |
| a clean run | report CSV holds a header and no rows |

## Out of scope

- Files the old folder has and the new one does not. Stated above, repeated
  here because it is the constraint most likely to be questioned later.
- Any comparison of the miss cache. It has no old counterpart.
- Anything written outside the report path. The tool reads two trees and
  writes one CSV.
