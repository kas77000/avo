# LimitUpDown

Builds `limitUpDown.csv`, the daily price-band file the Nova ATS uses to bound
orders. Two implementations that disagree about where the band should come
from, and the probes used to settle the question.

```
v1/       compute the band from rules + a kdb reference price
v2/       ask Bloomberg for the band, over B-PIPE
other/    the B-PIPE probes that made v2 possible
```

Both write the same seven columns:

```
#ReutersCode,BloombergCode,LimitDate,LimitUpPrice,LimitDownPrice,FidessaCode,Venue
7203.T,7203 JT,2026-09-03,3833,2433,7203.JP,TYO-MAIN
```

## The two versions

|  | v1 | v2 |
|---|---|---|
| Band from | rules in `config/bands.csv`, applied to a kdb reference price | Bloomberg, except Indonesia |
| Depends on | kdb | B-PIPE |
| Computes | every market | only Indonesia |
| `bands.csv` holds | six markets | one |
| Markets | six | seven countries, fifteen venues |
| Breaks when | a market changes its rule and nobody edits the CSV | Bloomberg has no limit for a name |
| Open risk | China ST names get ±10% instead of ±5% — no source for the flag | coverage: unknown until measured per venue |

**They are alternatives, not stages.** v1 removes the Bloomberg dependency
entirely and pays for it by encoding every market's rule in config. v2 keeps the
dependency and computes only the one market Bloomberg does not price for us, so
its arithmetic is one market wide instead of six.

Either can be evaluated without touching the other: `--self-test` and `--demo`
run on any machine, with no kdb, no Bloomberg and no network shares.

## The finding that produced v2

Bloomberg carries daily price limits under two sets of names, and our B-PIPE
entitlement serves only one of them. On 2026-09-03 the **static reference**
fields `PX_MAX_LIMIT`, `PX_MIN_LIMIT` and `PX_LAST` came back *"Field not
permitted to datafeed users"* — on the same request where the **real-time**
`MIN_LIMIT` / `MAX_LIMIT` answered with 2433.0 / 3833.0 for `7203 JT Equity`.

The two families are not two spellings of one field. Anything v2 needs must be
found in the real-time family, and sometimes there is no equivalent.
`other/bpipe_fields.py` lists what that family contains.

## other/

| | |
|---|---|
| `bpipe_probe.py` | one name, three ways: does B-PIPE serve its limits, and when? |
| `bpipe_fields.py` | every real-time field B-PIPE will serve, as a CSV |
| `bpipe_history.py` | can we get back the session that just finished? |
| `bpipe_auth.py` | which identity do we have, and does it hold the EIDs we were refused? |
| `em_probe.py` | what does equity_master carry, and can it identify a Chinese ST name? |

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

- v1, in detail: [`../docs/limit-up-down-how-it-works.md`](../docs/limit-up-down-how-it-works.md)
- why v1 is built that way: [`../docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md`](../docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md)
- v2: [`v2/README.md`](v2/README.md)
