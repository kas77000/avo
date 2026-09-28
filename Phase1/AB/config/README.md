# Phase1 AB config

## close_conditions.csv

The closing phase of each market we use: the `MarketPhase` whose `TypeADVCC`
is `ACV` in the legacy `MarketConditionBBG.xml`. Transcribed on 2026-09-28
from the photos in `no_git/MarketConditionBbg/`.

It has one row per Bloomberg exchange code in
`Phase0/AB/Historical/config/composites.csv` (18 codes). The XML's other
entries are left out: composite-only codes, `HK CAS`, `H1`, the other
Japanese venues, Pakistan and Europe. Add a row here when a code is added
there.

Part 1 uses it to find the closing trade in qatt. A market that was closed
that day falls back to `equity_master`.

| Column | Meaning |
|---|---|
| `BBGCode` | the XML's `BBGName`, the Bloomberg exchange code |
| `Country`, `Venue` | as in the XML |
| `UnixTimeZone` | the zone the times below are in |
| `StartTime`, `EndTime` | the phase's `StartTimeXml` / `EndTimeXml` |
| `AbaqueTime`, `AbaqueEndTime` | `AbaqueTimeXml` / `AbaqueEndTimeXml`, blank where the XML has none |
| `CondType` | `Sum`, `Period`, `Flag` or `ContinuousSum`, verbatim |
| `CloseCondCodes` | the phase's `CondCodes`, pipe-separated, verbatim |

`#N/A N.A.` is kept as the XML spells it. It is Bloomberg's "no condition
code", which is a trade with an empty condition in qatt. Where it is listed
(`C2`, `CS`, `HK`, `NZ`, `SP`, `TT`), a trade with no condition inside the
window counts as part of the close.

Japan's close code is a lowercase `e`.
