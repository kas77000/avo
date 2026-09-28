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
| `CloseCondCodes` | the codes that mark the close, pipe-separated |

The condition code alone decides the close. The XML's phase times are not
carried over.

Only codes that mark the close are kept. A code that also appears in the
same market's continuous phase is dropped:

| Dropped | From | Why |
|---|---|---|
| `#N/A N.A.` | C2, CS, HK, NZ, SP, TT | Bloomberg's "no condition code", which every continuous trade has |
| `UL`, `DL` | TT | limit up and limit down, also in continuous trading |
| `D`, `F`, `TS` | IJ | also in continuous trading |

Japan's `e` also marks the morning close (`AE|e`), so the close is the
**last** trade of the day whose condition is in the list.
