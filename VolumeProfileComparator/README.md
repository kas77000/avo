# Volume Profile Comparator

Two processes produce the same volume profile CSV — the one the Volume Profile
Viewer opens. This compares two of them for one market and says, stock by
stock, whether the **new** profile is **aligned** with the **old** one or
**off**, and shows the two side by side so it can be checked by eye.

```
python compare_profiles.py OLD.csv NEW.csv
```

```
old  profile_old.csv  (1,240 stocks)
new  profile_new.csv  (1,245 stocks)

 1,240 stocks in both files
 1,198 aligned   42 off (max gap > 2.00 pp)
   0 only in old   5 only in new

 worst:  HDFCB.IN       NSI-MAIN    -5.88 pp at 15:25:00   (old 83.98%, new 78.10%)
         ICICIBC.IN     NSI-MAIN    +4.00 pp at 9:15:00   (old 2.15%, new 6.15%)

 wrote comparison.csv, comparison.html
```

Python 3 only, nothing to install.

| Option | Default | |
|---|---|---|
| `--threshold PP` | `2.0` | the largest gap still called aligned, in percentage points |
| `--out DIR` | here | where `comparison.csv` and `comparison.html` go |
| `--self-test` | | checks on made-up profiles, no files needed |

## Aligned or off

For each stock in both files, the two **cumulated** curves are compared at
every bucket time either file has, and the gap is `new − old` in percentage
points of the day. The **largest gap**, and when it happened, is the stock's
score:

- at most `--threshold` → **ALIGNED**
- above it → **OFF**

A closing auction of a different size, a curve that runs early or late, a
profile that does not end at 100% — each moves the cumulated curve, so the
one number catches all of them and says *when*. A tie goes to the earliest
time, so the same two files always name the same moment.

**Different bucket grids are fine.** Each curve holds its last value until its
next bucket, which is what a cumulated curve means, so a 1-minute profile is
measured against a 5-minute one without inventing a point.

A stock in only one file is **ONLY_OLD** or **ONLY_NEW**.

## What it writes

**`comparison.csv`** — one row per stock, worst first:

```
FidessaCode,Venue,ReutersCode,Verdict,MaxGapPP,At,OldPct,NewPct
HDFCB.IN,NSI-MAIN,HDBK.NS,OFF,-5.88,15:25:00,83.98,78.10
INFO.IN,NSI-MAIN,INFY.NS,ONLY_OLD,,,,
RELIANCE.IN,NSI-MAIN,RELI.NS,ALIGNED,1.00,9:15:00,2.36,3.36
```

`MaxGapPP` is signed: negative means the new curve is *behind* the old one.
`OldPct` / `NewPct` are the two curves at `At`.

**`comparison.html`** — one self-contained page. Nothing is loaded from the
network, so it can be mailed.

- the counts, and the stock list, worst first — sortable, filterable by code
  or venue, and by verdict
- for the stock picked, **old and new side by side**: the cumulated curve and
  the volume per bucket, as in the viewer
- both sides on **the same time axis and the same scales**, so two different
  profiles cannot look alike just because each chart stretched to fit itself
- hovering either side moves a crosshair on both and reads out old, new and
  the gap at that time; the red dashed line is the time of the largest gap
- underneath, the gap across the day against the ± threshold
- **bar scale without the auctions** leaves the first and last trading
  buckets out of the bar scale, so a 20% close does not flatten the rest of the
  day; bars taller than the scale are clipped and labelled with their value
- <kbd>↑</kbd> / <kbd>↓</kbd> step through the list, so every OFF stock can be
  looked at in turn

**The exit code** is `0` when every stock is aligned and in both files, `1` when
anything is off or in one file only, and `2` when a file cannot be read — so a
scheduled job can act on it.

## Input

The viewer's format, read with the viewer's tolerance, because these files get
re-saved through Excel:

```
#TimeZone=India Standard Time,format=V2,isweekly=false
#FidessaCode,ReutersCode,Venue,TimeZone,Time,CumulatedPercentage
ICICIBC.IN,ICBK.NS,NSI-MAIN,India Standard Time,9:15:00,0.0215
```

- delimiter `,` `;` tab or `|`, and decimal commas
- cumulated values as `0..1` or `0..100`, decided for the whole file
- `9:15:00`, `09:15`, or an Excel time serial
- `#` on the header row optional; `ReutersCode`, `Venue`, `TimeZone` optional
- malformed rows are counted and skipped, not fatal

A stock is its **FidessaCode and its Venue**, as in the viewer. Times are
compared as written: two files whose time zones differ for the same stock are
refused, because their clocks do not line up. A session that crosses midnight
keeps its trading order.

## Size

3,000 stocks at 1-minute buckets — two 73 MB files — compare in about 25
seconds, and the page is about 26 MB and opens in about a second.
