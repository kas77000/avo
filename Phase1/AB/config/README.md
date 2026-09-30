# Phase1 AB config

## close_conditions.csv

The condition codes of each market's closing trade, taken from the closing
phase (the `MarketPhase` whose `TypeADVCC` is `ACV`) of the legacy
`MarketConditionBBG.xml`. Transcribed on 2026-09-28 from the photos in
`no_git/MarketConditionBbg/`.

It has one row per Bloomberg exchange code in
`Phase0/AB/Historical/config/composites.csv` (18 codes). Add a row here when
a code is added there.

Part 1 uses it to find the close in qatt. A name with no trade carrying one
of these codes takes its close from `equity_master` instead, whether its
market was closed that day or it simply had no closing trade. Every fallback
is written to the log file with the name, its market and the reason, so the
situation can be traced afterwards.

| Column | Meaning |
|---|---|
| `BBGCode` | the XML's `BBGName`, the Bloomberg exchange code |
| `Country`, `Venue` | as in the XML |
| `CloseCondCodes` | the codes that mark the close, pipe-separated |
| `LastTradeBefore` | optional, a time in **HKT** (`HH:MM` or `HH:MM:SS`): a name with no closing print closes at its last print at or before this time, not the day's last. Blank: the day's last print. Set only for `KQ`, `14:30:00` (15:30 in Seoul), so that KOSDAQ's after-market prints are not taken as the close |

The condition code alone decides the close; the XML's phase times are not
carried over. `LastTradeBefore` only bounds the fallback when no print
carries a close code: a closing print after it still wins. A name whose
prints all come after it takes equity_master's `PX_LAST`, with the reason
`none-before-cutoff`. A malformed time stops the extract, naming the code.

Only codes that mark the close are kept. A code that also appears in the
same market's continuous phase is dropped:

| Dropped | From | Why |
|---|---|---|
| `#N/A N.A.` | C2, CS, HK, NZ, SP, TT | Bloomberg's "no condition code", which every continuous trade has |
| `UL`, `DL` | TT | limit up and limit down, also in continuous trading |
| `D`, `F`, `TS` | IJ | also in continuous trading |

Japan's `e` also marks the morning close (`AE|e`), so the close is the
**last** trade of the day whose condition is in the list.
