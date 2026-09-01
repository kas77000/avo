# LimitUpDown

Builds `limitUpDown.csv`, the daily price-band file the Nova ATS uses to bound
orders. Python replacement for `LimitUpDown.r`, with **no Bloomberg
dependency**: every band in scope is arithmetic on a reference price, and the
rules live in `config/`, not in the code.

Markets: Indonesia, Malaysia, Korea, Philippines, China, Taiwan.
Japan, Thailand and India are config additions later - see the spec.

**How it works, in detail:**
[`../docs/limit-up-down-how-it-works.md`](../docs/limit-up-down-how-it-works.md)

## Running

```
python limit_up_down.py --self-test        arithmetic checks, no kdb, no files
python limit_up_down.py --demo             the whole pipeline on canned data
python limit_up_down.py ""                 real run, publish nowhere
python limit_up_down.py "Test|Pilot|Prod"  real run, publish
python limit_up_down.py --compare OLD.csv  diff the last output against R's
```

`--self-test` and `--demo` need nothing but Python: no kdb, no pykx, no q
licence, no network shares. Every module has its own:

```
python ticks.py --self-test
python bands.py --self-test
python marketcfg.py --self-test
python crosscode.py --self-test
python kdbsource.py --self-test
python mailer.py --self-test
```

## First run

Copy `local_settings.py.example` to `local_settings.py` and fill in the kdb
server and SMTP host. The paths are already those from `config_cash.xml`.

## Where things live

| | |
|---|---|
| `bands.py` | tier selection, band arithmetic, tick rounding. Pure. |
| `ticks.py` | tick ladders from a `.tsr` file or config rows. Pure. |
| `marketcfg.py` | loads and validates the three config CSVs |
| `crosscode.py` | reads CrossCode.csv, filters by type, venue and cutoff |
| `kdbsource.py` | reference prices. The only module that imports pykx. |
| `limit_up_down.py` | orchestration, validation, environment copy |
| `config/` | the market rules - see `config/README.md` |

## Before this goes to Prod

1. **China ST / \*ST names get ±10% instead of ±5%** because we have no source
   for the ST flag. The error is in the dangerous direction. See §10.1 of
   `../docs/superpowers/specs/2026-09-01-limit-up-down-python-design.md`.
2. Run in parallel with the R job and reconcile with `--compare`.

Only Indonesia rounds to a tick, as the R job does. Every other market
publishes `ref x (1 +/- pct)` as computed - see the note in `bands.py` for
why rounding does not change which orders the band admits.
