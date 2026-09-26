#!/usr/bin/env python3
"""Two volume profiles of one market, compared stock by stock.

Two processes produce the same volume profile CSV - the one the Volume
Profile Viewer reads - and this says, for every stock, whether the NEW one
is aligned with the OLD one or off, and shows the two side by side.

    python compare_profiles.py OLD.csv NEW.csv
    python compare_profiles.py OLD.csv NEW.csv --threshold 1.5 --out reports
    python compare_profiles.py --self-test

ONE NUMBER PER STOCK: THE LARGEST GAP IN THE CUMULATED CURVE.  At every
bucket time either file has, gap = new - old, in percentage points of the
day.  The largest absolute gap, and when it happened, is the stock's score:
at most --threshold (2.00 pp by default) is ALIGNED, above it is OFF.  A
different closing auction, a curve that runs early or late, a profile that
does not end at 100% - each moves the cumulated curve, so one number catches
them all and says when.

THE BUCKET GRIDS MAY DIFFER.  Each curve holds its last value until its next
bucket, and the two are compared at every time either file has.  That is
what a cumulated curve means - nothing more had traded by then - so a 1-minute
profile can be measured against a 5-minute one without inventing a point.

WHAT IT WRITES
    the terminal   counts, and the worst stocks first
    comparison.csv one row per stock, worst first, plus ONLY_OLD / ONLY_NEW
    comparison.html
                   one self-contained page: the stock list, and for the one
                   picked the OLD and NEW profiles SIDE BY SIDE on the same
                   scales, with the gap across the day underneath.  No
                   network, nothing to install - it can be mailed.

EXIT CODE: 0 every stock aligned and in both files, 1 anything off or in
only one file, 2 the input could not be read.  A scheduled job can act on it.

THE INPUT is the viewer's format, read with the viewer's tolerance, because
these files get re-saved through Excel:

    #TimeZone=India Standard Time,format=V2,isweekly=false
    #FidessaCode,ReutersCode,Venue,TimeZone,Time,CumulatedPercentage
    ICICIBC.IN,ICBK.NS,NSI-MAIN,India Standard Time,9:15:00,0.0215

    delimiter , ; tab or |      decimal commas      0..1 or 0..100
    9:15:00, 09:15 or an Excel time serial          '#' on the header optional
    ReutersCode, Venue and TimeZone optional        bad rows counted, skipped

A stock is its FidessaCode AND its Venue, as in the viewer.  Two files whose
time zones differ for the same stock are refused: their clocks do not line
up, and every gap would be measured between different moments.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_THRESHOLD = 2.0
DAY = 86_400


# =============================================================================
# READING
# =============================================================================

@dataclass
class Series:
    code: str
    venue: str
    ric: str = ""
    zone: str = ""
    #  seconds of day -> cumulated share, 0..1, in FILE order.  A repeated
    #  time keeps its first place and its last value.
    points: dict = field(default_factory=dict)


@dataclass
class Profile:
    path: str
    stocks: dict            # (code, venue) -> Series
    skipped: int = 0
    zone: str = ""


class InputError(ValueError):
    pass


def parse_time(text):
    """'9:15:00', '09:15', '9:15:00.000' or an Excel time serial -> seconds
    of day, or None."""
    s = (text or "").strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) not in (2, 3):
            return None
        try:
            h, m = int(parts[0]), int(parts[1])
            sec = float(parts[2].replace(",", ".")) if len(parts) == 3 else 0
        except ValueError:
            return None
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
            return None
        return h * 3600 + m * 60 + int(sec)
    try:
        x = float(s.replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(x) or x < 0:
        return None
    #  An Excel serial: the fraction is the time of day, whatever the date.
    return int(round((x - math.floor(x)) * DAY)) % DAY


def parse_number(text, delimiter):
    s = (text or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    #  A decimal comma: always when the file is not comma separated, and
    #  when a quoted field holds one with no dot beside it.
    if delimiter != "," or ("," in s and "." not in s):
        s = s.replace(",", ".")
    try:
        x = float(s)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def clock(secs) -> str:
    secs = int(secs) % DAY
    return f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}"


def read_profile(path) -> Profile:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()

    meta, header_at = {}, None
    for i, line in enumerate(lines):
        if "cumulatedpercentage" in line.lower():
            header_at = i
            break
        body = line.lstrip("#")
        for part in body.replace(";", ",").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                meta[k.strip().lower()] = v.strip()
    if header_at is None:
        raise InputError(f"{path}: no header row with CumulatedPercentage - "
                         f"is this a volume profile?")

    header_line = lines[header_at].lstrip("#")
    delimiter = max(",;\t|", key=header_line.count)
    header = [h.strip().lstrip("#").lower() for h in
              next(csv.reader([header_line], delimiter=delimiter))]
    col = {name: header.index(name) for name in header}
    for need in ("fidessacode", "time", "cumulatedpercentage"):
        if need not in col:
            raise InputError(f"{path}: no {need} column in the header "
                             f"{header_line!r}")

    def cell(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    profile = Profile(path=str(path), stocks={}, zone=meta.get("timezone", ""))
    rows = []
    for row in csv.reader(lines[header_at + 1:], delimiter=delimiter):
        if not row or not any(c.strip() for c in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        code = cell(row, "fidessacode")
        t = parse_time(cell(row, "time"))
        v = parse_number(cell(row, "cumulatedpercentage"), delimiter)
        if not code or t is None or v is None:
            profile.skipped += 1
            continue
        rows.append((code, cell(row, "venue"), cell(row, "reuterscode"),
                     cell(row, "timezone") or profile.zone, t, v))

    #  0..1 or 0..100, decided for the whole file: a single stock's curve
    #  could sit below 1.5 in percent and would be misread on its own.
    scale = 100.0 if rows and max(r[5] for r in rows) > 1.5 else 1.0
    for code, venue, ric, zone, t, v in rows:
        s = profile.stocks.get((code, venue))
        if s is None:
            s = profile.stocks[(code, venue)] = Series(code, venue, ric, zone)
        s.ric = s.ric or ric
        s.points[t] = v / scale
    if not profile.stocks:
        raise InputError(f"{path}: no readable rows "
                         f"({profile.skipped} skipped)")
    return profile


# =============================================================================
# COMPARING
# =============================================================================

def session_origin(old, new):
    """Where the session starts: the EARLIER of the two first buckets.

    Taking OLD's alone broke on a file missing its opening bucket - NEW's
    9:15 measured from OLD's 9:20 wraps to the end of the day, and a stock
    10 pp early reads as 90 pp late.  "Earlier" is within half a day, so a
    session across midnight still picks its evening start."""
    firsts = [next(iter(s.points)) for s in (old, new) if s is not None]
    o = firsts[0]
    for n in firsts[1:]:
        if 0 < (o - n) % DAY < DAY / 2:
            o = n
    return o


def session(series_list, origin):
    """Each series as [(seconds since origin, cum)], in session order.

    Measured from session_origin() and wrapped at midnight, so a
    session that crosses 00:00 stays in trading order instead of sorting
    its evening before its morning."""
    return [sorted(((t - origin) % DAY, v) for t, v in s.points.items())
            for s in series_list]


def step_values(points, grid):
    """The curve at each grid time: its last value at or before it, 0 before
    its first bucket."""
    out, i, last = [], 0, 0.0
    for g in grid:
        while i < len(points) and points[i][0] <= g:
            last = points[i][1]
            i += 1
        out.append(last)
    return out


@dataclass
class Result:
    code: str
    venue: str
    ric: str
    verdict: str            # ALIGNED, OFF, ONLY_OLD, ONLY_NEW
    gap: float = 0.0        # signed, new - old, percentage points
    at: str = ""
    old_pct: float = 0.0    # the two curves at `at`, in percent
    new_pct: float = 0.0


def compare(old: Series, new: Series, threshold):
    origin = session_origin(old, new)
    o, n = session([old, new], origin)
    grid = sorted({t for t, _ in o} | {t for t, _ in n})
    ov, nv = step_values(o, grid), step_values(n, grid)
    #  A tie - within float noise - goes to the EARLIEST time, so the same
    #  two files always report the same moment.
    top = max(abs(b - a) for a, b in zip(ov, nv))
    best = next(i for i in range(len(grid)) if abs(nv[i] - ov[i]) >= top - 1e-9)
    gap = (nv[best] - ov[best]) * 100
    return Result(old.code, old.venue, old.ric or new.ric,
                  "ALIGNED" if abs(gap) <= threshold + 1e-9 else "OFF",
                  #  Identical curves have no moment worth naming.
                  gap, clock(origin + grid[best]) if top > 1e-9 else "",
                  ov[best] * 100, nv[best] * 100)


def compare_files(old: Profile, new: Profile, threshold):
    clash = [f"{k[0]} {k[1]}: {old.stocks[k].zone!r} vs {new.stocks[k].zone!r}"
             for k in old.stocks.keys() & new.stocks.keys()
             if old.stocks[k].zone and new.stocks[k].zone
             and old.stocks[k].zone.casefold() != new.stocks[k].zone.casefold()]
    if clash:
        raise InputError(
            f"{len(clash)} stock(s) are in different time zones in the two "
            f"files, so their times do not line up:\n  "
            + "\n  ".join(sorted(clash)[:10]))
    results = []
    for k, s in old.stocks.items():
        if k in new.stocks:
            results.append(compare(s, new.stocks[k], threshold))
        else:
            results.append(Result(s.code, s.venue, s.ric, "ONLY_OLD"))
    for k, s in new.stocks.items():
        if k not in old.stocks:
            results.append(Result(s.code, s.venue, s.ric, "ONLY_NEW"))
    order = {"OFF": 0, "ONLY_OLD": 1, "ONLY_NEW": 1, "ALIGNED": 2}
    results.sort(key=lambda r: (order[r.verdict], -abs(r.gap), r.code,
                                r.venue))
    return results


# =============================================================================
# WRITING
# =============================================================================

CSV_COLUMNS = ["FidessaCode", "Venue", "ReutersCode", "Verdict", "MaxGapPP",
               "At", "OldPct", "NewPct"]


def write_csv(path, results):
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in results:
            both = r.verdict in ("ALIGNED", "OFF")
            w.writerow([r.code, r.venue, r.ric, r.verdict,
                        f"{r.gap:.2f}" if both else "", r.at,
                        f"{r.old_pct:.2f}" if both else "",
                        f"{r.new_pct:.2f}" if both else ""])


def _curve(series, origin):
    if series is None:
        return None
    pts = sorted(((t - origin) % DAY, v) for t, v in series.points.items())
    return [[t for t, _ in pts], [round(v, 6) for _, v in pts]]


def page_data(old: Profile, new: Profile, results, threshold):
    """Everything the page draws.  Each side keeps its OWN buckets, so the
    bars are that file's buckets and not an artefact of the merged grid; a
    side identical in its times to the other ships its times once."""
    stocks = []
    for r in results:
        k = (r.code, r.venue)
        o, n = old.stocks.get(k), new.stocks.get(k)
        origin = session_origin(o, n)
        oc, nc = _curve(o, origin), _curve(n, origin)
        if oc and nc and oc[0] == nc[0]:
            nc = [None, nc[1]]
        stocks.append({"c": r.code, "v": r.venue, "r": r.ric,
                       "verdict": r.verdict, "g": round(r.gap, 4),
                       "at": r.at, "origin": origin, "o": oc, "n": nc})
    return {"threshold": threshold,
            "old": Path(old.path).name, "new": Path(new.path).name,
            "zone": old.zone or new.zone
                    or next((s.zone for s in old.stocks.values() if s.zone),
                            ""),
            "stocks": stocks}


def write_html(path, data):
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    Path(path).write_text(PAGE.replace("/*__DATA__*/null", blob),
                          encoding="utf-8")


def summary(results, threshold, old: Profile, new: Profile) -> str:
    count = {v: sum(r.verdict == v for r in results)
             for v in ("ALIGNED", "OFF", "ONLY_OLD", "ONLY_NEW")}
    both = count["ALIGNED"] + count["OFF"]
    out = [f"old  {old.path}  ({len(old.stocks):,} stocks"
           + (f", {old.skipped} rows skipped" if old.skipped else "") + ")",
           f"new  {new.path}  ({len(new.stocks):,} stocks"
           + (f", {new.skipped} rows skipped" if new.skipped else "") + ")",
           "",
           f" {both:,} stocks in both files",
           f" {count['ALIGNED']:,} aligned   {count['OFF']:,} off "
           f"(max gap > {threshold:.2f} pp)"]
    if count["ONLY_OLD"] or count["ONLY_NEW"]:
        out.append(f"   {count['ONLY_OLD']:,} only in old   "
                   f"{count['ONLY_NEW']:,} only in new")
    worst = [r for r in results if r.verdict == "OFF"][:10]
    if worst:
        out.append("")
        for i, r in enumerate(worst):
            out.append(f" {'worst:' if i == 0 else '':6}  {r.code:<14} "
                       f"{r.venue:<10} {r.gap:+6.2f} pp at {r.at}   "
                       f"(old {r.old_pct:.2f}%, new {r.new_pct:.2f}%)")
    return "\n".join(out)


def run(old_path, new_path, threshold, out_dir) -> int:
    try:
        old, new = read_profile(old_path), read_profile(new_path)
        results = compare_files(old, new, threshold)
    except (InputError, OSError) as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "comparison.csv", results)
    write_html(out / "comparison.html", page_data(old, new, results,
                                                  threshold))
    print(summary(results, threshold, old, new))
    print(f"\n wrote {out / 'comparison.csv'}, {out / 'comparison.html'}")
    return 0 if all(r.verdict == "ALIGNED" for r in results) else 1


# =============================================================================
# THE PAGE.  Plain HTML, CSS and SVG drawn by a small script: no library, no
# network.  __DATA__ is replaced by the comparison, as JSON.
# =============================================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Volume Profile Comparison</title>
<style>
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e2e2e0;
--old:#3b6fb6;--new:#d9772b;--off:#c0392b;--ok:#2e8b57;--only:#8a6d3b;--hl:#fff4d6}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--panel:#1f1f22;--ink:#ececef;
--mute:#9a9aa2;--line:#34343a;--old:#6f9de0;--new:#f0a060;--off:#ff6b5b;--ok:#4cc38a;
--only:#d2b06a;--hl:#3a3320}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{font-size:18px;margin:0 0 4px}
.files{color:var(--mute);font-size:13px}
.files b{color:var(--ink);font-weight:600}
.counts{margin-top:8px;display:flex;gap:16px;flex-wrap:wrap}
.pill{font-weight:600}
.OFF{color:var(--off)}.ALIGNED{color:var(--ok)}.ONLY_OLD,.ONLY_NEW{color:var(--only)}
main{display:grid;grid-template-columns:420px 1fr;min-height:calc(100vh - 90px)}
aside{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;max-height:calc(100vh - 90px)}
.tools{padding:10px;display:flex;gap:6px;border-bottom:1px solid var(--line)}
.tools input,.tools select{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
.tools input{flex:1;min-width:0}
.list{overflow:auto;flex:1}
table{border-collapse:collapse;width:100%;font-size:13px}
th{position:sticky;top:0;background:var(--panel);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.sel td{background:var(--hl)}
tbody tr{cursor:pointer}
section{padding:16px 20px;min-width:0}
.title{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.title h2{margin:0;font-size:18px}
.readout{color:var(--mute);font-variant-numeric:tabular-nums;min-height:20px;margin:6px 0 10px}
.sides{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card h3{margin:0 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.04em}
.card.old h3{color:var(--old)}.card.new h3{color:var(--new)}
.lbl{font-size:12px;color:var(--mute);margin:6px 0 0}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--mute);font-size:11px}
.axis{stroke:var(--line)}
.gapcard{margin-top:14px}
.note{color:var(--mute);font-size:12px;margin-top:10px}
.opt{color:var(--mute);font-size:13px;display:flex;align-items:center;gap:6px;cursor:pointer}
.empty{color:var(--mute);padding:40px;text-align:center}
@media (max-width:900px){main{grid-template-columns:1fr}aside{max-height:320px;border-right:0;border-bottom:1px solid var(--line)}.sides{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>Volume profile comparison</h1>
  <div class="files">old <b id="fOld"></b> &nbsp;·&nbsp; new <b id="fNew"></b> &nbsp;·&nbsp; <span id="fZone"></span> &nbsp;·&nbsp; off when the cumulated curves differ by more than <b id="fThr"></b> pp</div>
  <div class="counts" id="counts"></div>
</header>
<main>
  <aside>
    <div class="tools">
      <input id="q" placeholder="Filter code or venue" autocomplete="off">
      <select id="vf"><option value="">All</option><option>OFF</option><option>ALIGNED</option><option value="ONLY">Only in one</option></select>
    </div>
    <div class="list"><table><thead><tr>
      <th data-k="c">Code</th><th data-k="v">Venue</th><th data-k="g" class="num">Gap pp</th><th data-k="at">At</th><th data-k="verdict">Verdict</th>
    </tr></thead><tbody id="rows"></tbody></table></div>
  </aside>
  <section id="view"><div class="empty">Pick a stock</div></section>
</main>
<script>
const D = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const W = 560, PADL = 40, PADR = 10;
let rows = D.stocks.slice(), shown = [], sel = null, sortK = null, sortDir = 1;
let noAuctions = false;

$("#fOld").textContent = D.old; $("#fNew").textContent = D.new;
$("#fZone").textContent = D.zone ? "times in " + D.zone : "";
$("#fThr").textContent = D.threshold.toFixed(2);
const cnt = {};
for (const s of D.stocks) cnt[s.verdict] = (cnt[s.verdict] || 0) + 1;
$("#counts").innerHTML =
  `<span>${(cnt.ALIGNED || 0) + (cnt.OFF || 0)} in both files</span>` +
  `<span class="pill ALIGNED">${cnt.ALIGNED || 0} aligned</span>` +
  `<span class="pill OFF">${cnt.OFF || 0} off</span>` +
  (cnt.ONLY_OLD ? `<span class="pill ONLY_OLD">${cnt.ONLY_OLD} only in old</span>` : "") +
  (cnt.ONLY_NEW ? `<span class="pill ONLY_NEW">${cnt.ONLY_NEW} only in new</span>` : "");

function clock(origin, s) {
  const t = ((origin + s) % 86400 + 86400) % 86400;
  return `${Math.floor(t / 3600)}:${String(Math.floor(t / 60) % 60).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}
const esc = t => String(t).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

function listRows() {
  const q = $("#q").value.trim().toLowerCase(), vf = $("#vf").value;
  shown = rows.filter(s => (!q || (s.c + " " + s.v + " " + s.r).toLowerCase().includes(q)) &&
    (!vf || (vf === "ONLY" ? s.verdict.startsWith("ONLY") : s.verdict === vf)));
  $("#rows").innerHTML = shown.map((s, i) => {
    const both = s.verdict === "OFF" || s.verdict === "ALIGNED";
    return `<tr data-i="${i}"${s === sel ? ' class="sel"' : ""}><td>${esc(s.c)}</td><td>${esc(s.v)}</td>` +
      `<td class="num">${both ? (s.g > 0 ? "+" : "") + s.g.toFixed(2) : ""}</td><td>${esc(s.at)}</td>` +
      `<td class="${s.verdict}">${s.verdict.replace("_", " ")}</td></tr>`;
  }).join("");
}
$("#rows").addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (tr) pick(shown[+tr.dataset.i]);
});
$("#q").addEventListener("input", listRows);
$("#vf").addEventListener("change", listRows);
document.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.k; sortDir = sortK === k ? -sortDir : (k === "g" ? -1 : 1); sortK = k;
  rows.sort((a, b) => {
    const x = k === "g" ? Math.abs(a.g) : a[k], y = k === "g" ? Math.abs(b.g) : b[k];
    return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
  });
  listRows();
}));
document.addEventListener("keydown", e => {
  if (e.target === $("#q") || !shown.length || !["ArrowDown", "ArrowUp"].includes(e.key)) return;
  e.preventDefault();
  const i = shown.indexOf(sel), j = e.key === "ArrowDown" ? Math.min(shown.length - 1, i + 1) : Math.max(0, i - 1);
  pick(shown[i < 0 ? 0 : j]);
});

// --- the curves -------------------------------------------------------------
function side(s, which) {
  const c = s[which]; if (!c) return null;
  const t = c[0] || s.o[0];
  return {t, c: c[1]};
}
function stepAt(sd, x) {
  if (!sd) return null;
  let lo = 0, hi = sd.t.length - 1, v = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (sd.t[m] <= x) { v = sd.c[m]; lo = m + 1; } else hi = m - 1; }
  return v;
}
function shares(sd) {
  return sd.c.map((v, i) => ({a: i ? sd.t[i - 1] : sd.t[0], b: sd.t[i], h: i ? v - sd.c[i - 1] : v}));
}

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function frame(h, w) {
  return el("svg", {viewBox: `0 0 ${w || W} ${h}`});
}
function xTicks(x0, x1) {
  const span = x1 - x0, steps = [300, 900, 1800, 3600, 7200, 10800];
  const st = steps.find(v => span / v <= 7) || 21600, out = [];
  for (let v = Math.ceil(x0 / st) * st; v <= x1; v += st) out.push(v);
  return out;
}

function draw(s) {
  const o = side(s, "o"), n = side(s, "n");
  const grid = [...new Set([...(o ? o.t : []), ...(n ? n.t : [])])].sort((a, b) => a - b);
  // Start where trading does: the last bucket at which both are still 0.
  let first = grid.findIndex(t => (stepAt(o, t) || 0) > 0 || (stepAt(n, t) || 0) > 0);
  const x0 = grid[Math.max(0, first - 1)], x1 = grid[grid.length - 1];
  const scaleX = w => v => PADL + (x1 > x0 ? (v - x0) / (x1 - x0) : 0.5) * (w - PADL - PADR);
  const X = scaleX(W), WG = 2 * W + 30, XG = scaleX(WG);
  const cumMax = Math.max(1, ...(o ? o.c : []), ...(n ? n.c : []));
  // THE AUCTIONS CAN FLATTEN EVERYTHING ELSE: a 20% close against 1% buckets.
  // Optionally the scale leaves out each side's first trading bucket and its
  // last one; a bar taller than the scale is clipped and labelled.
  const inView = sd => shares(sd).filter(b => b.b > x0);
  const barMax = Math.max(1e-9, ...[o, n].filter(Boolean).flatMap(sd => {
    const bs = inView(sd).map(b => b.h);
    return noAuctions && bs.length > 2 ? bs.slice(1, -1) : bs;
  }));
  const gaps = (o && n) ? grid.map(t => (stepAt(n, t) - stepAt(o, t)) * 100) : [];
  let worst = -1;
  gaps.forEach((g, i) => { if (worst < 0 || Math.abs(g) > Math.abs(gaps[worst])) worst = i; });
  const markX = worst >= 0 ? grid[worst] : null;
  const ticks = xTicks(x0, x1), cross = [];

  function axisX(svg, y, Xf = X, w = W) {
    el("line", {x1: PADL, x2: w - PADR, y1: y, y2: y, class: "axis"}, svg);
    for (const t of ticks) {
      el("line", {x1: Xf(t), x2: Xf(t), y1: y, y2: y + 4, class: "axis"}, svg);
      el("text", {x: Xf(t), y: y + 15, "text-anchor": "middle"}, svg).textContent = clock(s.origin, t).slice(0, -3);
    }
  }
  function marks(svg, top, bottom, Xf = X) {
    if (markX !== null)
      el("line", {x1: Xf(markX), x2: Xf(markX), y1: top, y2: bottom, stroke: "var(--off)", "stroke-dasharray": "3 3", "stroke-width": 1}, svg);
    const c = el("line", {x1: 0, x2: 0, y1: top, y2: bottom, stroke: "var(--mute)", "stroke-width": 1, visibility: "hidden"}, svg);
    c.X = Xf;
    cross.push(c);
  }
  function cumChart(sd, color) {
    const H = 190, top = 8, bot = 160, svg = frame(H);
    const Y = v => bot - v / cumMax * (bot - top);
    for (const p of [0, .25, .5, .75, 1]) {
      el("line", {x1: PADL, x2: W - PADR, y1: Y(p), y2: Y(p), class: "axis", "stroke-dasharray": p ? "2 3" : ""}, svg);
      el("text", {x: PADL - 6, y: Y(p) + 4, "text-anchor": "end"}, svg).textContent = p * 100 + "%";
    }
    axisX(svg, bot);
    if (sd) {
      let d = "", prev = 0;
      sd.t.forEach((t, i) => {
        if (t < x0) { prev = sd.c[i]; return; }
        d += (d ? `H${X(t)}V${Y(sd.c[i])}` : `M${X(x0)},${Y(prev)}H${X(t)}V${Y(sd.c[i])}`);
        prev = sd.c[i];
      });
      el("path", {d, fill: "none", stroke: color, "stroke-width": 2}, svg);
    } else el("text", {x: W / 2, y: 90, "text-anchor": "middle"}, svg).textContent = "not in this file";
    marks(svg, top, bot);
    return svg;
  }
  function barChart(sd, color) {
    const H = 150, top = 6, bot = 120, svg = frame(H);
    const Y = v => bot - Math.max(0, v) / barMax * (bot - top);
    el("text", {x: PADL - 6, y: top + 8, "text-anchor": "end"}, svg).textContent = (barMax * 100).toFixed(1) + "%";
    axisX(svg, bot);
    if (sd) for (const b of shares(sd)) {
      if (b.b <= x0) continue;
      const xa = X(Math.max(b.a, x0)), xb = X(b.b), w = Math.max(1, xb - xa - 0.5);
      const clipped = b.h > barMax * 1.0001, y = Y(Math.min(b.h, barMax));
      el("rect", {x: xb - w, y, width: w, height: bot - y, fill: color, opacity: .85}, svg);
      if (clipped) el("text", {x: xb - w / 2, y: top + 8, "text-anchor": "end"}, svg).textContent = (b.h * 100).toFixed(1) + "% ▲";
    }
    marks(svg, top, bot);
    return svg;
  }
  function gapChart() {
    const H = 130, top = 8, bot = 100, mid = (top + bot) / 2, svg = frame(H, WG), X = XG;
    const lim = Math.max(D.threshold * 1.25, ...gaps.map(Math.abs)) || 1;
    const Y = g => mid - g / lim * (mid - top);
    for (const g of [D.threshold, -D.threshold])
      el("line", {x1: PADL, x2: WG - PADR, y1: Y(g), y2: Y(g), stroke: "var(--off)", "stroke-dasharray": "4 3", opacity: .6}, svg);
    el("line", {x1: PADL, x2: WG - PADR, y1: mid, y2: mid, class: "axis"}, svg);
    for (const g of [lim, 0, -lim])
      el("text", {x: PADL - 6, y: Y(g) + 4, "text-anchor": "end"}, svg).textContent = (g > 0 ? "+" : "") + g.toFixed(1);
    axisX(svg, bot, XG, WG);
    let d = "";
    grid.forEach((t, i) => { if (t >= x0) d += (d ? "L" : "M") + X(t) + "," + Y(gaps[i]); });
    el("path", {d, fill: "none", stroke: "var(--ink)", "stroke-width": 1.5}, svg);
    marks(svg, top, bot, XG);
    return svg;
  }

  const v = $("#view");
  const both = o && n;
  v.innerHTML = `<div class="title"><h2>${esc(s.c)} <span style="color:var(--mute);font-weight:400">${esc(s.v)}</span></h2>` +
    `<span class="pill ${s.verdict}">${s.verdict.replace("_", " ")}</span>` +
    (both ? `<span>max gap <b>${(s.g > 0 ? "+" : "") + s.g.toFixed(2)} pp</b>${s.at ? " at " + esc(s.at) : ""}</span>` : "") +
    `<label class="opt"><input type="checkbox" id="na"${noAuctions ? " checked" : ""}> bar scale without the auctions</label>` +
    `</div><div class="readout" id="ro">Hover a chart to read both curves at one time</div>` +
    `<div class="sides"><div class="card old"><h3>Old · ${esc(D.old)}</h3><div class="lbl">Cumulated volume</div></div>` +
    `<div class="card new"><h3>New · ${esc(D.new)}</h3><div class="lbl">Cumulated volume</div></div></div>` +
    (both ? `<div class="card gapcard"><div class="lbl">Gap, new − old, percentage points of the day · dashed: ±${D.threshold.toFixed(2)} threshold</div></div>` : "") +
    `<div class="note">Both sides share one time axis and one scale for each chart, so their shapes can be compared by eye. The red dashed line is the time of the largest gap.</div>`;
  const cards = v.querySelectorAll(".sides .card");
  [[o, "var(--old)", cards[0]], [n, "var(--new)", cards[1]]].forEach(([sd, col, card]) => {
    card.appendChild(cumChart(sd, col));
    const l = document.createElement("div"); l.className = "lbl"; l.textContent = "Volume per bucket"; card.appendChild(l);
    card.appendChild(barChart(sd, col));
  });
  if (both) v.querySelector(".gapcard").appendChild(gapChart());

  $("#na").addEventListener("change", e => { noAuctions = e.target.checked; draw(s); });
  const ro = $("#ro");
  v.querySelectorAll("svg").forEach(svg => {
    svg.addEventListener("mousemove", e => {
      const vw = svg.viewBox.baseVal.width, r = svg.getBoundingClientRect(), px = (e.clientX - r.left) / r.width * vw;
      const xv = x0 + (px - PADL) / (vw - PADL - PADR) * (x1 - x0);
      let best = grid[0];
      for (const t of grid) if (Math.abs(t - xv) < Math.abs(best - xv)) best = t;
      for (const c of cross) { c.setAttribute("x1", c.X(best)); c.setAttribute("x2", c.X(best)); c.setAttribute("visibility", "visible"); }
      const ov = stepAt(o, best), nv = stepAt(n, best);
      ro.innerHTML = `<b>${clock(s.origin, best)}</b> &nbsp; old ${ov === null ? "—" : (ov * 100).toFixed(2) + "%"}` +
        ` &nbsp; new ${nv === null ? "—" : (nv * 100).toFixed(2) + "%"}` +
        (both ? ` &nbsp; gap <b class="${Math.abs((nv - ov) * 100) > D.threshold ? "OFF" : ""}">${((nv - ov) * 100 >= 0 ? "+" : "") + ((nv - ov) * 100).toFixed(2)} pp</b>` : "");
    });
    svg.addEventListener("mouseleave", () => cross.forEach(c => c.setAttribute("visibility", "hidden")));
  });
}

function pick(s) {
  if (!s) return;
  //  Only the highlight moves: rebuilding thousands of rows per step made
  //  the arrow keys lag.
  const was = $("#rows tr.sel"); if (was) was.classList.remove("sel");
  sel = s; draw(s);
  const tr = $(`#rows tr[data-i="${shown.indexOf(s)}"]`);
  if (tr) { tr.classList.add("sel"); tr.scrollIntoView({block: "nearest"}); }
}
listRows();
pick(shown[0]);
</script>
</body>
</html>
"""


# =============================================================================
# SELF-TEST
# =============================================================================

def self_test() -> int:
    import contextlib
    import io
    import tempfile
    ok = True
    quiet_run = globals()["run"]

    def run(*a):
        #  The runs below would print their own summaries into the report.
        with contextlib.redirect_stdout(io.StringIO()),                 contextlib.redirect_stderr(io.StringIO()):
            return quiet_run(*a)

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    HEAD = ("#TimeZone=India Standard Time,format=V2,isweekly=false\n"
            "#FidessaCode,ReutersCode,Venue,TimeZone,Time,CumulatedPercentage\n")

    def rows(code, pairs, venue="NSI-MAIN", zone="India Standard Time"):
        return "".join(f"{code},{code[:4]}.NS,{venue},{zone},{t},{v}\n"
                       for t, v in pairs)

    U = [("0:00:00", 0), ("9:00:00", 0), ("9:15:00", 0.10), ("12:00:00", 0.50),
         ("15:25:00", 0.80), ("15:30:00", 1.0)]

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def files(old_text, new_text):
            a, b = d / "old.csv", d / "new.csv"
            a.write_text(old_text, encoding="utf-8")
            b.write_text(new_text, encoding="utf-8")
            return read_profile(a), read_profile(b)

        print("compare_profiles --self-test\n\nthe same profile twice")
        old, new = files(HEAD + rows("AAA.IN", U), HEAD + rows("AAA.IN", U))
        r = compare_files(old, new, 2.0)
        check("is aligned, with no gap", (r[0].verdict, r[0].gap),
              ("ALIGNED", 0.0))

        print("\na curve that runs ahead")
        ahead = [(t, min(1.0, v + 0.03) if v and v < 1 else v) for t, v in U]
        old, new = files(HEAD + rows("AAA.IN", U), HEAD + rows("AAA.IN", ahead))
        r = compare_files(old, new, 2.0)[0]
        check("3 pp ahead is OFF at 2 pp", r.verdict, "OFF")
        check("the gap is signed, new minus old", round(r.gap, 6), 3.0)
        check("and says when", r.at, "9:15:00")
        check("the same curve is aligned at a 3 pp threshold",
              compare_files(old, new, 3.0)[0].verdict, "ALIGNED")

        print("\na different closing auction")
        close = U[:-2] + [("15:25:00", 0.88), ("15:30:00", 1.0)]
        old, new = files(HEAD + rows("AAA.IN", U), HEAD + rows("AAA.IN", close))
        r = compare_files(old, new, 2.0)[0]
        check("8 pp less in the auction is found at the last pre-auction "
              "bucket", (r.verdict, round(r.gap, 6), r.at),
              ("OFF", 8.0, "15:25:00"))

        print("\ndifferent bucket grids")
        fine = [("0:00:00", 0), ("9:15:00", 0.10), ("10:30:00", 0.30),
                ("12:00:00", 0.50), ("15:25:00", 0.80), ("15:30:00", 1.0)]
        old, new = files(HEAD + rows("AAA.IN", U), HEAD + rows("AAA.IN", fine))
        r = compare_files(old, new, 2.0)[0]
        check("an extra bucket is compared against the other curve's last "
              "value - 30% against 10%, 20 pp", round(r.gap, 6), 20.0)
        check("at the time only one file has", r.at, "10:30:00")

        print("\na file missing its first bucket")
        S = [("9:15:00", 0.10), ("12:00:00", 0.50), ("15:25:00", 0.80),
             ("15:30:00", 1.0)]
        old, new = files(HEAD + rows("AAA.IN", S[1:]), HEAD + rows("AAA.IN", S))
        r = compare_files(old, new, 2.0)[0]
        check("NEW starting first is 0 against 10% at its open, not wrapped "
              "to the end of the day", (round(r.gap, 6), r.at),
              (10.0, "9:15:00"))
        check("and the page draws from that same open",
              page_data(old, new, [r], 2.0)["stocks"][0]["origin"],
              parse_time("9:15:00"))
        old, new = files(HEAD + rows("AAA.IN", S), HEAD + rows("AAA.IN", S[1:]))
        r = compare_files(old, new, 2.0)[0]
        check("and OLD starting first is the mirror", (round(r.gap, 6), r.at),
              (-10.0, "9:15:00"))

        print("\na stock in only one file")
        old, new = files(HEAD + rows("AAA.IN", U) + rows("BBB.IN", U),
                         HEAD + rows("AAA.IN", U) + rows("CCC.IN", U))
        r = {x.code: x.verdict for x in compare_files(old, new, 2.0)}
        check("each side's extra is named",
              (r["BBB.IN"], r["CCC.IN"]), ("ONLY_OLD", "ONLY_NEW"))
        check("one code on two venues is two stocks",
              len(files(HEAD + rows("AAA.IN", U) + rows("AAA.IN", U, "BSE"),
                        HEAD + rows("AAA.IN", U))[0].stocks), 2)

        print("\na file re-saved through Excel")
        excel = ("FidessaCode;Venue;Time;CumulatedPercentage\n"
                 + "".join(f"AAA.IN;NSI-MAIN;{t[:-3]};{str(v * 100).replace('.', ',')}\n"
                           for t, v in U)
                 + "AAA.IN;NSI-MAIN;not a time;5\n")
        old, new = files(HEAD + rows("AAA.IN", U), excel)
        r = compare_files(old, new, 2.0)[0]
        check("semicolons, decimal commas, percent and HH:MM read the same",
              (r.verdict, round(r.gap, 6)), ("ALIGNED", 0.0))
        check("the bad row is counted, not fatal", new.skipped, 1)
        check("an Excel time serial is a time of day",
              (parse_time("0.385416666667"), parse_time("46000.5")),
              (33300, 43200))

        print("\ntime zones")
        try:
            files(HEAD + rows("AAA.IN", U),
                  HEAD + rows("AAA.IN", U, zone="Tokyo Standard Time"))
            compare_files(*files(HEAD + rows("AAA.IN", U),
                                 HEAD + rows("AAA.IN", U,
                                             zone="Tokyo Standard Time")),
                          2.0)
            check("different zones raised", False, True)
        except InputError as e:
            check("different zones are refused, naming the stock",
                  "AAA.IN" in str(e), True)

        print("\na session across midnight")
        night = [("22:00:00", 0.2), ("23:30:00", 0.6), ("1:00:00", 1.0)]
        late = [("22:00:00", 0.2), ("23:30:00", 0.6), ("1:00:00", 1.0),
                ("0:30:00", 0.9)]
        old, new = files(HEAD + rows("AAA.IN", night),
                         HEAD + rows("AAA.IN", late))
        r = compare_files(old, new, 2.0)[0]
        check("00:30 falls between 23:30 and 01:00, not before 22:00",
              (round(r.gap, 6), r.at), (30.0, "0:30:00"))

        print("\nthe outputs")
        old, new = files(HEAD + rows("AAA.IN", U) + rows("BBB.IN", U),
                         HEAD + rows("AAA.IN", ahead) + rows("BBB.IN", U))
        out = d / "out"
        code = run(d / "old.csv", d / "new.csv", 2.0, out)
        lines = (out / "comparison.csv").read_text().splitlines()
        check("the CSV has a header and one row per stock, worst first",
              [l.split(",")[0] for l in lines], ["FidessaCode", "AAA.IN",
                                                 "BBB.IN"])
        check("with the verdict and the gap", lines[1].split(",")[3:5],
              ["OFF", "3.00"])
        page = (out / "comparison.html").read_text(encoding="utf-8")
        check("the page carries the data, and no placeholder",
              ("/*__DATA__*/" in page, '"c":"AAA.IN"' in page), (False, True))
        check("and loads nothing from the network",
              any(s in page for s in ("http://", "https://")
                  if s + "www.w3.org" not in page), False)
        check("anything off exits 1", code, 1)
        (d / "new.csv").write_text(HEAD + rows("AAA.IN", U)
                                   + rows("BBB.IN", U), encoding="utf-8")
        check("all aligned exits 0", run(d / "old.csv", d / "new.csv", 2.0,
                                         out), 0)
        (d / "new.csv").write_text("not,a,profile\n", encoding="utf-8")
        check("an unreadable file exits 2",
              run(d / "old.csv", d / "new.csv", 2.0, out), 2)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Compare two volume profiles of one market: is the new "
                    "one aligned with the old one, stock by stock?")
    p.add_argument("old", nargs="?", help="the reference profile CSV")
    p.add_argument("new", nargs="?", help="the profile being checked")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help="largest cumulated gap, in percentage points, still "
                        f"called aligned (default {DEFAULT_THRESHOLD})")
    p.add_argument("--out", default=".",
                   help="where comparison.csv and comparison.html go "
                        "(default: here)")
    p.add_argument("--self-test", action="store_true",
                   help="checks on made-up profiles, no files needed")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if not (a.old and a.new):
        p.error("give the OLD and the NEW profile")
    return run(a.old, a.new, a.threshold, a.out)


if __name__ == "__main__":
    sys.exit(main())
