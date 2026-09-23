# LimitUpDown — ask Bloomberg, or compute it

Builds `limitUpDown.csv`, the daily price-band file the Nova ATS uses to bound
orders. A Python port of `LimitUpDown.r`, and the version that runs.

```
*.py       the job
config/    one row per venue, one row per band tier
other/     the B-PIPE probes that settled what the job could ask for
```

It writes the seven columns the ATS reads, unchanged from the R job:

```
#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue
7203.T,7203 JT,2026-09-03,3833,2433,7203.JP,TYO-MAIN
```

**Every venue names its own source, and any of them can be switched by editing
one word** in `config/markets.csv`:

```
Source=bloomberg   MIN_LIMIT / MAX_LIMIT off B-PIPE
Source=computed    band = f(previous close, tiers), rounded to the tick
                   previous close from kdb's equity_master
```

Each source is opened **only if it has work**. Switch every venue to computed
and the job never touches B-PIPE; leave them all on bloomberg and it never
touches kdb. That is what makes a market B-PIPE will not serve us — an
entitlement refusal, say — still publishable.

### Where a computed close comes from

`equity_master` in kdb is the Bloomberg **Data Licence** feed, so its `PX_LAST`
on the previous partition is a close of the same lineage as `PX_YEST_CLOSE`,
**adjusted for corporate actions**, reached without a real-time entitlement.
The alternatives were worse:

| candidate | why not |
|---|---|
| `PX_YEST_CLOSE` | static, and confirmed refused to this subscription |
| `PREV_CLOSE_VALUE_REALTIME` | only served for names the plant serves at all — exactly the set that fails |
| **`equity_master.PX_LAST`** | **this** |

Two things it gets right that a naive version would not. **The date** rolls back
to the most recent partition with rows, because yesterday is a Sunday every
Monday, and both dates are printed so a stale close is visible. It is asked
for **two ways** — a Python date converted by pykx, then a bound computed in q
from `.z.D` so no date crosses the wire at all — because a 2026-09-04 run died
on `QError: type` here. The report says which one answered. If both fail, the
error says it is a schema question and names the probe:

```
python other/em_probe.py --server HOST:PORT --meta
``` **The symbol**
gets up to two candidates — the crosscode's own suffix first, then the venue's
`BBGComposite` — because Shanghai is `600001 CG` in the crosscode and
`600001.CH` in equity_master. The run reports which suffix hit.

### As shipped: Japan, Thailand and India ask; everything else computes

```
bloomberg   TYO-MAIN  JNX-MAIN  CHJ-MAIN  SET-MAIN
            NSI-MAIN  BSE-MAIN  BSE-SECONDARY
computed    the other twelve
```

**The markets Bloomberg prices are exactly the markets whose rule is not
written down here**, and each of them is refused if you try to compute it. TSE
limits are an absolute price-step table nobody has transcribed; Thailand's and
India's are not in `bands.csv` either. A percentage tier invented for any of
them would be a plausible-looking wrong answer on live orders, so switching one
is refused outright:

```
TYO-MAIN has Source=computed but no band tiers in bands.csv. Add its tiers,
or leave it on Source=bloomberg - a market whose rule nobody has written
down cannot be computed.
```

## Thailand and India need more than a config row

Both were out of scope until the filters below existed, and a bare
`markets.csv` line for either would have published limits that must not exist.

### Thailand: the local line only

The SET lists one company three ways — the local board, the foreign board
(`/F`) and the NVDR (`/Q`) — and Nova trades the local one. The other two are
dropped in `crosscode.py`, before the universe, so they are counted in the
report and never paid for in a Bloomberg request. R greps the same two
(`LimitUpDown.r:180`).

### India: three venues, and names that must NOT get a limit

| | |
|---|---|
| `NSI-MAIN` | the NSE, priced by Bloomberg |
| `BSE-MAIN` | the BSE, priced by Bloomberg |
| `BSE-SECONDARY` | every `BSE-MAIN` row again, off the same fetch |

**Some Indian names have their limits configured inside the ATS strategy
files**, and for those Nova's own configuration is the authority. Publishing a
`limitUpDown` row for one would *override a number a person set deliberately*,
which is worse than publishing nothing — so each Indian venue names its
strategy file in the new `ExcludeFile` column of `markets.csv`, and every
mnemonic the file lists is dropped from the universe:

```
India,NSI-MAIN,IN,10:49:00,bloomberg,,,,in-nse_drv.stra
India,BSE-MAIN,IB,10:49:00,bloomberg,,,,in-bse_drv.stra
```

Both are **filenames, not paths** — they are resolved against `TSR_DIR`,
because they live beside `spol_JKT.tsr` on the ATS share. So the config
committed here names no machine, and moving the share is one setting rather
than three edits. An absolute path is still honoured, for the day one of them
does not live with the others.

The file is whitespace-separated with an eight-line preamble, and the mnemonic
is its second field, matched against `Mnemo` with the first two characters
stripped — the crosscode prefixes a country code the strategy file does not
carry. `LimitUpDown.r:112-152`.

**An unreadable strategy file stops the run, exactly as R's `stop()` does**, and
the reason is worth stating: an empty exclusion list is indistinguishable from
a correct one, and the only symptom would be Indian names quietly receiving a
limit that overrides the desk's. It is read **only once the venue has reached
its cutoff**, so an Indian share being down at 07:30 cannot take out Japan.

**The BSE secondary venue has no crosscode line of its own.** A name on the NSE
may also be reachable on the BSE under a different code, and the crosscode says
so in three columns of the *same* row — `VenueList` naming `BSE-SECONDARY`,
plus `BSEBloombergCode` and `BSERic`. Those rows are synthesised in `india.py`,
asked of Bloomberg in the same request as everything else, and published
**twice**, once under each venue, off one set of limits. A code the crosscode
already carries itself is left to the ordinary path rather than synthesised, or
one name would appear twice under `BSE-MAIN`. `LimitUpDown.r:387-414`.

One deliberate difference from R: R builds that list from the crosscode as
read, this builds it from the universe as filtered — so a BSE listing is never
derived from a line that lost a duplicate or is not ACTV. It is the rule this
job already has everywhere else.

## One substitution in each branch

Bloomberg carries these numbers under two sets of names and our B-PIPE
entitlement serves only one. A probe on 2026-09-03 got *"Field not permitted to
datafeed users"* for `PX_MIN_LIMIT`, `PX_MAX_LIMIT` and `PX_LAST` on the same
request where the **real-time** names answered:

| wanted | barred (static) | used here |
|---|---|---|
| the limits | `PX_MAX_LIMIT` / `PX_MIN_LIMIT` | `MAX_LIMIT` / `MIN_LIMIT` |
| the close | `PX_YEST_CLOSE` | `PREV_CLOSE_VALUE_REALTIME`, or the next candidate that answers |
| last trade | `PX_LAST` | `LAST_PRICE` |

`7203 JT Equity` returned `MIN_LIMIT` 2433.0 / `MAX_LIMIT` 3833.0. See
`other/bpipe_probe.py`.

## The status filter

Only ACTV names are published, and the status comes from **CrossCode's own
`BloombergStatus` column** — not from Bloomberg. It is already in the
file we read to build the universe, and `dedupe` already trusts it to choose
between two rows claiming one code. The filter applies it once more, to the
case dedupe never sees: a delisted name that had no competitor to lose to.

It also means the file no longer hangs on `MARKET_STATUS`, a **static**
field from the same family as `PX_LAST`. **The 2026-09-04 run settled that
question: `MARKET_STATUS` IS served to us** — 21,869 names came back
`ACTV` — so it is kept as a cross-check and the two filters agree. The
CrossCode column stays primary because it costs nothing and cannot be
withdrawn by an entitlement change.

Two rules keep the filter from emptying the file:

| | |
|---|---|
| a **blank** status | no opinion — the row is kept, the same rule `band_from` applies to a field Bloomberg did not serve |
| a **missing column** | fatal. Every row would read as "no opinion", the filter would pass everything, and the only symptom would be delisted names quietly getting a band |

**And do not reach for `RT_EXCH_MARKET_STATUS` instead.** Bloomberg's own real-time
model has two status axes, visible as two `MKTDATA_EVENT_SUBTYPE` values:

| axis | field | answers |
|---|---|---|
| `MARKETSTATUS` | `RT_EXCH_MARKET_STATUS` | what **session phase** is the exchange in — open, closed, auction, halt |
| `SECURITYSTATUS` | `RT_SIMP_SEC_STATUS` | this **instrument's** own state |

**What the 2026-09-04 run actually showed, which is not what was predicted
here:** `RT_EXCH_MARKET_STATUS` came back `ACTV` for 21,863 names — it
tracks the listing, not the session phase, and it did **not** read "closed"
for the universe. The reasoning that follows was wrong on the facts; the
conclusion survives for a different reason. `RT_SIMP_SEC_STATUS` *is* the
session-shaped one — 11,309 `TMOC`, 8,032 `TRAD`, 2,391 `CLOS`, 129
`AUCT` — so filtering on **that** at 07:30 would drop most of the file.
Neither is used.

Both real-time candidates are requested and **tallied but never filtered on**.
The 2026-09-04 run carried:

```
  status     21869  MARKET_STATUS = ACTV            <- served after all
  status     21863  RT_EXCH_MARKET_STATUS = ACTV    <- the listing, not the session
  status     11309  RT_SIMP_SEC_STATUS = TMOC       <- this is the session-shaped one
  status      8032  RT_SIMP_SEC_STATUS = TRAD
  status      2391  RT_SIMP_SEC_STATUS = CLOS
```

## Reading the run report

It opens with one line per venue — published, excluded, and where the band
comes from. **Every configured venue appears, including one that published
nothing**, because a market losing its whole universe is the thing most worth
seeing and the thing a published-only table cannot say:

```
  venue        published  excluded  source
  JKT-MAIN           842        42  computed
  KLS-MAIN             0       905  bloomberg   <- nothing published
  TYO-MAIN          3421        18  bloomberg
```

Then every exclusion, named, counted **and broken down by venue**:

```
  excluded    412  no MIN_LIMIT
    KLS-MAIN       412  MAYBANK.KL (MAYBANK MK), PBBANK.KL (PBBANK MK) (+410 more)
  excluded     18  last price outside the limits
    TYO-MAIN        18  6501.T (6501 JT), 7011.T (7011 JT) (+16 more)
```

The venue line is the point. A bare `excluded 412 no MIN_LIMIT` cannot tell you
whether 412 names are scattered across the region or whether **one whole market
has vanished**; split by venue, it says so at a glance. Both codes are shown
because the Bloomberg one is what you paste into a terminal to check a name by
hand, and the RIC is what you match against the published file.

### The entitlement CSV

Names B-PIPE refused for want of an entitlement are also written to
`entitlement_refused.csv`, beside `OUT_TEMP`:

```csv
ReutersCode,BloombergCode,Venue,EIDs,Message
MAYBANK.KL,MAYBANK MK,KLS-MAIN,64487 64488,Bloomberg refused the security: Security Entitlement Check Failed! ...
```

They are separated from every other exclusion because the fix is different in
kind: no code change reaches these names. The `EIDs` column is the part a
market-data team acts on. A run with no entitlement refusals **deletes** the
file rather than leaving yesterday's looking like today's. It is never
published to Test/Pilot/Prod — the ATS does not read it.

Run `other/bpipe_auth.py` to find out whether a different identity on the
same machine already holds those EIDs.

## Investigating a kdb fault

A real run spends its first minutes on sixteen thousand Bloomberg names and
only then touches kdb, so a kdb fault costs a whole run to see once. This
reaches the same code in seconds:

```
python limit_up_down.py --kdb-check          five real names, verbosely
python limit_up_down.py --kdb-check --sample 50
```

It prints the query sent, the argument **and what pykx turned it into**, and
what came back — then the symbol candidates per name and which of them
answered. Nothing is written and Bloomberg is never opened.

The schema (`kdb-queries/no_git/kdb/equity_master.csv`) says `date` is a q
date, so a `'type` here is the client's conversion, not the column. That is
what the `arg ... -> ... (q type N)` line settles.

## Running

```
python limit_up_down.py --self-test        checks, no Bloomberg, no files
python limit_up_down.py --demo             both branches on canned data
python limit_up_down.py ""                 real run, publish nowhere
python limit_up_down.py "Test|Pilot|Prod"  real run, publish
python limit_up_down.py --compare OLD.csv  diff the last output against another
python limit_up_down.py --kdb-check        only the kdb path, verbosely
python limit_up_down.py --venues KSC-MAIN  one venue, or several, pipe separated
```

### Working on one market

`--venues "KSC-MAIN|KOE-MAIN"` narrows a real run, a `--compare` and a
`--kdb-check` to those venues, so an analysis does not cost a full fetch of
sixteen thousand names. A venue markets.csv has never heard of is refused by
name, with the list of real ones — a typo that silently matched nothing would
look exactly like a market with no rows.

> **A narrowed run does not publish.** The output file is a *replacement*, not a
> merge, so copying a Korea-only file to Prod would delete every other market's
> limits from the feed. `--venues` writes `out/limitUpDown.csv` for reading and
> refuses to copy, whatever environments are named on the command line, and says
> so on stderr.

The exclusions are still reported for the whole crosscode — a venue nobody
configured, a security type we do not trade — because those read the same
whichever venues a run is about. Only the universe is narrowed.

`--compare` prints its differences **and writes every one of them** to
`compare-report.csv` (`--report` moves it). The printed lines are for reading;
the CSV is the record, and it sorts and filters when a cutover turns up more
than fits on a screen:

```
status,venue,code,close,column,old,new
rowcount,SSE-MAIN,,,,0,1
only_in_old,TSE-MAIN,6758 JT,,,,
price,KSC-MAIN,0000D0 KP,8750,LimitUpPrice,11375,11370
price,TSE-MAIN,7203 JT,,LimitUpPrice,3900,3833.0
```

**`close` is the price a computed limit came from**, so a disagreement can be
checked against the number behind it without a second lookup. It is joined from
`out/closes.csv`, which a real run writes beside the output — the output file
itself cannot carry it, since its seven columns are the ATS contract.

**Blank means Bloomberg priced that row**, so the column also says *which path*
produced it, which is usually the first thing you want when two files disagree.
A `--compare` between two files with no run behind them leaves it blank
throughout and says so.

**`code` is the BloombergCode**, because that is what the report is read in.
The comparison still *keys* on `#ReutersCode` — that is what the two files
agree on and what the ATS contract puts first — and the printed lines still
say the RIC, so each form names a row the way its reader expects. A file with
no `BloombergCode` column falls back to the RIC rather than leaving the row
unidentified.

`only_in_old` and `only_in_new` carry the venue too, so the report reads by
market without joining anything back to the crosscode. A run with nothing to
report still writes the header.

**`--compare` does not run the job** — it diffs whatever the last run left in
`out/`. Run it first, or it now says so by name instead of raising.

**kdb runs before Bloomberg.** The Bloomberg fetch is sixteen thousand names
and minutes of it, so running it first meant every kdb fault cost a whole run
to see once. The cheap, fragile side now fails fast.

`--self-test` and `--demo` need nothing but Python. Every module has its own:

```
python bands.py --self-test        python marketcfg.py --self-test
python ticks.py --self-test        python crosscode.py --self-test
python bpipe.py --self-test        python mailer.py --self-test
python india.py --self-test
```

## First run

```
pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
copy local_settings.py.example local_settings.py
```

Fill in `BPIPE_HOST`, `BPIPE_PORT`, `BPIPE_APP`, `EQUITY_MASTER_SERVER`,
`TSR_DIR`, `LOG_DIR` and the SMTP host. `EQUITY_MASTER_SERVER` is only read when some
venue is `computed`.

`TSR_DIR` now carries India's two `.stra` strategy files as well as the tick
ladder, so it has to be the real ATS share before India's 10:49 cutoff — the
run stops there rather than publishing a limit over one the desk configured.
The B-PIPE three have no defaults — the job refuses to start rather than connect
somewhere you did not mean.

**Keep `OUT_TEMP` different from v1's** while both are running, or whichever
finishes last is the file that gets published.

## Where things live

| | |
|---|---|
| `bpipe.py` | session, authorization, batched fetch. The only module that imports blpapi. |
| `kdbclose.py` | the previous close, and the tick ladder, out of kdb. The only module that imports pykx. |
| `bands.py` | tier selection, band arithmetic, tick rounding. Pure. Copied from v1. |
| `ticks.py` | tick ladders, from kdb's `ticksizetbl` or a `.tsr`. Pure. |
| `marketcfg.py` | loads the config **and enforces the split** |
| `crosscode.py` | CrossCode.csv → the universe, filtered and deduplicated |
| `india.py` | the ATS strategy files, and the BSE listings with no row of their own |
| `limit_up_down.py` | orchestration, validation, environment copy |
| `config/markets.csv` | one row per venue: cutoff, which side of the split, and India's `ExcludeFile` |
| `config/bands.csv` | tiers per venue. Present for twelve; they are what make a venue switchable |
| `config/spol_JKT.tsr` | **placeholder, and Indonesia only.** Point `TSR_DIR` at the ATS share, which also holds India's two `.stra` files. |

`marketcfg` refuses a half-configured venue: a `bloomberg` venue carrying a tick
file, tiers for a venue Bloomberg prices, a `computed` venue with no tiers. Each
is somebody's half-finished edit, and each would otherwise surface as a market
silently missing from a production feed.

## Rules worth knowing about

- **Indonesia's arithmetic runs in this order**: tier from the previous close,
  band, floor the down leg at `MinPrice`, and only *then* round to the tick.
  Rounding before flooring would move prices near a tier boundary. The tick is
  chosen from the close, not from the limit being rounded.
- **The cutoff is cumulative by time of day.** Each run rewrites the whole file
  with the venues whose `Time` has passed, so the 07:30 run publishes Japan and
  Korea and the 09:03 run republishes those and adds the rest.
- **Deduplication prefers the ACTV row, then the ACTV filter takes the
  rest.** A repeated `BloombergCode` is settled on `BloombergStatus`; if
  none of the group is ACTV the code is published by nobody, because a band
  off a delisted line is worse than no band. The order matters — filtering
  first would leave dedupe's preference as dead code.
- **A limit that does not bracket the last trade is not published.** A *missing*
  last price is not a veto, though — it is counted and reported, because this
  job runs pre-open and a real-time field may not have ticked yet. If
  `LAST_PRICE` turns out to be always populated at run time, tighten
  `bpipe.band_from`.
- **A name under Rp 50 matches no tier**, and is reported rather than quietly
  lost.
- **Rights are excluded** by the `Type in {Equity, ETF}` filter on CrossCode.csv,
  for every market.
- **Write to temp, validate, then copy.**

## Scope

Nine countries, nineteen venues:

| | venues | cutoff | source |
|---|---|---|---|
| Japan | `TYO-MAIN` (JT), `JNX-MAIN` (JE), `CHJ-MAIN` (JI) | 07:30 | **bloomberg**, no fallback |
| Korea | `KSC-MAIN` (KP, KOSPI) | 07:30 | computed, ±30%, rounded on kdb's ladder |
| Korea | `KOE-MAIN` (KQ, KOSDAQ) | 07:30 | computed, ±30%, rounded on kdb's ladder |
| Malaysia | `KLS-MAIN` | 07:59 | computed, ±30% |
| Taiwan | `TAI-MAIN` | 07:59 | computed, ±10%, rounded on kdb's ladder |
| Indonesia | `JKT-MAIN` | 07:59 | computed, tiered + tick |
| China | `SHA`, `SHH`, `SSC`, `SZA`, `SHZ`, `SZC` | 09:03 | computed, ±10% / ±20% |
| Philippines | `PHS-MAIN` | 09:03 | computed, ±30% |
| Thailand | `SET-MAIN` | 10:39 | **bloomberg**, local line only |
| India | `NSI-MAIN`, `BSE-MAIN`, `BSE-SECONDARY` | 10:49 | **bloomberg**, less the ATS's own names |

Thailand and India are the two that took more than a config row — see above.

### Rounding, and where the tick comes from

### The exchange's own calculation, for Korea and Taiwan

`Rounding=krx` is not a rounding mode but the **three-step calculation KRX
publishes**, and no amount of rounding a finished band reproduces it:

```
1.  range = base price x the limit percentage
2.  TRUNCATE that range to the tick of the BASE PRICE      <- the step we lacked
3.  multiply by the leverage multiple, then base +/- range,
    each truncated to the tick of THAT price
```

> 가격제한폭은 기준가격에 100분의 30을 곱하여 산출한 금액이며, 호가가격단위 미만
> 금액은 절사한다 — KRX

Step 2 is why both legs are symmetric about the close: they move by the **same
whole number of the base's ticks**. And step 3's order matters — `0080Y0 KP` at
8025 gives `0.30 x 8025 = 2407.50`, truncated on its 5 tick to `2405`, doubled to
`4810`, so `12835/3215`. Doubling *first* gives 4815 and 12840, which the
exchange does not print.

Verified against 19 names from a live compare against Bloomberg: 18 match on both
legs, and the one that does not is off by a base price of 51052 against the
51055 that reproduces its limit — a close difference, not a rounding one.

`bands.csv` therefore carries the **ordinary 30% and the multiple separately**,
as the regulation phrases it, rather than a pre-multiplied 60%:

```
KSC-MAIN,pct,,0,0.30,0.30,leverage,,2
KSC-MAIN,pct,,0,0.30,0.30,3x,,3
KSC-MAIN,pct,,0,0.30,0.30,inverse,,
```

Indonesia stays on `inward` — `LimitUpDown.r` rounds the finished band and has no
step 2, and that is the authority for that market.

**A band that already lands on a valid tick is published as it comes out.**
`8750 × 1.3 = 11375` on a 5 tick stays 11375; rounding only moves a value that
is not on the grid. That is what `floor`/`ceiling` already do and there is no
mode that moves it one tick further in — one was briefly added on the strength
of a single name and a whole-universe compare against Bloomberg then showed
hundreds out by exactly one tick.

A venue rounds only if its `Rounding` column says so. Today that is Korea's
`KSC-MAIN` and Indonesia; the other nine computed venues publish the raw band,
which is a config decision and a one-word edit.

**Each leg rounds on the coarser of two ticks** — the one at the previous close
and the one where that leg actually lands. A ladder is a function of price and
the two legs are at different prices, so a band spanning a tier boundary has two
ticks, not one.

Two Bloomberg-confirmed Korean names pin the rule down — **one per venue**, and
they point opposite ways, which is why it is not simply "the leg's own tick".
Note `KOE-MAIN` is **KQ/KOSDAQ** and `KSC-MAIN` is **KP/KOSPI**; that mapping is
counterintuitive and was verified against `config_cash.xml`:

| | venue | close | leg | tick at close | tick at leg | Bloomberg |
|---|---|---|---|---|---|---|
| `000250 KQ` up | `KOE-MAIN` | 157,500 | 204,750 | 100 | **500** | 204,500 |
| `000020 KP` down | `KSC-MAIN` | 5,150 | 3,605 | **10** | 5 | 3,610 |

The first needs the leg's tick (204,750 crosses 200,000 into the 500 band). The
second needs the close's: 3,605 is *already* a valid price on its own tick of 5,
so only the coarser 10 lifts it to 3,610. The coarser of the two gives both.

Because a ladder is monotonic, "coarser of the two" is just "the tick at the
higher price" — so **every down leg is unchanged** (the close is the higher
there) and an up leg changes only when the limit crosses into a coarser band.

**This is per venue, in the `TickFrom` column**, because the two venues that
round have different verified answers:

| `TickFrom` | venue | source |
|---|---|---|
| `close` (blank) | Indonesia | `LimitUpDown.r:315-324` — the tick comes from `PX_YEST_CLOSE` and both legs floor/ceil on it |
| `coarser` | Korea `KSC-MAIN` and `KOE-MAIN` | Bloomberg, one verified name each |

**Only a venue that rounds carries it**, because only a venue that rounds
resolves a tick at all. The nine computed venues with `Rounding` blank publish
the raw band and never touch a ladder, so the column is blank for them — and
`coarser` is Bloomberg's answer for *Korea*, not a rule anyone has checked for
Malaysia, Taiwan, China or the Philippines. A venue that starts rounding later
picks its own.

Not a detail: an Indonesian close of 4,500 has an up leg of 5,625, which
crosses the 5,000 floor where the tick goes 10 → 25. The R job publishes
**5,620**; the coarser rule would publish 5,625. Every Indonesian name near a
tier boundary would move.

**The ladder is per name, not per venue, and it comes out of kdb** —
`ticksizeids` maps a sym to a tick-table id, `ticksizetbl` holds that table's
tiers. Both sit beside `equity_master` on `EQUITY_MASTER_SERVER`, so this needs
no new connection and no new setting, and they are the same two tables
`blp_lib.q` rounds by. Per name rather than per venue matters: one venue can
hold two boards whose ladders differ, and nothing here has to know that.

```
ticksizeids  sym=`000020.KS        ->  id `6132
ticksizetbl  id=`6132              ->  price 2000 tick 1
                                       price 5000 tick 5
                                       price 20000 tick 10   ...
```

> **kdb's bounds run the opposite way to ours.** Its `price` is an *exclusive
> upper* bound — `first ticksize where px < price` — while `ticks.tick_for`
> takes the highest floor *at or below* the price. `ticks.from_kdb` converts
> between them, and its self-test checks every boundary against a transcription
> of `blp_lib.q`'s own lookup, because getting it backwards would round every
> price to its neighbouring tier.

Only Indonesia still names a `TickSource`, and that is deliberate: its ladder
**is** the ATS's own file, so a copy from anywhere else could drift from what
the trading system actually rounds by. A venue that names one uses it; every
other venue asks kdb.

**A name kdb has no ladder for is reported, not published unrounded** — an
unrounded limit is one the exchange rejects. The run prints the count and lists
the first few, and `--kdb-check` fetches ladders for its sample so coverage can
be checked in seconds rather than discovered by a live run.

### A computed name with no close falls back to Bloomberg

A `computed` name that `equity_master` has no close for is **asked of Bloomberg
rather than dropped.** A `PX_LAST` of zero counts as no close and always has —
`kdbclose._to_decimal` refuses anything `<= 0`, because a zero close otherwise
computes a band of zero to zero.

This is the reason the kdb side runs first. Those names are known *before* the
B-PIPE request is built, so they ride along in the **same** request; the
fallback costs no extra round trip, only a longer security list.

They are priced off **Bloomberg's own limits, not the venue's band** — a
different arithmetic from their neighbours on the same venue. That is the point
(it is what recovers a name the tiers cannot price) but it does mean a venue's
output is not uniformly one method.

A name that fails *both* keeps both halves of the story:

```
excluded  1  no close in equity_master, then no answer from Bloomberg
  JKT-MAIN  1  NOCL.JK (NOCL IJ)
```

Dropping it under a bare Bloomberg reason would hide that kdb is what failed
first.

**It is per venue**, in `markets.csv`'s `NoCloseFallback` column: `bloomberg`
asks, blank drops as before. Blank is the default on purpose — a venue only
asks once someone has written it down.

As shipped, every computed venue asks **except Indonesia**. Japan, Thailand and
India are `Source=bloomberg` and never reach the fallback at all, so their
column is blank too.

### A leveraged product does not get the venue's band

`bands.csv` has a `NameMarker` column. A row carrying one applies only to
securities whose **exchange name** contains that word, matched case
insensitively; a blank marker is the venue default, exactly as `SymPrefix` is.

```
KSC-MAIN,pct,,0,0.30,0.30,
KSC-MAIN,pct,,0,0.60,0.60,leverage
KSC-MAIN,pct,,0,0.15,0.15,0.5x
KSC-MAIN,pct,,0,0.60,0.60,2x
KSC-MAIN,pct,,0,0.90,0.90,3x
KSC-MAIN,pct,,0,0.30,0.30,inverse
```

**The multiple is the signal, not the word "inverse".** A −1x product moves like
anything else and takes the ordinary band; a ±2x takes twice it. Real names from
a run's `excluded.csv` show why a rule keyed on the word would miss cases:

| name | multiple | band |
|---|---|---|
| `SAMSUNG KODEX Inverse ETF` | −1x | 30% |
| `SAMSUNG KODEX Inverse 0.5X ETN` | −0.5x | 15% |
| `Samsung KODEX 200 Futures Inverse 2X ETF` | −2x | 60% |
| `SAMSUNG KODEX Inverse 3X ETN` | −3x | 90% |
| `Shinhan Bloomberg -2X WTI Futures ETN B 94` | −2x | 60% — no "inverse" in the name |
| `Shinhan SOL ... leverage ETF` | 2x | 60% — leverage means 2x in Korea |

**A multiple outranks a word marker**, whatever their lengths. `Inverse 3X`
matches both `inverse` and `3x`, and on length alone the longer `inverse` would
win and publish a 3x product at a third of its real width. The sign is dropped:
a −2x and a 2x move the same distance, and a band is a width.

**A multiple with no row is refused, never given the default.** `KODEX 4X
Futures ETN` matches no marker at all, so without that guard it would fall
through to the blank row and publish at a quarter of its width. It reports as
`leveraged` instead, carrying the name.

**The exchange does not always leave spaces, or spell it the same way.** Real
`LONG_COMP_NAME` values that a word-boundary match found nothing in:

```
Inverse2X      3XLeverage      Leverege      Inver
```

A marker that is itself a multiple is matched *as a multiple*, not as a word, so
`2x` finds the 2 in `Inverse2X` where no boundary rule will — while `MATRIX 2XL`
is still not a 2x product, because what follows a multiple has to be a word that
makes it one. `Leverege` is a `bands.csv` row of its own: a band is not the place
to be precious about an issuer's typo. Word markers still match on boundaries, so
`Coverage Analytics` is not leveraged.

`Inver` is a name the feed **truncated**, and a truncated name cannot say which
multiple it is — that one is a 2x, so the ordinary band would have been half its
real width. It is refused and reported rather than guessed.

**These names round to the NEAREST tick, not inward.** `bands.csv` has a
`Rounding` column per tier; blank means the venue's mode, so an ordinary Korean
name still rounds inward and only the ETF/ETN family differs. Four values off a
live run pin it:

```
66367.6 -> 66370      35736.4 -> 35735
 1527.6 ->  1530       1522.4 ->  1520
```

Inward sends every one of them the other way, and `35736.4` is what rules out a
10 tick as well — nearest would make it 35740 there, and the exchange publishes
35735. The 5 comes from table 10392, flat above 2,000.

Korea prices a leveraged product at twice the ordinary band. `0080Y0 KP` closed
at 8,025 and the exchange published 12,835/3,215 — ±60% where the plain row says
30. Nothing in the *ticker* says so: its crosscode `Type` is `ETF`, identical to
an ordinary unleveraged one, and there is no prefix to match the way China has
688 and 300. The name does say so, and `equity_master.LONG_COMP_NAME` carries
it: *"Shinhan SOL Shipbuilding TOP3 Plus leverage ETF"*. It rides on the query
that already fetches the close, so both arrive together and describe the same
listing.

**A marker with no row is still refused.** 60% is Korea's answer for `leverage`;
it is not established for an inverse, which may track −1x and get the ordinary
band. Those report as `leveraged` in `excluded.csv`, carrying the name that
caught them. What is written down is used; what is not is reported, never
guessed.

### And the other way: Bloomberg would not price it, so compute

`NoDataFallback=computed` in `markets.csv` does the reverse. A name B-PIPE
refuses, has no answer for, or answers without a usable limit is given **the
venue's own band** instead of being dropped.

**Blank on every venue today, and it has to be** — `marketcfg` refuses the
column on a venue with no tiers in `bands.csv`, and none of the six `bloomberg`
venues has any. Tokyo's limits are an absolute step table nobody has
transcribed; Thailand's and India's rules are not written down either. Write a
venue's tiers and the column becomes available to it.

**The ordering is the awkward part.** kdb runs *before* B-PIPE, so at the moment
the closes are fetched we do not yet know which names Bloomberg will fail. So
the closes for every name on a fallback venue are fetched up front, in the
request already going out, and most are never used. That costs nothing while the
column is blank, and the alternative is a second kdb round trip after Bloomberg.

**A rescued name is still an entitlement we do not hold.** The EID report reads
B-PIPE's refusals as B-PIPE made them, before any retry, so publishing a
computed row does not quietly remove the name from `entitlement_refused.csv` —
the missing contract is a fact about the contract, not about whether arithmetic
saved the row.

A name that fails both keeps both halves: `no data from Bloomberg, then no
previous close in equity_master`.

### The run log, and how each name was priced

Everything a real run prints — progress, the report, a
fatal error with its traceback — also goes to
`LOG_DIR\LimitUpDown-YYYYMMDD-HHMMSS.log`, one file per run. `LOG_DIR` is set in
`local_settings.py` and defaults to `logs\` beside the script.

Every run then mails `LimitUpDown SUCCEEDED` or `LimitUpDown FAILED` to
`EMAIL_TO`, with that log attached — including a run that stopped at a startup
check or crashed. The mail goes out after the log is closed, so the attachment
is the whole run. A mail server that is down is reported on the console and
does not turn a good run into a failed one.

`sources.csv`, written beside the output every run, lists every published row
with how it was priced:

```
ReutersCode,BloombergCode,Venue,Source
7203.T,7203 JT,TYO-MAIN,bloomberg
A.KS,A KP,KSC-MAIN,computed
```

**The path the name ended on is what counts.** Bloomberg would not price it and
the band was computed instead: `computed`. A computed name with no close that
Bloomberg rescued: `bloomberg`.

The log's report carries the same split as a ratio of the published rows:
`Computed: 20% (3200)  Bloomberg: 80% (12800)`.

### Which names did not make the file

`excluded.csv`, written beside the output every run:

```
ReutersCode,BloombergCode,Venue,Missing,Reason,Detail
A.KS,A KP,KSC-MAIN,close,no previous close in equity_master,
B.KQ,B KQ,KOE-MAIN,ladder,no tick ladder for this name,close 5150
C.JK,C IJ,JKT-MAIN,close-and-bloomberg,"no close ..., then no answer ...",
```

**`Missing` names the input that was absent**, in one word, so the file filters
by it — `close`, `ladder`, `band-tier`, `entitlement`, `no-answer` and so on.
The `Reason` says the same thing in prose; `Missing` is what makes "how many
names did we lose for want of a close" a filter rather than a reading exercise.
`Detail` carries what we *did* have, so a name dropped for want of a ladder
still shows the close it had. The run report totals the same tokens.

**Every** dropped name with its reason — the run report shows the first five per
venue and then `(+N more)`, which is right for reading and useless for answering
"which names, exactly". A run that dropped nothing still writes the header, so
an empty file is never yesterday's left behind. `entitlement_refused.csv` is
still written alongside it and still covers only the EID refusals, which are
what a market-data team acts on.

### The one arithmetic gap, and it is China's

The China tiers key the wider band off the **ticker prefix**: `688` for the
STAR board and `300` for ChiNext get ±20%, everything else ±10%. That is
correct for the boards, but **ST and \*ST names are capped at ±5% and nothing
here knows which they are** — the crosscode carries no such flag, so they get
±10% and the band comes out twice as wide as the exchange allows.

Bloomberg knew. This is the one thing the computed path gives up, and it
applies to every Chinese venue now that they are all computed. Before Prod,
either find a source for the ST flag or keep China on `bloomberg`.

## Before this goes anywhere near Prod

1. **Set `EQUITY_MASTER_SERVER`**, and check the `sym hit` counts in the run
   report. A market whose count is zero is a market whose key does not
   resolve, and it will publish nothing.
2. **China's ST names** — see above. The only known arithmetic gap.
3. **Coverage per venue against the file in production today.** `--compare`
   exists exactly for this, and it matters more now than it did: twelve
   markets just changed where their numbers come from, so the diff against
   yesterday's Bloomberg-sourced file is the check that the tiers agree with
   what the exchange actually did.

## other/

| | |
|---|---|
| `bpipe_probe.py` | one name, three ways: does B-PIPE serve its limits, and when? |
| `bpipe_fields.py` | every real-time field B-PIPE will serve, as a CSV |
| `bpipe_history.py` | can we get back the session that just finished? |
| `bpipe_auth.py` | which identity do we have, and does it hold the EIDs we were refused? |
| `em_probe.py` | what does equity_master carry, and can it identify a Chinese ST name? |
| `bpipe_get.py` | ask B-PIPE for whichever fields you name, on whichever securities you name |

Connection settings live at the top of `bpipe_probe.py` and are shared by all
three. They ship empty.

`bpipe_auth.py` exists because a refusal reading *"EID(s) needed: 64487 or
64488"* is not a bug and no code change reaches it. It tries three
authentication modes — application-only (what the job uses), user-only by OS
logon, and both — and reports which authorize and which hold the EIDs. The
question it settles first is whether a **user** account is reachable from the
machine at all, since an application login is usually narrower than a person's,
and switching identity costs nothing where buying an entitlement does not.

`bpipe_history.py` asks the same entitlement question one layer up. The field
list above says which *fields* we are served; it says nothing about whether we
may replay a finished day. So the probe asks `//blp/refdata` for one name over
one session three ways — `IntradayTickRequest`, `IntradayBarRequest` and
`HistoricalDataRequest` — because an entitlement can carry bars while barring
raw ticks, and that only shows up if you ask for both. It keeps *refused*,
*errored* and *served but empty* apart in the output, since a Tokyo holiday and
a missing entitlement both return nothing and only one of them is a finding.

## Documentation

- why it is built this way, and what the R job does:
  [`../docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md`](../docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md)
- the R job itself: `no_git/LimitUpDown.r`, which is not published here
