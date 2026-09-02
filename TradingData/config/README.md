# TradingData config

`markets.csv` — one row per Fidessa market.

| Column | Meaning |
|---|---|
| `FidessaMarket` | the crosscode's `FidessaMarket` value |
| `BBGComposite` | Bloomberg composite exchange code, used as the fallback when building an `equity_master` sym. Blank means "only try the crosscode's own suffix". |
| `NoShortSell` | `TRUE` for the markets listed at `:314-320`, else `FALSE` |
| `RespectShortSellPrice` | `TRUE` for the markets at `:302-306`, blank otherwise — the R job's default is `NA`, which writes as empty |

A market absent from this file gets `NoShortSell=FALSE`, blank
`RespectShortSellPrice`, and no composite fallback. The run reports any market
in the crosscode that has no row here.

## Why BBGComposite is here

`equity_master` is keyed on `(date, sym)`, where `sym` is a ticker dot-joined to
an exchange code — but *which* code is not settled. Korea's `KSC-MAIN` has venue
code `KP` and composite `KS`; China's `SSC-MAIN` has venue `C1` and composite
`CH`. The codes we were given for `sym` mix both kinds.

So sym resolution tries two candidates per row: the crosscode's own
`BloombergCode` suffix first, then this composite. The run reports which
suffixes actually hit, which settles the question on the first live run instead
of hardcoding a guess now.

Edit in Excel. Keep it comma-separated with the header intact.
