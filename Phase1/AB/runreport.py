#!/usr/bin/env python3
"""What one extract.py run did, for the mail at its end: a subject, a short
plain-text body, and one self-contained HTML page to attach.

build() fills a RunReport as it goes - the universe, each market, every
fallback close, the zip - and logs.Log hands it every `!!` and `XX` line
through its on_emit hook.  Nothing here reads the log file.

THE HTML IS FOR OUTLOOK.  Plain tables, inline CSS only, no script, no
external asset, every text escaped.

    python runreport.py --self-test
"""

from __future__ import annotations

import datetime as dt
import html
import socket
from pathlib import Path

ALL_USES = ("luld", "td", "ticks")

#  A market's row: its counts, then how long it took.
MARKET_COLUMNS = ("syms", "ticks", "prints", "close-code", "last-trade",
                  "none-before-cutoff", "no-trades", "no-close",
                  "quote-only", "seconds")
#  The closes by source, in the body and the totals.
CLOSE_KINDS = ("close-code", "last-trade", "none-before-cutoff",
               "no-trades", "no-close")


def close_kind(source, reason) -> str:
    """closes.csv's (source, reason) as one of CLOSE_KINDS."""
    if source == "qatt":
        return "last-trade" if reason == "last-trade" else "close-code"
    if source == "equity_master":
        return reason if reason in CLOSE_KINDS else "no-trades"
    return "no-close"


class RunReport:
    def __init__(self, uses=ALL_USES, today=None):
        self.started = dt.datetime.now()
        self.ended = None
        self.host = socket.gethostname()
        self.today = today or dt.date.today()
        self.uses = tuple(uses)
        self.params = {}            # name -> value, as asked
        self.run = {}               # day, source, equity_master date, ...
        self.trade_date = None
        self.cutoffs = []           # (code, HKT, kdb)
        self.universe = {}          # rows, kept, dropped [(reason, n, top)]
        self.markets = {}           # market -> {column: value, "note": ""}
        self.markets_total = 0
        self.missing = []           # markets still to do
        self.fallbacks = []         # (sym, market, reason, close)
        self.lines = []             # (level, text): every !! and XX
        self.stage = None
        self.zip = None
        self.zip_size = 0
        self.members = []
        self.manifest = {}
        self.status = ""            # OK, PARTIAL or FAILED

    # -- filled as the run goes ---------------------------------------------

    def capture(self, level, text) -> None:
        """logs.Log's on_emit hook: keep every !! and XX line."""
        if level in ("!!", "XX"):
            self.lines.append((level, text))

    def market(self, mkt, closes_rows, prints="", quote_only="",
               seconds="", note="") -> None:
        """A market's row, from its closes - (sym, close, source, reason)
        dicts - and what the run measured.  Its fallbacks go to the list."""
        row = dict.fromkeys(MARKET_COLUMNS, 0)
        row.update(prints=prints, **{"quote-only": quote_only},
                   seconds=seconds, note=note)
        for r in closes_rows:
            kind = close_kind(r["source"], r["reason"])
            row["syms"] += 1
            row[kind] += 1
            if r["source"] == "qatt":
                row["ticks"] += 1
            else:
                self.fallbacks.append((r["sym"], mkt, r.get("why") or
                                       r["reason"], r["close"]))
        self.markets[mkt] = row

    def totals(self) -> dict:
        return {k: sum(m[k] for m in self.markets.values())
                for k in CLOSE_KINDS}

    def first_fail(self) -> str:
        return next((t for lvl, t in self.lines if lvl == "XX"), "")

    # -- the mail -------------------------------------------------------------

    def date(self):
        return self.trade_date or self.today

    def subject(self) -> str:
        head = f"[Phase1] extract {self.date()}"
        if self.uses != ALL_USES:
            head += f" ({'|'.join(self.uses)})"
        if self.status == "OK":
            tail = f"OK - {Path(self.zip).name if self.zip else ''}"
        elif self.status == "PARTIAL":
            tail = f"PARTIAL - {len(self.missing)} markets still to do"
        else:
            first = self.first_fail() or "no zip written"
            tail = "FAILED - " + (first if len(first) <= 120
                                  else first[:117] + "...")
        return f"{head}: {tail}"

    def duration(self) -> str:
        end = self.ended or dt.datetime.now()
        secs = int((end - self.started).total_seconds())
        return f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}"

    def body(self) -> str:
        t = self.totals()
        done = self.markets_total - len(self.missing)
        lines = [
            self.subject(), "",
            f"trade date   {self.date()}",
            f"source       {self.run.get('source', '')}",
            f"--for        {'|'.join(self.uses)}",
            f"--market     {self.params.get('--market') or 'all'}",
            f"zip          {self.zip or 'none'}"
            + (f"  ({self.zip_size / 1e6:,.1f} MB)" if self.zip else ""),
            f"duration     {self.duration()}",
            "closes       " + ", ".join(f"{k} {t[k]:,}" for k in CLOSE_KINDS),
            f"markets      {done} of {self.markets_total} done",
            f"log          {sum(l == '!!' for l, _ in self.lines)} !!, "
            f"{sum(l == 'XX' for l, _ in self.lines)} XX",
            "", "The attached report has every market, fallback and warning."]
        return "\n".join(lines) + "\n"

    # -- the page ---------------------------------------------------------------

    def html(self) -> str:
        e = html.escape
        css_t = ("border-collapse:collapse;font-family:Arial,sans-serif;"
                 "font-size:12px;margin-bottom:16px")
        css_c = "border:1px solid #999;padding:3px 6px;text-align:left"

        def table(head, rows):
            out = [f'<table style="{css_t}"><tr>']
            out += [f'<th style="{css_c};background:#ddd">{e(str(h))}</th>'
                    for h in head]
            out.append("</tr>")
            for r in rows:
                out.append("<tr>" + "".join(
                    f'<td style="{css_c}">{e(str(v))}</td>' for v in r)
                    + "</tr>")
            if not rows:
                out.append(f'<tr><td style="{css_c}" colspan="{len(head)}">'
                           f'none</td></tr>')
            return "".join(out) + "</table>"

        run = [("status", self.status), ("host", self.host),
               ("started", f"{self.started:%Y-%m-%d %H:%M:%S}"),
               ("ended", (self.ended or dt.datetime.now())
                .strftime("%Y-%m-%d %H:%M:%S")),
               ("duration", self.duration()),
               ("trade date", self.date())]
        run += [(k, v) for k, v in self.params.items()]
        run += [(k, v) for k, v in self.run.items()]
        run += [("LastTradeBefore", f"{c} {h} HKT = {k} kdb")
                for c, h, k in self.cutoffs] or [("LastTradeBefore", "none")]
        uni = [("CrossCode rows", self.universe.get("rows", "")),
               ("kept", self.universe.get("kept", ""))]
        uni += [(f"dropped: {r}", f"{n}" + (f" ({top})" if top else ""))
                for r, n, top in self.universe.get("dropped", [])]
        mk = [[m] + [row[c] for c in MARKET_COLUMNS] + [row.get("note", "")]
              for m, row in sorted(self.markets.items())]
        mk += [[m] + [""] * len(MARKET_COLUMNS) + ["still to do"]
               for m in self.missing if m not in self.markets]
        out = [("zip", self.zip or "none"),
               ("size", f"{self.zip_size:,} bytes" if self.zip else ""),
               ("members", ", ".join(self.members))]
        out += [(f"manifest: {k}", v) for k, v in self.manifest.items()]
        return "".join([
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
            f"<title>{e(self.subject())}</title></head>",
            '<body style="font-family:Arial,sans-serif;font-size:12px">',
            f"<h1 style=\"font-size:16px\">{e(self.subject())}</h1>",
            "<h2 style=\"font-size:14px\">Run</h2>",
            table(("", ""), run),
            "<h2 style=\"font-size:14px\">Universe</h2>",
            table(("", ""), uni),
            "<h2 style=\"font-size:14px\">Per market</h2>",
            table(("market",) + MARKET_COLUMNS + ("",), mk),
            "<h2 style=\"font-size:14px\">Fallbacks</h2>",
            table(("sym", "market", "reason", "close"), self.fallbacks),
            "<h2 style=\"font-size:14px\">Warnings and errors</h2>",
            table(("level", "line"), self.lines),
            "<h2 style=\"font-size:14px\">Output</h2>",
            table(("", ""), out),
            "</body></html>\n"])

    def write_html(self, folder) -> Path:
        """phase1-YYYYMMDD-report.html in `folder`."""
        path = Path(folder) / f"phase1-{self.date():%Y%m%d}-report.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.html(), encoding="utf-8")
        return path


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("runreport --self-test\n\nthe subject")
    r = RunReport(today=dt.date(2026, 9, 29))
    r.trade_date = dt.date(2026, 9, 28)
    r.status, r.zip = "OK", "C:/x/phase1-20260928.zip"
    check("OK, with the zip", r.subject(),
          "[Phase1] extract 2026-09-28: OK - phase1-20260928.zip")
    r.uses = ("luld", "td")
    r.status, r.missing = "PARTIAL", ["AT", "C1"]
    check("PARTIAL, with the markets left, and --for when not all three",
          r.subject(),
          "[Phase1] extract 2026-09-28 (luld|td): PARTIAL - 2 markets "
          "still to do")
    r.uses, r.status = ALL_USES, "FAILED"
    r.capture("!!", "a warning first")
    r.capture("XX", "the quote table is not on X " + "y" * 200)
    check("FAILED, with the first XX line, cut to 120",
          (r.subject()[:53], len(r.subject().split(": ", 1)[1]) - 9),
          ("[Phase1] extract 2026-09-28: FAILED - the quote table", 120))
    r2 = RunReport(today=dt.date(2026, 9, 29))
    r2.status = "FAILED"
    check("no trade date yet: today's", r2.subject(),
          "[Phase1] extract 2026-09-29: FAILED - no zip written")

    print("\nthe page")
    r.market("KQ", [{"sym": "A.KS", "close": "9", "source": "qatt",
                     "reason": "last-trade"},
                    {"sym": "B.KS", "close": "8", "source": "equity_master",
                     "reason": "none-before-cutoff"},
                    {"sym": "C.KS", "close": "", "source": "",
                     "reason": "no-close", "why": "no-trades"}],
             prints=12, quote_only=0, seconds=1.5)
    r.capture("!!", "close  <b>  a <tag> & more")
    page = r.html()
    check("every section is there",
          [s in page for s in ("<h2 style=\"font-size:14px\">" + h + "</h2>"
                               for h in ("Run", "Universe", "Per market",
                                         "Fallbacks", "Warnings and errors",
                                         "Output"))], [True] * 6)
    check("text is escaped", ("a &lt;tag&gt; &amp; more" in page,
                              "<tag>" in page), (True, False))
    check("no external asset, no script",
          any(x in page for x in ("<script", "<link", "src=", "@import")),
          False)
    check("the fallbacks: PX_LAST and no close, with the reason",
          r.fallbacks, [("B.KS", "KQ", "none-before-cutoff", "8"),
                        ("C.KS", "KQ", "no-trades", "")])
    check("the closes by kind", r.totals(),
          {"close-code": 0, "last-trade": 1, "none-before-cutoff": 1,
           "no-trades": 0, "no-close": 1})
    with tempfile.TemporaryDirectory() as d:
        p = r.write_html(d)
        check("written as phase1-YYYYMMDD-report.html", p.name,
              "phase1-20260928-report.html")
    check("the body counts the !! and XX lines",
          [ln for ln in r.body().splitlines() if ln.startswith("log")],
          ["log          2 !!, 1 XX"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
