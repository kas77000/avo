# LimitUpDown configuration

Three files, replacing `config_cash.xml` and generalising
`Indo_maping_limit_up.csv`.

| File | One row per |
|---|---|
| `markets.csv` | venue |
| `bands.csv` | venue per band tier |
| `ticks.csv` | venue per tick tier |

The key everywhere is **`FidessaVenueID`**. `BBGVenueCode` is carried as an
attribute but is NOT unique - China's `CG` and `CS` each map to two venues -
so it can never be used as a lookup key.

`KOE-MAIN -> KQ` and `KSC-MAIN -> KP` are correct as written, verified against
`config_cash.xml`. They look swapped. They are not.

## WARNING: ticks.csv holds placeholder values

Every venue except JKT-MAIN currently has a single flat tick tier that is
almost certainly wrong. Real exchange tick ladders must be entered before this
job publishes to Prod - see Task 9 of
`docs/superpowers/plans/2026-09-01-limit-up-down-python.md`.

Indonesia reads `spol_JKT.tsr` from the ATS instead, which cannot drift from
the trading system.

## spol_JKT.tsr is also a placeholder

The authoritative file is on the ATS share (`TSRIndo` in `config_cash.xml`).
Point `TSR_DIR` in `local_settings.py` at that folder and this local copy is
never read. It exists so `marketcfg.py --self-test` can validate the shipped
config on a machine with no network shares.
