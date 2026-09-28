#!/usr/bin/env python3
"""Read CrossCode.csv.  It is the security master and it drives the row set.

Seven columns, exactly the ones the R job selects at :526.  Unlike the
LimitUpDown reader this one filters nothing on security type or venue -
TradingData publishes every instrument in the crosscode, warrants included.
The only rows dropped are those with no BloombergCode, because there is
nothing to join them to, and they are reported rather than vanished.

Note the leading '#' on the first header cell.  It is part of the column
name in the real file and in the output, not a comment.

    python crosscode.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = ("#FidessaCode", "RicCode", "Type", "BloombergCode",
           "BloombergSecurityType", "FidessaMarket", "Currency")


@dataclass(frozen=True)
class Row:
    fidessa_code: str
    ric: str
    bbg: str
    ticker: str
    bbg_ext: str
    sec_type: str
    bbg_sec_type: str
    market: str
    currency: str
    is_reit: bool


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


def load(path):
    kept = []
    by_reason = {}

    def drop(reason, who):
        by_reason.setdefault(reason, []).append(who)

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"{path}: missing column(s) {', '.join(missing)}.  "
                f"Expected {', '.join(COLUMNS)}")

        for r in reader:
            code = (r.get("#FidessaCode") or "").strip()
            bbg = (r.get("BloombergCode") or "").strip()
            if not bbg:
                drop("no BloombergCode", code)
                continue
            ticker, ext = split_bbg(bbg)
            bbg_sec_type = (r.get("BloombergSecurityType") or "").strip()
            kept.append(Row(
                fidessa_code=code,
                ric=(r.get("RicCode") or "").strip(),
                bbg=bbg,
                ticker=ticker,
                bbg_ext=ext,
                sec_type=(r.get("Type") or "").strip(),
                bbg_sec_type=bbg_sec_type,
                market=(r.get("FidessaMarket") or "").strip(),
                currency=(r.get("Currency") or "").strip(),
                is_reit=bbg_sec_type == "REIT"))

    return kept, [Excluded(reason=k, rows=v)
                  for k, v in sorted(by_reason.items())]


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    HDR = ("#FidessaCode,RicCode,Type,BloombergCode,BloombergSecurityType,"
           "FidessaMarket,Currency\n")
    BODY = ("BHP.AU,BHP.AX,Equity,BHP AU,Equity,ASX-MAIN,AUD\n"
            "LINK.HK,823.HK,Equity,823 HK,REIT,HKG-MAIN,HKD\n"
            "STW.AU,STW.AX,ETF,STW AU,Equity,ASX-MAIN,AUD\n"
            "NO.XX,,Equity,,Equity,ASX-MAIN,AUD\n")

    print("crosscode --self-test\n\nsplitting the bloomberg code")
    check("ticker and exchange", split_bbg("005930 KP"), ("005930", "KP"))
    check("no space at all", split_bbg("ABC"), ("ABC", ""))
    check("nothing at all", split_bbg(""), ("", ""))

    print("\nreading")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + BODY, encoding="utf-8")
        rows, excl = load(p)

        check("three usable rows", len(rows), 3)
        check("the hash on the first header cell is not part of the value",
              rows[0].fidessa_code, "BHP.AU")
        check("the ticker is the bbg code without its exchange",
              rows[0].ticker, "BHP")
        check("and the exchange is kept separately", rows[0].bbg_ext, "AU")
        check("currency is carried for the FX join", rows[0].currency, "AUD")
        check("a REIT is flagged, per :528", rows[1].is_reit, True)
        check("an ETF whose BloombergSecurityType is Equity is not a REIT",
              rows[2].is_reit, False)
        check("Type and BloombergSecurityType are both kept",
              (rows[2].sec_type, rows[2].bbg_sec_type), ("ETF", "Equity"))

        reasons = {e.reason: e.rows for e in excl}
        check("a row with no bloomberg code cannot be joined, and is reported",
              reasons["no BloombergCode"], ["NO.XX"])

    print("\na missing column is a hard error, not a silent blank")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.csv"
        p.write_text("RicCode,Type\nA.AX,Equity\n", encoding="utf-8")
        try:
            load(p)
            check("raised", False, True)
        except ValueError as exc:
            check("names what is missing", "#FidessaCode" in str(exc), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
