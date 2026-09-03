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
  field        412  PX_YEST_CLOSE: Field not permitted to datafeed users
```

`PX_YEST_CLOSE` rides along purely as a diagnostic. The first real run tells you
which candidate to keep — then delete the others from `bpipe.PREV_CLOSE_FIELDS`.

## The status filter

Only ACTV names are published, and the status comes from **CrossCode's own
`BloombergStatus` column** — not from Bloomberg. It is already in the
file we read to build the universe, and `dedupe` already trusts it to choose
between two rows claiming one code. The filter applies it once more, to the
case dedupe never sees: a delisted name that had no competitor to lose to.

That removes a dependency rather than adding one. `MARKET_STATUS` is a
**static** field, from the same family as `PX_LAST` — the one our
entitlement refused. It is still requested and still honoured as a
cross-check when it is served, but nothing depends on it any more.

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

This job runs **07:30–09:03 Hong Kong**, which is pre-open or closed for every
market in scope. Filtering on a session field would read "not open" for the
entire universe and publish an empty file, every day. `RT_SIMP_SEC_STATUS` is
the axis worth testing.

Both real-time candidates are requested and **tallied but never filtered on**, so
one run shows what they carry:

```
  status      2841  RT_SIMP_SEC_STATUS = TRADING
  status        17  RT_SIMP_SEC_STATUS = HALTED
  status      2858  RT_EXCH_MARKET_STATUS = CLOSED     <- why it cannot be the filter
```

When the values are known, set `bpipe.STATUS_FIELD` and `STATUS_ACTIVE`.

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

1. **Confirm which previous-close field answers**, then prune the candidates.
2. **Coverage per venue against the file in production today.**
   `--compare` exists for this. A market Bloomberg will not price is a
   market this version cannot publish, and the row count is where that
   shows.
