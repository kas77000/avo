#!/usr/bin/env python3
"""Read CrossCode.csv.  It is the security master and it drives the row set.

IT ASKS FOR AS LITTLE AS IT CAN.  Two columns are load-bearing -
BloombergCode and FidessaMarket - and a file missing either is refused by
name.  Everything else is a diagnostic, read when present and blank when
not, and the run says which ones were absent.

That is deliberate rather than lax.  The two readers already in this repo
DISAGREE about the file: TradingData wants "#FidessaCode", "Currency" and
"BloombergSecurityType"; LimitUpDown v1 and v2 want "FidessaCode" and
"BloombergStatus".  Both cannot be right, nobody here has the real file to
look at, and every column they differ on is one this job never uses.  So
the reader takes either spelling of the first column and refuses only over
the two it genuinely cannot work without.

Rows with no BloombergCode are dropped - there is nothing to resolve them
with - and reported rather than vanished.

WHAT THIS MODULE DOES NOT DO.  It does not collapse venues, apply the MIC
filter, or decide which row names a file.  All three need equity_master, so
they live in universe.py where the kdb answer is in hand.  This file is the
reader and nothing else.

    python crosscode.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

#  ONLY WHAT THIS JOB ACTUALLY USES.  BloombergCode gives the ticker, the
#  exchange code and the output filename; FidessaMarket gives the composite
#  fallback and the timezone label.  Nothing else is load-bearing, so
#  nothing else is required - a reader that refuses to start over a column
#  it never reads is a reader that refuses to start.
REQUIRED = ("BloombergCode", "FidessaMarket")

#  Read when present, blank when not.  All diagnostics: they make the trace
#  and the exclusion reports legible and nothing else.
OPTIONAL = ("RicCode", "Type", "BloombergSecurityType", "Currency",
            "BloombergStatus")

#  THE FIRST COLUMN IS SPELT TWO WAYS IN THIS REPO.  TradingData reads
#  "#FidessaCode"; LimitUpDown v1 and v2 read "FidessaCode".  Only one of
#  them can match the real file, nobody here has one to look at, and the
#  column is a diagnostic either way - so both spellings are accepted and
#  the run says which it found.
FIDESSA_CODE = ("#FidessaCode", "FidessaCode")


@dataclass(frozen=True)
class Row:
    fidessa_code: str
    ric: str
    bbg: str                 # "7203 JT" - ticker and PRIMARY exchange code
    ticker: str              # "7203"
    bbg_ext: str             # "JT"
    sec_type: str
    bbg_sec_type: str
    market: str
    currency: str


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)


def split_bbg(bbg: str):
    """('005930 KP') -> ('005930', 'KP').

    Split from the RIGHT.  A ticker can contain a space - Thai foreign lines
    and several Korean preferreds do - and only the last field is ever the
    exchange code."""
    parts = (bbg or "").strip().rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def bbg_dotted(bbg: str) -> str:
    """('7203 JT') -> '7203.JT', the shape equity_master stores in sym_bpipe.

    A ticker carrying a space keeps it: only the exchange code moves."""
    ticker, ext = split_bbg(bbg)
    return f"{ticker}.{ext}" if ext else ticker


def bbg_full(bbg: str) -> str:
    """('7203 JT') -> '7203 JT EQUITY', the shape sym_mbpipe stores.

    Upper case because that is how the sample rows read, and because a
    symbol comparison in q is exact."""
    b = (bbg or "").strip()
    return f"{b} EQUITY".upper() if b else ""


def load(path):
    """(rows, excluded).  `excluded` also carries a note naming any optional
    column the file did not have, so a silently-blank RicCode in the trace
    is explained rather than mysterious."""
    kept = []
    by_reason = {}

    def drop(reason, who):
        by_reason.setdefault(reason, []).append(who)

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        missing = [c for c in REQUIRED if c not in fields]
        if missing:
            raise ValueError(
                f"{path}: missing column(s) {', '.join(missing)}.  "
                f"This job needs {', '.join(REQUIRED)}; it reads "
                f"{', '.join(OPTIONAL)} when present and ignores the rest.")
        absent = [c for c in OPTIONAL if c not in fields]
        fidessa = next((c for c in FIDESSA_CODE if c in fields), "")
        if not fidessa:
            absent.append("FidessaCode")

        for r in reader:
            code = (r.get(fidessa) or "").strip() if fidessa else ""
            bbg = (r.get("BloombergCode") or "").strip()
            if not bbg:
                drop("no BloombergCode", code)
                continue
            ticker, ext = split_bbg(bbg)
            kept.append(Row(
                fidessa_code=code,
                ric=(r.get("RicCode") or "").strip(),
                bbg=bbg,
                ticker=ticker,
                bbg_ext=ext,
                sec_type=(r.get("Type") or "").strip(),
                bbg_sec_type=(r.get("BloombergSecurityType") or "").strip(),
                market=(r.get("FidessaMarket") or "").strip(),
                currency=(r.get("Currency") or "").strip()))

    excluded = [Excluded(reason=k, rows=v)
                for k, v in sorted(by_reason.items())]
    if absent:
        excluded.append(Excluded(
            reason=f"columns not in this file, read as blank: "
                   f"{', '.join(absent)}"))
    return kept, excluded


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("crosscode --self-test\n\nsplitting a Bloomberg code")
    check("ticker and exchange", split_bbg("005930 KP"), ("005930", "KP"))
    check("Australia, where the primary reads like a composite",
          split_bbg("BHP AU"), ("BHP", "AU"))
    check("split from the right, so a ticker with a space survives intact",
          split_bbg("SCB-R TB"), ("SCB-R", "TB"))
    check("and one with two words does too",
          split_bbg("AAA BB CC"), ("AAA BB", "CC"))
    check("no exchange code at all", split_bbg("BHP"), ("BHP", ""))
    check("nothing at all", split_bbg(""), ("", ""))
    check("surrounding whitespace is not part of either half",
          split_bbg("  BHP AU  "), ("BHP", "AU"))

    print("\nthe two shapes equity_master stores")
    check("sym_bpipe is dot-joined", bbg_dotted("7203 JT"), "7203.JT")
    check("a ticker's own space is kept - only the exchange code moves",
          bbg_dotted("SCB-R TB"), "SCB-R.TB")
    check("no exchange code, nothing to join", bbg_dotted("BHP"), "BHP")
    check("sym_mbpipe is the full name, upper case",
          bbg_full("7203 JT"), "7203 JT EQUITY")
    check("already upper case is unchanged",
          bbg_full("BHP AU"), "BHP AU EQUITY")
    check("an empty code makes no candidate at all", bbg_full(""), "")

    print("\nreading the file")
    HDR = ("#FidessaCode,RicCode,Type,BloombergCode,BloombergSecurityType,"
           "FidessaMarket,Currency\n")
    BODY = ("BHP.AU,BHP.AX,Equity,BHP AU,Equity,ASX-MAIN,AUD\n"
            "7203.JP,7203.T,Equity,7203 JT,Equity,TYO-MAIN,JPY\n"
            "7203.JE,7203.CHJ,Equity,7203 JE,Equity,JNX-MAIN,JPY\n"
            "STW.AU,STW.AX,ETF,STW AU,Equity,ASX-MAIN,AUD\n"
            "NO.XX,,Equity,,Equity,ASX-MAIN,AUD\n")

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + BODY, encoding="utf-8")
        rows, excl = load(p)

        check("every row with a BloombergCode is kept", len(rows), 4)
        check("the ticker is the code without its exchange",
              rows[0].ticker, "BHP")
        check("and the exchange is kept separately", rows[0].bbg_ext, "AU")
        check("the whole code is kept too - it is what names the output file",
              rows[1].bbg, "7203 JT")
        check("two venue rows for one Japanese name both survive here; "
              "collapsing them is universe.py's job, not this one",
              [r.bbg for r in rows if r.ticker == "7203"],
              ["7203 JT", "7203 JE"])
        check("a row with no BloombergCode is dropped",
              excl[0].reason, "no BloombergCode")
        check("and named, not merely counted", excl[0].rows, ["NO.XX"])
        check("TradingData's header has no BloombergStatus, and the reader "
              "says so rather than leaving a blank column unexplained",
              [e.reason for e in excl if "not in this file" in e.reason],
              ["columns not in this file, read as blank: BloombergStatus"])

    print("\nthe two spellings of the first column")
    LUD = ("FidessaCode,RicCode,Type,BloombergCode,BloombergStatus,"
           "FidessaMarket\n"
           "BHP.AU,BHP.AX,Equity,BHP AU,ACTV,ASX-MAIN\n")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(LUD, encoding="utf-8")
        rows, excl = load(p)
        check("LimitUpDown's spelling, with no '#', is read too - the repo's "
              "two readers disagree and only one of them can be right",
              rows[0].fidessa_code, "BHP.AU")
        check("and the rest of the row survives it", rows[0].bbg, "BHP AU")
        check("the columns THIS file lacks are the ones reported",
              [e.reason for e in excl],
              ["columns not in this file, read as blank: "
               "BloombergSecurityType, Currency"])

    print("\nthe smallest file that works")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text("BloombergCode,FidessaMarket\n7203 JT,TYO-MAIN\n",
                     encoding="utf-8")
        rows, excl = load(p)
        check("two columns are enough - everything else is a diagnostic",
              (rows[0].bbg, rows[0].market), ("7203 JT", "TYO-MAIN"))
        check("and the six absent ones are named, so a blank RicCode in a "
              "trace is explained rather than mysterious",
              "RicCode" in excl[0].reason, True)

        for bad, why in (("BloombergCode\n7203 JT\n", "FidessaMarket"),
                         ("FidessaMarket\nTYO-MAIN\n", "BloombergCode")):
            p.write_text(bad, encoding="utf-8")
            try:
                load(p)
                check(f"a file with no {why} raised", False, True)
            except ValueError as e:
                check(f"a file with no {why} is refused, naming it",
                      why in str(e), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
