#!/usr/bin/env python3
"""The sanity-check reports: one CSV and one dark-mode HTML page per report.

Two reports use this - the run summary written after every generation, and
the comparison against the legacy file written by --compare.  Both are a
list of SECTIONS, each a titled table, and this module only renders them:
what goes in each table is limit_up_down.py's business.

THE CSV IS EVERY TABLE IN ONE FILE, each block opened by its title on a row
of its own and closed by a blank row, so it opens in Excel as one sheet
that reads top to bottom.  The HTML is the same tables, with headline
figures above them, a filter box on each table and no external resources -
it is a mail attachment and has to open on a machine with no internet.

    python sanity.py --self-test
"""

from __future__ import annotations

import csv
import html
import re
from pathlib import Path


def section(title, header, rows, note=""):
    """One titled table.  `rows` are lists in the order of `header`."""
    return {"title": title, "header": list(header),
            "rows": [list(r) for r in rows], "note": note}


def write_csv(path, sections):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        for i, s in enumerate(sections):
            if i:
                w.writerow([])
            w.writerow([f"## {s['title']}"])
            w.writerow(s["header"])
            w.writerows(s["rows"])
    return path


# =============================================================================
# HTML
# =============================================================================

#  A number, a signed number, a percentage, or a count with its percentage
#  - "3200 (20%)".  Columns made only of these are right-aligned.
_NUMERIC = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?%?( \(\d+(\.\d+)?%\))?$")

#  Cell values that are shown as a coloured tag rather than as text - only
#  in these columns, so a venue's configuration is not dressed up as a
#  result.
PILL_COLUMNS = {"Source", "Value"}
_PILLS = {"computed": "computed", "bloomberg": "bloomberg",
          "OK": "ok", "MISMATCH": "bad", "FAILED": "bad", "differs": "warn"}

_CSS = """
:root {
  --bg: #0d1117; --panel: #161b22; --panel-2: #1c2230; --border: #2b3240;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
  --ok: #3fb950; --warn: #d29922; --bad: #f85149;
  --computed: #a371f7; --bloomberg: #39c5cf;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width: 1240px; margin: 0 auto; padding: 32px 16px 64px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px;
  margin-bottom: 8px; }
h1 { font-size: 24px; font-weight: 600; margin: 0; letter-spacing: -.01em; }
.sub { color: var(--muted); margin: 0 0 28px; }
.status { padding: 4px 12px; border-radius: 999px; font-weight: 600;
  font-size: 13px; border: 1px solid currentColor; }
.status.ok { color: var(--ok); } .status.warn { color: var(--warn); }
.status.bad { color: var(--bad); }
.kpis { display: grid; gap: 12px; margin-bottom: 20px;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
.kpi { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; }
.kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .06em; }
.kpi .value { font-size: 26px; font-weight: 600; margin-top: 2px;
  font-variant-numeric: tabular-nums; }
.kpi .note { color: var(--muted); font-size: 12px; }
.kpi.ok .value { color: var(--ok); } .kpi.warn .value { color: var(--warn); }
.kpi.bad .value { color: var(--bad); }
.split { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 28px; }
.bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden;
  background: var(--panel-2); margin: 10px 0 8px; }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 20px; color: var(--muted);
  font-size: 13px; }
.legend i { display: inline-block; width: 10px; height: 10px;
  border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
details { background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; margin-bottom: 16px; overflow: hidden; }
summary { cursor: pointer; list-style: none; display: flex; flex-wrap: wrap;
  align-items: center; gap: 10px; padding: 14px 16px; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "\\25B8"; color: var(--muted);
  transition: transform .15s; }
details[open] summary::before { transform: rotate(90deg); }
summary h2 { font-size: 16px; font-weight: 600; margin: 0; }
.count { background: var(--panel-2); color: var(--muted); border-radius: 999px;
  padding: 1px 10px; font-size: 12px; font-variant-numeric: tabular-nums; }
.note-line { color: var(--muted); padding: 0 16px 10px; margin: 0; }
.tools { padding: 0 16px 10px; }
.tools input { width: 100%; max-width: 360px; background: var(--bg);
  color: var(--text); border: 1px solid var(--border); border-radius: 6px;
  padding: 7px 10px; font: inherit; }
.tools input:focus { outline: none; border-color: var(--accent); }
.scroll { max-height: 560px; overflow: auto; border-top: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 7px 14px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--border); }
th { position: sticky; top: 0; background: var(--panel-2); color: var(--muted);
  font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: .05em; z-index: 1; }
td.wrap { white-space: normal; min-width: 260px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:hover td { background: var(--panel-2); }
td.empty { color: var(--muted); font-style: italic; }
.pill { display: inline-block; padding: 0 9px; border-radius: 999px;
  font-size: 12px; font-weight: 600; border: 1px solid currentColor; }
.pill.computed { color: var(--computed); } .pill.bloomberg { color: var(--bloomberg); }
.pill.ok { color: var(--ok); } .pill.warn { color: var(--warn); }
.pill.bad { color: var(--bad); }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; }
"""

_JS = """
document.querySelectorAll('input[data-for]').forEach(function (box) {
  var body = document.getElementById(box.dataset.for);
  var shown = document.getElementById(box.dataset.for + '-n');
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase(), n = 0;
    Array.prototype.forEach.call(body.rows, function (tr) {
      var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
      tr.style.display = hit ? '' : 'none';
      if (hit) n++;
    });
    shown.textContent = q ? n + ' of ' + body.rows.length : body.rows.length;
  });
});
"""

#  Tables longer than this get a filter box.
FILTER_FROM = 15


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _numeric_columns(rows, width):
    out = []
    for c in range(width):
        cells = [str(r[c]).strip() for r in rows
                 if c < len(r) and str(r[c]).strip()]
        out.append(bool(cells) and all(_NUMERIC.match(x) for x in cells))
    return out


def _cell(v, num, pill=True):
    text = "" if v is None else str(v)
    if pill and text in _PILLS:
        return f'<td><span class="pill {_PILLS[text]}">{_esc(text)}</span></td>'
    cls = "num" if num else ("wrap" if len(text) > 60 else "")
    return f'<td class="{cls}">{_esc(text)}</td>' if cls else \
        f"<td>{_esc(text)}</td>"


def _table(i, s):
    header, rows = s["header"], s["rows"]
    num = _numeric_columns(rows, len(header))
    tid = f"t{i}"
    out = [f'<details open><summary><h2>{_esc(s["title"])}</h2>'
           f'<span class="count" id="{tid}-n">{len(rows)}</span></summary>']
    if s.get("note"):
        out.append(f'<p class="note-line">{_esc(s["note"])}</p>')
    if len(rows) > FILTER_FROM:
        out.append(f'<div class="tools"><input type="search" data-for="{tid}" '
                   f'placeholder="Filter {len(rows)} rows..."></div>')
    out.append('<div class="scroll"><table><thead><tr>')
    out.extend(f'<th class="{"num" if n else ""}">{_esc(h)}</th>'
               for h, n in zip(header, num))
    out.append(f'</tr></thead><tbody id="{tid}">')
    if not rows:
        out.append(f'<tr><td class="empty" colspan="{len(header)}">None</td>'
                   f'</tr>')
    for r in rows:
        out.append("<tr>" + "".join(_cell(v, n, h in PILL_COLUMNS)
                                    for v, n, h in zip(r, num, header))
                   + "</tr>")
    out.append("</tbody></table></div></details>")
    return "\n".join(out)


def render_html(title, subtitle, status, kpis, sections, split=None,
                footer=""):
    """`status` is (tone, text) with tone ok | warn | bad.  `kpis` are
    (label, value, note, tone) - tone may be "".  `split` is an optional
    [(label, count, css colour var)] drawn as one stacked bar."""
    tone, text = status
    parts = [
        "<!DOCTYPE html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="dark">',
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body><main>",
        f'<header><h1>{_esc(title)}</h1>'
        f'<span class="status {tone}">{_esc(text)}</span></header>',
        f'<p class="sub">{_esc(subtitle)}</p>', '<section class="kpis">']
    for label, value, note, ktone in kpis:
        parts.append(f'<div class="kpi {ktone}"><div class="label">'
                     f'{_esc(label)}</div><div class="value">{_esc(value)}'
                     f'</div><div class="note">{_esc(note)}</div></div>')
    parts.append("</section>")
    total = sum(n for _, n, _ in split or [])
    if total:
        parts.append('<section class="split"><div class="label">'
                     'How the published rows were priced</div><div class="bar">')
        parts.extend(f'<span style="width:{100 * n / total:.3f}%;'
                     f'background:var({var})"></span>'
                     for _, n, var in split)
        parts.append('</div><div class="legend">')
        parts.extend(f'<span><i style="background:var({var})"></i>'
                     f'{_esc(label)} {n} ({100 * n / total:.0f}%)</span>'
                     for label, n, var in split)
        parts.append("</div></section>")
    parts.extend(_table(i, s) for i, s in enumerate(sections))
    if footer:
        parts.append(f"<footer>{_esc(footer)}</footer>")
    parts.append(f"</main><script>{_JS}</script></body></html>")
    return "\n".join(parts)


def write_html(path, *args, **kwargs):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(*args, **kwargs), encoding="utf-8")
    return path


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    sections = [section("Summary", ["Metric", "Value"],
                        [["CrossCode lines", 10], ["Published", "8"]]),
                section("Not published", ["ReutersCode", "Reason"],
                        [["A.T", "no close, <then> refused"]]),
                section("Empty", ["X"], [])]

    print("sanity --self-test\n\nthe csv")
    with tempfile.TemporaryDirectory() as d:
        text = write_csv(Path(d) / "r.csv", sections).read_text(
            encoding="utf-8")
    check("every table in one file, titled and separated by a blank row",
          text.splitlines(),
          ["## Summary", "Metric,Value", "CrossCode lines,10", "Published,8",
           "", "## Not published", "ReutersCode,Reason",
           "A.T,\"no close, <then> refused\"", "", "## Empty", "X"])

    print("\nthe html")
    page = render_html("LimitUpDown run", "sub", ("ok", "OK"),
                       [("Published", "8", "rows", "ok")], sections,
                       split=[("Computed", 2, "--computed"),
                              ("Bloomberg", 6, "--bloomberg")])
    check("dark by default", "--bg: #0d1117" in page
          and 'content="dark"' in page, True)
    check("cell text is escaped", "&lt;then&gt;" in page
          and "<then>" not in page, True)
    check("a count column is right-aligned",
          '<td class="num">10</td>' in page, True)
    check("the split bar carries both shares",
          ("Computed 2 (25%)" in page, "Bloomberg 6 (75%)" in page),
          (True, True))
    check("an empty table says None rather than showing nothing",
          '>None</td>' in page, True)
    check("no external resource - it opens offline",
          re.search(r'(src|href)="https?:', page), None)
    long = render_html("t", "s", ("ok", "OK"), [],
                       [section("Long", ["A"], [[i] for i in range(20)])])
    check("a long table gets a filter box and a short one does not",
          ('type="search"' in long, 'type="search"' in page), (True, False))
    check("computed and bloomberg are shown as tags",
          '<span class="pill computed">computed</span>' in render_html(
              "t", "s", ("ok", "OK"), [],
              [section("S", ["Source"], [["computed"]])]), True)
    check("but not in a column that is not a result",
          "pill" in render_html("t", "s", ("ok", "OK"), [],
                                [section("S", ["Configured"],
                                         [["bloomberg"]])]).split("<main>")[1],
          False)
    check("a count with its share is still numeric",
          _numeric_columns([["3200 (20%)"], ["12"]], 1), [True])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
