#!/usr/bin/env python3
"""Write the synthetic fixture the Nova R jobs test against.

    tests/fixture/phase1-20260925.zip   what AB's extract.py would export
    tests/fixture/CrossCode.csv         Nova's own copy of the crosscode
    tests/fixture/missing-members.zip   the manifest alone, to be refused

Every number is invented.  The trade date is Friday 2026-09-25, and every
time in ticks.csv and quote_only.csv is kdb's clock, China Standard Time.

The names, and what each one is for:

    7203 JT / 7203 JE   one sym, 7203.JP, written as `7203 JP`.  A morning
                        close and a day close both carry `e`; the close is
                        the LAST of them.
    005930 KP           Korea, closed by a GC print.
    299990 KP           a Korean ETF whose LONG_COMP_NAME carries 2X.
    123450 KQ           Korea, a close but no ladder.
    AIA NZ              prints, none of them CA: the close falls back to
                        equity_master (no-closing-trade).
    8888 HK             no prints, a quote: one quote-only line.
    8889 HK             nothing at all: NoTradingDay, and no close anywhere.

The CrossCode also carries a basket and a row with no BloombergCode, which
every job drops.

    python make_fixture.py
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixture"
DATE = "2026-09-25"
ZIP_NAME = "phase1-20260925.zip"

#  A fixed stamp on every member, so rewriting the zip changes nothing git
#  can see unless the content changed.
STAMP = (2026, 9, 25, 19, 0, 0)

NA = "#N/A N.A."

CROSSCODE = [
    ["#FidessaCode", "RicCode", "Type", "BloombergCode",
     "BloombergSecurityType", "FidessaMarket", "Currency", "BloombergStatus",
     "Mnemo", "VenueList", "BSEBloombergCode", "BSERic"],
    ["7203.TYO", "7203.T", "Equity", "7203 JT", "Common Stock", "TYO-MAIN",
     "JPY", "ACTV", "JP7203", "", "", ""],
    ["7203.JNX", "7203.JNX", "Equity", "7203 JE", "Common Stock", "JNX-MAIN",
     "JPY", "ACTV", "JP7203", "", "", ""],
    ["005930.KSC", "005930.KS", "Equity", "005930 KP", "Common Stock",
     "KSC-MAIN", "KRW", "ACTV", "KR005930", "", "", ""],
    ["299990.KSC", "299990.KS", "ETF", "299990 KP", "ETP", "KSC-MAIN",
     "KRW", "ACTV", "KR299990", "", "", ""],
    ["123450.KOE", "123450.KQ", "Equity", "123450 KQ", "Common Stock",
     "KOE-MAIN", "KRW", "ACTV", "KR123450", "", "", ""],
    ["AIA.NZX", "AIA.NZ", "Equity", "AIA NZ", "Common Stock", "NZX-MAIN",
     "NZD", "ACTV", "NZAIA", "", "", ""],
    ["8888.HKG", "8888.HK", "Equity", "8888 HK", "Common Stock", "HKG-MAIN",
     "HKD", "ACTV", "HK8888", "", "", ""],
    ["8889.HKG", "8889.HK", "Equity", "8889 HK", "Common Stock", "HKG-MAIN",
     "HKD", "ACTV", "HK8889", "", "", ""],
    ["BSKT.HKG", "", "Basket", "BSKT HK", "", "HKG-MAIN", "HKD", "", "", "",
     "", ""],
    ["NOBBG.HKG", "NOBBG.HK", "Equity", "", "", "HKG-MAIN", "HKD", "ACTV",
     "", "", "", ""],
]

MASTER = [
    ["BloombergCode", "sym", "EQY_PRIM_EXCH_SHRT", "COMPOSITE_EXCH_CODE",
     "ID_MIC_PRIM_EXCH"],
    ["7203 JT", "7203.JP", "JT", "JP", "XTKS"],
    ["7203 JE", "7203.JP", "JT", "JP", "XTKS"],
    ["005930 KP", "005930.KS", "KP", "KS", "XKRX"],
    ["299990 KP", "299990.KS", "KP", "KS", "XKRX"],
    ["123450 KQ", "123450.KS", "KQ", "KS", "XKOS"],
    ["AIA NZ", "AIA.NZ", "NZ", "NZ", "XNZE"],
    ["8888 HK", "8888.HK", "HK", "HK", "XHKG"],
    ["8889 HK", "8889.HK", "HK", "HK", "XHKG"],
]

TICKS = [
    ["sym", "time", "price", "size", "cond", "ex"],
    #  Tokyo, 09:00-15:30 JST, which is 08:00-14:30 here.
    ["7203.JP", "08:00:00", "2850", "412300", "O", "T"],
    ["7203.JP", "08:00:01", "2851.5", "1200", NA, "T"],
    ["7203.JP", "09:15:42", "2862", "300", NA, "T"],
    ["7203.JP", "10:30:00", "2858", "98000", "e", "T"],
    ["7203.JP", "11:30:00", "2860", "51000", NA, "T"],
    ["7203.JP", "14:29:59", "2874", "1500", "R@S", "T"],
    ["7203.JP", "14:30:00", "2876", "1203400", "e", "T"],
    #  Seoul, the same hours.
    ["005930.KS", "08:00:00", "71200", "250000", NA, "K"],
    ["005930.KS", "11:02:17", "71500", "830", NA, "K"],
    ["005930.KS", "14:30:00", "71800", "2100000", "GC", "K"],
    ["299990.KS", "08:00:00", "15230", "1000", NA, "K"],
    ["299990.KS", "14:30:00", "15310", "52000", "GC", "K"],
    ["123450.KS", "08:00:05", "8390", "4000", NA, "Q"],
    ["123450.KS", "14:30:00", "8420", "61000", "GC", "Q"],
    #  Auckland, NZST until the 27th: 10:00-16:45 is 06:00-12:45 here.
    #  No CA print, so the close is equity_master's.
    ["AIA.NZ", "06:00:00", "6.12", "15000", NA, "N"],
    ["AIA.NZ", "09:31:10", "6.15", "2000", NA, "N"],
    ["AIA.NZ", "12:44:58", "6.14", "800", "XT", "N"],
]

QUOTE_ONLY = [
    ["sym", "time", "bid", "ask", "cond"],
    ["8888.HK", "16:08:02", "3.41", "3.43", "CA"],
]

CLOSES = [
    ["sym", "close", "source", "reason"],
    ["7203.JP", "2876", "qatt", ""],
    ["005930.KS", "71800", "qatt", ""],
    ["299990.KS", "15310", "qatt", ""],
    ["123450.KS", "8420", "qatt", ""],
    ["AIA.NZ", "6.13", "equity_master", "no-closing-trade"],
    ["8888.HK", "3.4", "equity_master", "no-trades"],
    ["8889.HK", "", "", "no-close"],
]

EQUITY = [
    ["BloombergCode", "sym", "PX_LAST", "EQY_BETA", "volatility",
     "REL_INDEX", "CUR_MKT_CAP", "fx_last", "ID_ISIN", "INDUSTRY_SECTOR",
     "LONG_COMP_NAME"],
    ["7203 JT", "7203.JP", "2871", "1.05", "0.24", "TPX", "46500000",
     "0.0068", "JP00FIXTURE1", "Consumer, Cyclical", "TOYOTA MOTOR CORP"],
    ["7203 JE", "7203.JP", "2871", "1.05", "0.24", "TPX", "46500000",
     "0.0068", "JP00FIXTURE1", "Consumer, Cyclical", "TOYOTA MOTOR CORP"],
    ["005930 KP", "005930.KS", "71300", "1.12", "0.28", "KOSPI",
     "428000000", "0.00072", "KR00FIXTURE2", "Technology",
     "SAMSUNG ELECTRONICS CO LTD"],
    ["299990 KP", "299990.KS", "15250", "1.98", "0.41", "KOSPI", "310000",
     "0.00072", "KR00FIXTURE3", "Funds", "FIXTURE KOSPI200 2X ETF"],
    ["123450 KQ", "123450.KS", "8400", "0.87", "0.35", "KOSDAQ", "920000",
     "0.00072", "KR00FIXTURE4", "Industrial", "FIXTURE KOSDAQ HOLDINGS"],
    ["AIA NZ", "AIA.NZ", "6.13", "0.71", "0.19", "NZSE50FG", "10400",
     "0.58", "NZ00FIXTURE5", "Industrial", "AUCKLAND INTL AIRPORT LTD"],
    ["8888 HK", "8888.HK", "3.4", "0", "0.33", "HSCI", "2100", "0.128",
     "HK00FIXTURE6", "Financial", "FIXTURE HK QUOTE LTD"],
    ["8889 HK", "8889.HK", "", "0.5", "", "HSCI", "850", "0.128",
     "HK00FIXTURE7", "Consumer, Non-cyclical", "FIXTURE HK SUSPENDED LTD"],
]

#  Raw kdb rows: each price is an EXCLUSIVE upper bound of its band, and the
#  last bound stands in for "everything above".
JP_LADDER = [("1000", "0.1"), ("3000", "0.5"), ("10000", "1"),
             ("30000", "5"), ("100000", "10"), ("300000", "50"),
             ("1000000", "100"), ("999999999", "1000")]
KR_LADDER = [("2000", "1"), ("5000", "5"), ("20000", "10"), ("50000", "50"),
             ("200000", "100"), ("500000", "500"), ("999999999", "1000")]
KR_ETF_LADDER = [("2000", "1"), ("999999999", "5")]

LADDERS = [["BloombergCode", "sym", "price", "ticksize"]]
for code, sym, ladder in (("7203 JT", "7203.JP", JP_LADDER),
                          ("7203 JE", "7203.JP", JP_LADDER),
                          ("005930 KP", "005930.KS", KR_LADDER),
                          ("299990 KP", "299990.KS", KR_ETF_LADDER)):
    LADDERS += [[code, sym, p, t] for p, t in ladder]


def _count(rows, pred=lambda r: True):
    return sum(1 for r in rows[1:] if pred(r))


MANIFEST = [
    ["key", "value"],
    ["date", DATE],
    ["source", "hdb"],
    ["equity_master date", DATE],
    ["time column", "tradeTime"],
    ["kdb timezone", "China Standard Time"],
    ["syms asked", str(_count(CLOSES))],
    ["prints", str(_count(TICKS))],
    ["syms with prints", str(len({r[0] for r in TICKS[1:]}))],
    ["quote only", str(_count(QUOTE_ONLY))],
    ["closes from qatt", str(_count(CLOSES, lambda r: r[2] == "qatt"))],
    ["closes from equity_master",
     str(_count(CLOSES, lambda r: r[2] == "equity_master"))],
    ["no close", str(_count(CLOSES, lambda r: r[1] == ""))],
    ["exported at", "2026-09-25 19:00:00"],
]

MEMBERS = {
    "manifest.csv": MANIFEST,
    "master.csv": MASTER,
    "ticks.csv": TICKS,
    "closes.csv": CLOSES,
    "quote_only.csv": QUOTE_ONLY,
    "equity.csv": EQUITY,
    "ladders.csv": LADDERS,
}


def _csv(rows) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(HERE / ZIP_NAME, "w") as z:
        for name, rows in MEMBERS.items():
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, _csv(rows))
    #  The manifest alone, for the check that a short zip is refused.
    with zipfile.ZipFile(HERE / "missing-members.zip", "w") as z:
        info = zipfile.ZipInfo("manifest.csv", date_time=STAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, _csv(MANIFEST))
    (HERE / "CrossCode.csv").write_bytes(_csv(CROSSCODE))
    print(f"wrote {HERE / ZIP_NAME} ({len(MEMBERS)} members) and "
          f"{HERE / 'CrossCode.csv'}")


if __name__ == "__main__":
    main()
