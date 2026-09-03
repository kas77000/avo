#!/usr/bin/env python3
"""Read CrossCode.csv and cut it down to the rows we will price today.

THREE FILTERS, in this order:

  security type      Equity and ETF only
  configured venue   a FidessaMarket with no row in markets.csv
  cutoff             a venue whose Time has not yet passed

The cutoff is why running this at 07:59 and again at 09:03 produces
different files.  Each run rewrites the WHOLE output with only the venues
that are open, so the 09:03 run republishes Korea and adds China.  That is
long-standing behaviour, and changing it silently would strand a market.

Nothing is dropped quietly.  Every filter returns what it removed so the run
can report it.

FUTURE: the static-limit exclusion goes here.  India's in-nse_drv.stra and
in-bse_drv.stra list names whose limits are configured in the ATS strategy
file and which must therefore NOT get a published limit.  India is out of
scope today; when it returns, the filter belongs between the venue and
cutoff checks and needs one optional ExcludeFile column in markets.csv.

    python crosscode.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

KEEP_TYPES = ("Equity", "ETF")


@dataclass(frozen=True)
class Row:
    ric: str
    bbg: str
    ticker: str
    fidessa_code: str
    venue_id: str
    sec_type: str


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)


def split_bbg(bbg: str):
    """('005930 KP') -> ('005930', 'KP')."""
    parts = (bbg or "").rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def load(path, venues: dict, now: time):
    kept = []
    by_reason = {}

    def drop(reason, ric):
        by_reason.setdefault(reason, []).append(ric)

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            ric = (r.get("RicCode") or "").strip()
            sec_type = (r.get("Type") or "").strip()
            venue_id = (r.get("FidessaMarket") or "").strip()
            if sec_type not in KEEP_TYPES:
                drop("security type not Equity/ETF", ric)
                continue
            v = venues.get(venue_id)
            if v is None:
                drop("venue not in markets.csv", ric)
                continue
            if now < v.cutoff:
                drop("cutoff not reached", ric)
                continue
            bbg = (r.get("BloombergCode") or "").strip()
            ticker, _exch = split_bbg(bbg)
            kept.append(Row(ric=ric, bbg=bbg, ticker=ticker,
                            fidessa_code=(r.get("FidessaCode") or "").strip(),
                            venue_id=venue_id, sec_type=sec_type))

    return kept, [Excluded(reason=k, rows=v)
                  for k, v in sorted(by_reason.items())]


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    import marketcfg
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def venue(vid, cutoff):
        return marketcfg.Venue(
            country="X", venue_id=vid, bbg_venue="XX", bbg_composite="XX",
            cutoff=cutoff, ref_price="close_print", tick_source="config",
            min_price=None, rounding="inward")

    V = {"JKT-MAIN": venue("JKT-MAIN", time(7, 59)),
         "SHA-MAIN": venue("SHA-MAIN", time(9, 3))}

    HDR = "RicCode,BloombergCode,FidessaCode,FidessaMarket,Type\n"
    BODY = ("BBCA.JK,BBCA IJ,BBCA.ID,JKT-MAIN,Equity\n"
            "600001.SS,600001 CG,600001.CN,SHA-MAIN,Equity\n"
            "XYZ.JK,XYZ IJ,XYZ.ID,JKT-MAIN,Warrant\n"
            "ABC.KS,ABC KP,ABC.KR,KSC-MAIN,Equity\n")

    print("crosscode --self-test\n\nsplitting the bloomberg code")
    check("ticker and exchange", split_bbg("005930 KP"), ("005930", "KP"))
    check("a ticker with a space in it keeps everything but the last word",
          split_bbg("BRK/A US Equity"), ("BRK/A US", "Equity"))
    check("no space at all", split_bbg("ABC"), ("ABC", ""))
    check("nothing at all", split_bbg(""), ("", ""))

    print("\nfiltering")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + BODY, encoding="utf-8")

        rows, excl = load(p, V, time(10, 0))
        check("both open venues, equities only", sorted(r.ric for r in rows),
              ["600001.SS", "BBCA.JK"])
        check("the ticker is the bbg code without the exchange",
              [r.ticker for r in rows if r.ric == "600001.SS"], ["600001"])
        reasons = {e.reason: e.rows for e in excl}
        check("the warrant is reported, not vanished",
              reasons["security type not Equity/ETF"], ["XYZ.JK"])
        check("so is the venue nobody configured",
              reasons["venue not in markets.csv"], ["ABC.KS"])

        rows, excl = load(p, V, time(8, 30))
        check("at 08:30 Shanghai has not opened yet",
              [r.ric for r in rows], ["BBCA.JK"])
        reasons = {e.reason: e.rows for e in excl}
        check("and says so", reasons["cutoff not reached"], ["600001.SS"])

        rows, excl = load(p, V, time(7, 0))
        check("before every cutoff, nothing is published", rows, [])

        rows, _ = load(p, V, time(7, 59))
        check("a cutoff is reached AT its time, not after",
              [r.ric for r in rows], ["BBCA.JK"])

    print("\nan ETF is kept, everything exotic is not")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + "E.JK,E IJ,E.ID,JKT-MAIN,ETF\n"
                           "R.JK,R IJ,R.ID,JKT-MAIN,Right\n"
                           "B.JK,B IJ,B.ID,JKT-MAIN,Bond\n", encoding="utf-8")
        rows, excl = load(p, V, time(10, 0))
        check("the ETF survives", [r.ric for r in rows], ["E.JK"])
        check("the right and the bond are both reported",
              {e.reason: sorted(e.rows) for e in excl},
              {"security type not Equity/ETF": ["B.JK", "R.JK"]})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
