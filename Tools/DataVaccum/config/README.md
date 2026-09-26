# DataVaccum config

`files.csv` — one row per file to fetch.

| Column | Meaning |
|---|---|
| `Name` | a short label. Shown in the run's output, and what `--only` takes. Must be unique. |
| `SourcePath` | the full UNC path of the file on the share, **including the filename**. Not a directory, not a wildcard. |
| `Route` | `direct` or `server` — see below |

```csv
Name,SourcePath,Route
positions,\\finance-nas\risk\Positions.csv,direct
prices,\\segb-vol1\marketdata\Prices.dat,server
```

## Choosing the Route

`direct` means this machine copies from the share itself. `server` means the
server copies it to the staging folder first and this machine takes it from
there.

**Don't agonise over it — run `python vaccum.py --probe`.** It resolves every
path from both ends and tells you which rows have the wrong route, including
the `server` rows that would work as `direct`. Prefer `direct` where it
works: one hop instead of two, and no credentials involved.

## Why it must be a UNC path

A mapped drive letter is per-user and per-machine. `S:` on your desktop and
`S:` on the server are not required to be the same share, and under a WinRM
session the mapping usually does not exist at all — so a drive letter that
works when you are sitting at the machine resolves to nothing when the
script runs. The parser rejects drive letters for that reason rather than
letting them fail later and less clearly.

## Why one file per row, no wildcards

The job is a known list of files fetched daily. A wildcard makes "how many
files should have arrived?" unanswerable, which turns a share that quietly
stopped publishing into a run that still reports success. A named row that
does not arrive is a failure, and says so.

Edit in Excel. Keep it comma-separated with the header intact.
