#!/usr/bin/env python3
"""Two ADV files of one market, compared column by column.

    python compare_adv.py OLD.csv NEW.csv
    python compare_adv.py OLD.csv NEW.csv -t 50       list changes above 50%
    python compare_adv.py OLD.csv NEW.csv --old-name Bcore --new-name Nova
    python compare_adv.py --self-test

A row is an instrument, keyed on #FidessaCode (the first column when there
is no such header).  Every column that holds numbers in both files - MeanAov,
MeanAiv, MeanAcv, MeanAdv, ... - gets one section of the page, in the old
file's column order:

    a scatter of old against new over the instruments in both files, log on
    both axes, with the no-change diagonal.  Zero and negative values cannot
    sit on a log axis, so they are drawn at 1e-12, the floor of the axis.

    the instruments whose change, (new - old) / old, is more than
    --threshold percent (100 by default) either way, largest first.  An old
    value of 0 with a new one that is not is "N/A (old = 0)" and comes first.

WHAT IT WRITES
    <old file name>_comparison.html, beside the old file or in --out.  One
    self-contained page, inline SVG, nothing loaded from the network - it
    can be mailed.  The title is the old file's name with _ as spaces:
    GlobalAdv_Hong_Kong.csv is "GlobalAdv Hong Kong: old vs new".

THE INPUT: a CSV with a header row; delimiter , ; tab or |.  Blank or
non-numeric cells are left out of that column's chart and table.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from html import escape
from pathlib import Path

DEFAULT_THRESHOLD = 100.0
KEY = "#FidessaCode"
FLOOR = 1e-12        # where a value of 0 or below is drawn on the log axes


# =============================================================================
# READING
# =============================================================================

def _number(text):
    try:
        v = float(text.strip())
    except (ValueError, AttributeError):
        return None
    return v if math.isfinite(v) else None


def read_adv(path) -> tuple[str, list, dict]:
    """(key column, value columns in file order, {code: {column: float}})."""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    if not rows:
        raise ValueError(f"{path} is empty")
    header = [h.strip() for h in rows[0]]
    k = header.index(KEY) if KEY in header else 0
    columns = [h for i, h in enumerate(header) if i != k]
    data = {}
    for row in rows[1:]:
        if len(row) <= k or not row[k].strip():
            continue
        data[row[k].strip()] = {h: _number(row[i]) for i, h in
                                enumerate(header) if i != k and i < len(row)}
    #  a column with no number anywhere (a name, a venue) is not compared
    columns = [c for c in columns
               if any(r.get(c) is not None for r in data.values())]
    return header[k], columns, data


# =============================================================================
# COMPARING
# =============================================================================

def change_pct(old, new):
    """(new - old) / old in percent; None when old is 0 and new is not."""
    if old == 0:
        return 0.0 if new == 0 else None
    return 100 * (new - old) / abs(old)


def big_changes(pairs, threshold) -> list:
    """(code, old, new, change or None) above the threshold, largest first,
    the old-is-zero ones before everything."""
    out = []
    for code, old, new in pairs:
        ch = change_pct(old, new)
        if ch is None or abs(ch) > threshold:
            out.append((code, old, new, ch))
    out.sort(key=lambda r: (r[3] is not None,
                            -abs(r[3]) if r[3] is not None else 0, r[0]))
    return out


# =============================================================================
# DRAWING
# =============================================================================

W = H = 560
LEFT, TOP, RIGHT, BOTTOM = 70, 20, 16, 56
SIDE = W - LEFT - RIGHT            # the plot is square: the same scale both ways


def _pow10(e) -> str:
    sign = "&#8722;" if e < 0 else ""
    return f'10<tspan dy="-6" font-size="9">{sign}{abs(e)}</tspan>'


def _fmt(v) -> str:
    return f"{v:,.4f}"


def scatter_svg(pairs, old_label, new_label) -> str:
    """Old on x, new on y, both log, the same decades on both axes."""
    vals = [max(v, FLOOR) for _, o, n in pairs for v in (o, n)] or [1.0]
    lo = math.floor(math.log10(min(vals)))
    hi = max(math.ceil(math.log10(max(vals))), lo + 1)
    step = math.ceil((hi - lo) / 9)

    def P(v):
        return SIDE * (math.log10(max(v, FLOOR)) - lo) / (hi - lo)

    parts = []
    for e in range(lo, hi + 1, step):
        x, y = LEFT + P(10 ** e), TOP + SIDE - P(10 ** e)
        parts.append(
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{TOP}" y2="{TOP + SIDE}" '
            f'class="grid"/><line x1="{LEFT}" x2="{LEFT + SIDE}" '
            f'y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x:.1f}" y="{TOP + SIDE + 18}" text-anchor="middle">'
            f'{_pow10(e)}</text>'
            f'<text x="{LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f'{_pow10(e)}</text>')
    parts.append(f'<line x1="{LEFT}" y1="{TOP + SIDE}" x2="{LEFT + SIDE}" '
                 f'y2="{TOP}" class="diag"/>')
    for code, o, n in pairs:
        parts.append(
            f'<circle cx="{LEFT + P(o):.1f}" cy="{TOP + SIDE - P(n):.1f}" '
            f'r="2"><title>{escape(code)}&#10;old {_fmt(o)}&#10;new '
            f'{_fmt(n)}</title></circle>')
    parts.append(
        f'<rect x="{LEFT}" y="{TOP}" width="{SIDE}" height="{SIDE}" '
        f'class="frame"/>'
        f'<g transform="translate({LEFT + 10},{TOP + 10})">'
        f'<rect width="92" height="22" rx="3" class="key"/>'
        f'<line x1="8" x2="28" y1="11" y2="11" class="diag"/>'
        f'<text x="34" y="15" class="ink">No change</text></g>'
        f'<text x="{LEFT + SIDE / 2}" y="{TOP + SIDE + 42}" '
        f'text-anchor="middle" class="ink">{escape(old_label)}</text>'
        f'<text transform="translate(16,{TOP + SIDE / 2}) rotate(-90)" '
        f'text-anchor="middle" class="ink">{escape(new_label)}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="'
            f'{escape(new_label)} against {escape(old_label)}">'
            + "".join(parts) + "</svg>")


def table_html(rows, key, old_name, new_name) -> str:
    if not rows:
        return '<p class="none">None.</p>'
    body = "".join(
        f"<tr><td>{escape(code)}</td><td>{_fmt(o)}</td><td>{_fmt(n)}</td>"
        f"<td>{'N/A (old = 0)' if ch is None else f'{ch:+,.2f}%'}</td>"
        f"<td>{'Increase' if n > o else 'Decrease'}</td></tr>"
        for code, o, n, ch in rows)
    return (f"<table><thead><tr><th>{escape(key)}</th>"
            f"<th>Old ({escape(old_name)})</th><th>New ({escape(new_name)})</th>"
            f"<th>Change</th><th>Direction</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}: old vs new</title>
<style>
:root {{ --page:#f4f4f2; --surface:#fff; --ink:#1b1b1b; --ink2:#52514e;
  --muted:#6b6a66; --grid:#e6e5df; --axis:#8c8b85; --dot:#1f6f78;
  --diag:#c0392b; --ring:rgba(11,11,11,.12); --head:#eeeeea; }}
@media (prefers-color-scheme: dark) {{ :root {{ --page:#0d0d0d;
  --surface:#1a1a19; --ink:#f2f2f0; --ink2:#c3c2b7; --muted:#9a9993;
  --grid:#2c2c2a; --axis:#6b6a66; --dot:#4fb3bf; --diag:#e5675a;
  --ring:rgba(255,255,255,.12); --head:#242422; }} }}
body {{ margin:0; padding:24px 16px; background:var(--page); color:var(--ink);
  font:14px/1.45 system-ui, sans-serif; }}
main {{ max-width:1500px; margin:0 auto; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
.sub {{ color:var(--ink2); margin:0 0 20px; }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 620px), 1fr));
  gap:20px; }}
.card {{ background:var(--surface); border:1px solid var(--ring);
  border-radius:6px; padding:16px; min-width:0; }}
h2 {{ font-size:17px; margin:0 0 8px; }}
h3 {{ font-size:14px; margin:16px 0 6px; }}
.ctitle {{ text-align:center; color:var(--ink2); margin:0; }}
svg {{ width:100%; max-width:{w}px; height:auto; display:block; margin:0 auto; }}
svg text {{ fill:var(--muted); font-size:11px; }}
svg text.ink {{ fill:var(--ink2); font-size:12px; }}
svg .grid {{ stroke:var(--grid); }}
svg .frame {{ fill:none; stroke:var(--axis); }}
svg .diag {{ stroke:var(--diag); stroke-width:1.2; stroke-dasharray:5 4; }}
svg .key {{ fill:var(--surface); stroke:var(--ring); }}
svg circle {{ fill:var(--dot); fill-opacity:.6; }}
svg circle:hover {{ fill-opacity:1; r:4; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums;
  font-size:13px; }}
th, td {{ text-align:right; padding:5px 8px; border-bottom:1px solid var(--grid);
  white-space:nowrap; }}
th:first-child, td:first-child {{ text-align:left; }}
th {{ background:var(--head); color:var(--ink2); font-weight:600; }}
.none {{ color:var(--muted); margin:0; }}
</style></head><body><main>
<h1>{title}: old vs new</h1>
<p class="sub">{sub}</p>
<div class="grid2">{cards}</div>
</main></body></html>
"""


def write_page(path, title, old_name, new_name, old, new, threshold) -> dict:
    """Write the page; return {column: number above the threshold}."""
    key, old_cols, old_rows = old
    _, new_cols, new_rows = new
    shared = [c for c in old_rows if c in new_rows]
    columns = [c for c in old_cols if c in new_cols]
    cards, counts = [], {}
    for col in columns:
        pairs = [(c, old_rows[c].get(col), new_rows[c].get(col))
                 for c in shared]
        pairs = [p for p in pairs if p[1] is not None and p[2] is not None]
        big = big_changes(pairs, threshold)
        counts[col] = len(big)
        cards.append(
            f'<section class="card"><h2>{escape(col)}</h2>'
            f'<p class="ctitle">{escape(title)}: {escape(col)}<br>'
            f'{len(pairs):,} shared instruments</p>'
            + scatter_svg(pairs, f"Old: {old_name} {col}",
                          f"New: {new_name} {col}")
            + f"<h3>Differences greater than {threshold:g}% ({len(big)})</h3>"
            f'<div class="wrap">{table_html(big, key, old_name, new_name)}'
            f"</div></section>")
    sub = (f"Old: {escape(old_name)} | New: {escape(new_name)} | "
           f"Shared instruments: {len(shared):,} | "
           f"{escape(old_name)}-only: {len(old_rows) - len(shared):,} | "
           f"{escape(new_name)}-only: {len(new_rows) - len(shared):,}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(_PAGE.format(
        title=escape(title), sub=sub, w=W, cards="".join(cards)),
        encoding="utf-8")
    return counts


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old", nargs="?")
    ap.add_argument("new", nargs="?")
    ap.add_argument("--old-name", default="Bcore")
    ap.add_argument("--new-name", default="Nova")
    ap.add_argument("-t", "--threshold", type=float, default=DEFAULT_THRESHOLD,
                    metavar="PCT", help="list changes above this, in percent")
    ap.add_argument("--out", metavar="DIR",
                    help="where the page goes (default: beside the old file)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not (args.old and args.new):
        ap.error("OLD and NEW are required")
    try:
        old, new = read_adv(args.old), read_adv(args.new)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        print(f"cannot read: {e}", file=sys.stderr)
        return 2
    stem = Path(args.old).stem
    out = Path(args.out or Path(args.old).parent) / f"{stem}_comparison.html"
    counts = write_page(out, stem.replace("_", " "), args.old_name,
                        args.new_name, old, new, args.threshold)
    shared = sum(1 for c in old[2] if c in new[2])
    print(f"old  {args.old}  ({len(old[2]):,} instruments)")
    print(f"new  {args.new}  ({len(new[2]):,} instruments)")
    print(f"\n {shared:,} in both   {len(old[2]) - shared:,} only in old   "
          f"{len(new[2]) - shared:,} only in new\n")
    for col, n in counts.items():
        print(f"  {col:<20} {n:5,d} above {args.threshold:g}%")
    missing = [c for c in old[1] if c not in new[1]] + \
              [c for c in new[1] if c not in old[1]]
    if missing:
        print(f"\n  not in both files, not compared: {', '.join(missing)}")
    print(f"\n wrote {out}")
    return 0


def self_test() -> int:
    import tempfile
    failed = 0

    def check(name, got, want):
        nonlocal failed
        ok = got == want
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name}"
              + ("" if ok else f"\n       got  {got!r}\n       want {want!r}"))

    old_csv = ("#FidessaCode,Venue,MeanAov,MeanAiv\n"
               "3423.HK,HKG,0,10\n2041.HK,HKG,3447,851714\n"
               "1.HK,HKG,100,100\n9.HK,HKG,5,5\n")
    new_csv = ("#FidessaCode;Venue;MeanAov;MeanAiv;Extra\n"
               "3423.HK;HKG;1;10;1\n2041.HK;HKG;134945;1708922;1\n"
               "1.HK;HKG;40;;1\n7.HK;HKG;1;1;1\n8.HK;HKG;1;1;1\n")
    with tempfile.TemporaryDirectory() as d:
        o, n = Path(d) / "GlobalAdv_Hong_Kong.csv", Path(d) / "new.csv"
        o.write_text(old_csv, encoding="utf-8")
        n.write_text(new_csv, encoding="utf-8")
        key, cols, rows = read_adv(o)
        check("the key is #FidessaCode, Venue is not a number column",
              (key, cols), ("#FidessaCode", ["MeanAov", "MeanAiv"]))
        check("a ; file reads too, a blank cell is None",
              read_adv(n)[2]["1.HK"]["MeanAiv"], None)
        pairs = [(c, rows[c]["MeanAov"], read_adv(n)[2][c]["MeanAov"])
                 for c in ("1.HK", "2041.HK", "3423.HK")]
        big = big_changes(pairs, 100)
        check("old = 0 first, then the largest; -60% is not listed",
              [(c, None if ch is None else round(ch, 2)) for c, _, _, ch in big],
              [("3423.HK", None), ("2041.HK", 3814.85)])
        check("the MeanAiv change of 2041.HK is +100.65%",
              round(change_pct(851714, 1708922), 2), 100.65)
        rc = main([str(o), str(n), "--out", d])
        page = (Path(d) / "GlobalAdv_Hong_Kong_comparison.html").read_text(
            encoding="utf-8")
        check("the run succeeds", rc, 0)
        check("the title comes from the old file's name",
              "GlobalAdv Hong Kong: old vs new" in page, True)
        check("the counts line",
              "Shared instruments: 3 | Bcore-only: 1 | Nova-only: 2" in page,
              True)
        check("one section per shared number column",
              (page.count('<section class="card">'), "Extra</h2>" in page),
              (2, False))
        check("the MeanAiv chart leaves out the blank cell",
              "MeanAiv<br>2 shared instruments" in page, True)
        check("the table reads as in the reference page",
              "<td>+3,814.85%</td><td>Increase</td>" in page
              and "N/A (old = 0)" in page, True)
    print("self-test " + ("passed" if not failed else f"FAILED ({failed})"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
