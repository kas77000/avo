# LimitUpDown configuration

Two files, replacing `config_cash.xml` and generalising
`Indo_maping_limit_up.csv`.

| File | One row per |
|---|---|
| `markets.csv` | venue |
| `bands.csv` | venue per band tier |

The key everywhere is **`FidessaVenueID`**. `BBGVenueCode` is carried as an
attribute but is NOT unique - China's `CG` and `CS` each map to two venues -
so it can never be used as a lookup key.

`KOE-MAIN -> KQ` and `KSC-MAIN -> KP` are correct as written, verified against
`config_cash.xml`. They look swapped. They are not.

## Most markets do not round

`Rounding=none` is the normal setting and needs no tick table: the band is
`ref x (1 +/- pct)` and that number is published as it comes out.

Only Indonesia rounds, exactly as `LimitUpDown.r` does today, because
Indonesia is the one market the R job computed rather than read from
Bloomberg. Japan will round too when it arrives.

A venue that rounds names a `TickSource`; a venue that does not must leave it
blank. Getting that pair wrong is a config error, not a silent default.

### If a market later needs rounding

1. Set its `Rounding` to `inward`, `outward` or `nearest`.
2. Point `TickSource` at a `.tsr` file, or at `config` and add a `ticks.csv`
   with `FidessaVenueID,FloorFrom,Tick` rows.

No code change either way.

## spol_JKT.tsr here is a placeholder

The authoritative file is on the ATS share (`TSRIndo` in `config_cash.xml`).
Point `TSR_DIR` in `local_settings.py` at that folder and this local copy is
never read. It exists so `marketcfg.py --self-test` can validate the shipped
config on a machine with no network shares.
