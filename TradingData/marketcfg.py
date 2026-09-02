#!/usr/bin/env python3
"""Per-market configuration, as a CSV the desk can edit in Excel.

The R job hardcodes two market lists at :302-320.  They are the kind of thing
that changes without a code release, so they live in config/markets.csv here.
The same file carries the Bloomberg composite code used as a fallback when
building an equity_master sym.

    python marketcfg.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

HKG = ("HKG-MAIN", "HKG-GEM")


@dataclass(frozen=True)
class Market:
    fidessa_market: str
    bbg_composite: str
    no_short_sell: str
    respect_short_sell: str


def load(path):
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("FidessaMarket") or "").strip()
            if not key:
                continue
            out[key] = Market(
                fidessa_market=key,
                bbg_composite=(r.get("BBGComposite") or "").strip(),
                no_short_sell=(r.get("NoShortSell") or "FALSE").strip(),
                respect_short_sell=(r.get("RespectShortSellPrice")
                                    or "").strip())
    return out


def no_short_sell(market: str, markets) -> str:
    m = markets.get(market)
    return m.no_short_sell if m else "FALSE"


def respect_short_sell(market: str, sec_type: str, is_reit: bool,
                       markets) -> str:
    """:302-312.  The base value is per-market; then Hong Kong ETFs that are
    not REITs are pulled back to FALSE."""
    m = markets.get(market)
    base = m.respect_short_sell if m else ""
    if sec_type == "ETF" and market in HKG and not is_reit:
        return "FALSE"
    return base


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    here = Path(__file__).resolve().parent
    M = load(here / "config" / "markets.csv")

    print("marketcfg --self-test\n\nthe shipped config")
    check("Korea's two boards share the KS composite",
          (M["KSC-MAIN"].bbg_composite, M["KOE-MAIN"].bbg_composite),
          ("KS", "KS"))
    check("every China board composites to CH",
          {M[k].bbg_composite for k in
           ("SHA-MAIN", "SHH-MAIN", "SHZ-MAIN", "SSC-MAIN",
            "SZA-MAIN", "SZC-MAIN")},
          {"CH"})

    print("\nNoShortSell, per :314-320")
    check("China and India cannot short",
          sorted(k for k, v in M.items() if v.no_short_sell == "TRUE"),
          ["BSE-MAIN", "NSI-MAIN", "SHA-MAIN", "SHH-MAIN", "SHZ-MAIN",
           "SSC-MAIN", "SZA-MAIN", "SZC-MAIN"])
    check("an unconfigured market defaults to FALSE",
          no_short_sell("XXX-MAIN", M), "FALSE")

    print("\nRespectShortSellPrice, per :302-312")
    check("Hong Kong equities respect it",
          respect_short_sell("HKG-MAIN", "Equity", False, M), "TRUE")
    check("a Hong Kong ETF that is not a REIT does not",
          respect_short_sell("HKG-MAIN", "ETF", False, M), "FALSE")
    check("a Hong Kong ETF that IS a REIT still does",
          respect_short_sell("HKG-MAIN", "ETF", True, M), "TRUE")
    check("the ETF carve-out is Hong Kong only",
          respect_short_sell("TYO-MAIN", "ETF", False, M), "TRUE")
    check("elsewhere the R job leaves NA, which writes blank",
          respect_short_sell("ASX-MAIN", "Equity", False, M), "")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
