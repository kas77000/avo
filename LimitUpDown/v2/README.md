# LimitUpDown v2 — ask Bloomberg, except Indonesia

Builds `limitUpDown.csv`, the daily price-band file the Nova ATS uses to bound
orders. **Bloomberg supplies the band for every market except Indonesia, which
is computed** from a tier table and a tick ladder. The split is declared in
`config/markets.csv`, not written into the code.

```
Source=bloomberg   MIN_LIMIT / MAX_LIMIT off B-PIPE
Source=computed    band = f(previous close, tiers), rounded to the tick
```

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
`../other/bpipe_probe.py`.

**Which previous-close field answers is not yet known.** Rather than guess one
mnemonic and get an empty Indonesia, the job asks for all three, uses the first
that answers per name, and prints the tally every run:

```
  close from   412  PREV_CLOSE_VALUE_REALTIME
```

The first real run tells you which candidate to keep — then delete the others
from `bpipe.PREV_CLOSE_FIELDS`. `PX_YEST_CLOSE` is **not** requested: it is
static and confirmed unavailable on this subscription, so asking would cost a
refusal per name and buy nothing.

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

## v1 or v2?

| | v1 | v2 |
|---|---|---|
| Computes | every market, from rules | only Indonesia, as R does |
| Asks Bloomberg | nothing | everything else |
| Needs | kdb | B-PIPE |
| `bands.csv` holds | six markets | one |
| Breaks when | a market changes its rule and nobody edits the CSV | Bloomberg has no limit for a name |

They are alternatives, not stages.

## Running

```
python limit_up_down.py --self-test        checks, no Bloomberg, no files
python limit_up_down.py --demo             both branches on canned data
python limit_up_down.py ""                 real run, publish nowhere
python limit_up_down.py "Test|Pilot|Prod"  real run, publish
python limit_up_down.py --compare OLD.csv  diff the last output against another
```

`--self-test` and `--demo` need nothing but Python. Every module has its own:

```
python bands.py --self-test        python marketcfg.py --self-test
python ticks.py --self-test        python crosscode.py --self-test
python bpipe.py --self-test        python mailer.py --self-test
```

## First run

```
pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
copy local_settings.py.example local_settings.py
```

Fill in `BPIPE_HOST`, `BPIPE_PORT`, `BPIPE_APP`, `TSR_DIR` and the SMTP host.
The B-PIPE three have no defaults — the job refuses to start rather than connect
somewhere you did not mean.

**Keep `OUT_TEMP` different from v1's** while both are running, or whichever
finishes last is the file that gets published.

## Where things live

| | |
|---|---|
| `bpipe.py` | session, authorization, batched fetch. The only module that imports blpapi. |
| `bands.py` | tier selection, band arithmetic, tick rounding. Pure. Copied from v1. |
| `ticks.py` | tick ladders from a `.tsr` file. Pure. Copied from v1. |
| `marketcfg.py` | loads the config **and enforces the split** |
| `crosscode.py` | CrossCode.csv → the universe, filtered and deduplicated |
| `limit_up_down.py` | orchestration, validation, environment copy |
| `config/markets.csv` | one row per venue: cutoff, and which side of the split |
| `config/bands.csv` | tiers, for computed venues only — today, Indonesia |
| `config/spol_JKT.tsr` | **placeholder.** Point `TSR_DIR` at the ATS share. |

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

Seven countries, fifteen venues:

| | venues | cutoff | source |
|---|---|---|---|
| Japan | `TYO-MAIN` (JT), `JNX-MAIN` (JE), `CHJ-MAIN` (JI) | 07:30 | bloomberg |
| Korea | `KOE-MAIN`, `KSC-MAIN` | 07:30 | bloomberg |
| Malaysia | `KLS-MAIN` | 07:59 | bloomberg |
| Taiwan | `TAI-MAIN` | 07:59 | bloomberg |
| Indonesia | `JKT-MAIN` | 07:59 | **computed** |
| China | `SHA`, `SHH`, `SSC`, `SZA`, `SHZ`, `SZC` | 09:03 | bloomberg |
| Philippines | `PHS-MAIN` | 09:03 | bloomberg |

**Thailand and India are out, deliberately** — not merely unlisted. Adding either
is more than a config row: Thailand needs a `/F|/Q` foreign-ticker filter, and
India needs the static-limit exclusion — names configured in the ATS strategy
file must NOT get a published limit — plus the BSE secondary venue. Neither
filter is written here, so a bare config row would publish limits for names that
must not have them.

## Before this goes anywhere near Prod

1. ~~Confirm which previous-close field answers.~~ **Done** —
   `PREV_CLOSE_VALUE_REALTIME` answered for all 884 Indonesian names on
   2026-09-04. The other two stay as a free fallback.
2. **Coverage per venue against the file in production today.**
   `--compare` exists for this. A market Bloomberg will not price is a
   market this version cannot publish, and the row count is where that
   shows.
