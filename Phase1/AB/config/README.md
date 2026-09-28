# Phase1 AB config

## close_conditions.csv

The condition codes of each market's closing trade, taken from the closing
phase (the `MarketPhase` whose `TypeADVCC` is `ACV`) of the legacy
`MarketConditionBBG.xml`. Transcribed on 2026-09-28 from the photos in
`no_git/MarketConditionBbg/`.

It has one row per Bloomberg exchange code in
`Phase0/AB/Historical/config/composites.csv` (18 codes). Add a row here when
a code is added there.

Part 1 uses it to find the close in qatt. A market that was closed that day
falls back to `equity_master`.

| Column | Meaning |
|---|---|
| `BBGCode` | the XML's `BBGName`, the Bloomberg exchange code |
| `Country`, `Venue` | as in the XML |
| `CloseCondCodes` | the phase's `CondCodes`, pipe-separated, verbatim |

The condition code alone decides the close. The XML's phase times are not
carried over.

`#N/A N.A.` is kept as the XML spells it. It is Bloomberg's "no condition
code", which is a trade with an empty condition in qatt. `C2`, `CS`, `HK`,
`NZ`, `SP` and `TT` list it, and in those markets an empty condition also
marks every continuous trade. So the close is the **last** trade of the day
whose condition is in the list, not any matching trade.

Japan's close code is a lowercase `e`.
