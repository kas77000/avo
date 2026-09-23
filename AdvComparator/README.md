# ADV Comparator

Two ADV files for one market, **old** and **new**, compared column by column
on one HTML page.

```
python compare_adv.py OLD.csv NEW.csv
python compare_adv.py OLD.csv NEW.csv -t 50
```

```
old  GlobalAdv_Hong_Kong.csv  (3,074 instruments)
new  GlobalAdv_Hong_Kong_nova.csv  (3,228 instruments)

 3,070 in both   4 only in old   158 only in new

  MeanAov                  7 above 100%
  MeanAiv                  5 above 100%
  ...

 wrote GlobalAdv_Hong_Kong_comparison.html
```

Python 3 only, nothing to install.

| Option | Default | |
|---|---|---|
| `-t PCT`, `--threshold PCT` | `100` | list the instruments whose change is more than this, in percent, either way |
| `--old-name NAME` | `Bcore` | what the page calls the old file |
| `--new-name NAME` | `Nova` | what the page calls the new file |
| `--out DIR` | beside the old file | where the page goes |
| `--self-test` | | checks on made-up files, no input needed |

## What it compares

A row is an instrument, keyed on `#FidessaCode` (or on the first column if
there is no such header). Every column that holds numbers in both files, such
as `MeanAov`, `MeanAiv`, `MeanAcv` and `MeanAdv`, gets its own section, in the
old file's column order. A column with no numbers, such as a venue, is
skipped. A column that is only in one file is named in the terminal output.

## What each section shows

- **A scatter plot of old against new**, log scale on both axes, with the
  dashed *No change* diagonal. It uses every instrument in both files. A dot
  above the line went up, and a dot below it went down. Zero cannot go on a
  log axis, so a value of 0 is drawn at 10⁻¹². Hover over a dot to see its
  code and both values.
- **Differences greater than `-t`%**: `#FidessaCode`, old, new, change and
  direction. Change is `(new − old) / old`. Rows where old is 0 and new is not
  come first, marked `N/A (old = 0)`. The rest are sorted with the largest
  change first.

The page is named after the old file, so `GlobalAdv_Hong_Kong.csv` gives
`GlobalAdv_Hong_Kong_comparison.html`, titled *GlobalAdv Hong Kong: old vs
new*. It is a single self-contained file (inline SVG, nothing loaded from the
network), so it can be mailed. It is always dark, whatever the browser's
theme.

## Input

A CSV with a header row. The delimiter can be `,` `;` tab or `|`. A blank or
non-numeric cell is left out of that column's chart and table.
