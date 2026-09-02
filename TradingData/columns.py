#!/usr/bin/env python3
"""The market rules, as pure functions.

Every function here takes values and returns a value.  No file is read, no
socket is opened, nothing is logged.  That is deliberate: the rules are the
part most likely to be wrong, and they are the part that must be testable on
a laptop with no kdb, no Bloomberg licence and no shares outstanding.

    python columns.py --self-test
"""

from __future__ import annotations

from decimal import Decimal

SEGMENT_DEFAULT = "Default"

# :165-168.  Note the R job's boundaries are exclusive going up, so a cap of
# exactly 300m is MICRO and exactly 2bn is SMALL.
_CAPI = ((Decimal("10000000000"), "BIG"),
         (Decimal("2000000000"), "MID"),
         (Decimal("300000000"), "SMALL"))

# :381-400.  ETFs are forced to A-B regardless of ticker.
_ASX = (("B", "A-B"), ("F", "C-F"), ("M", "G-M"), ("R", "N-R"), ("Z", "S-Z"))

_CN_ETF_MARKETS = ("SHA-MAIN", "SHH-MAIN", "SSC-MAIN")

# :265.  These two share a RIC extension with two possible ICB values and
# cannot be told apart, so they never take a propagated value.
_ICB_SKIP_MARKETS = ("SZA-MAIN", "SZC-MAIN")
_ICB_SKIP_RICS = ("NoRIC", "TWO")


def capi_bucket(market_cap) -> str:
    if market_cap is None:
        return ""
    for floor, name in _CAPI:
        if market_cap > floor:
            return name
    return "MICRO"


def sector(gics: str, industry: str) -> str:
    """:295 prefers GICS_SECTOR_NAME and falls back to INDUSTRY_SECTOR.

    equity_master has no GICS_SECTOR_NAME, so in practice every row takes the
    fallback.  The comma substitution is :89 - a comma would break the
    unquoted CSV the R job writes."""
    value = (gics or "").strip() or (industry or "").strip()
    return value.replace(",", "|")


def segment_asx(ticker: str, sec_type: str) -> str:
    if sec_type == "ETF":
        return "A-B"
    first = (ticker or "").strip()[:1].upper()
    if not ("A" <= first <= "Z"):
        return ""
    for ceiling, name in _ASX:
        if first <= ceiling:
            return name
    return ""


def segment_cn(market: str, sec_type: str):
    """None means 'this rule does not apply', which is not the same as ''."""
    if sec_type == "ETF" and market in _CN_ETF_MARKETS:
        return "NO_CAS"
    return None


def ext_ric(ric: str) -> str:
    return (ric or "").rsplit(".", 1)[-1]


def ext_bbg(bbg: str) -> str:
    return (bbg or "").rsplit(" ", 1)[-1]


def propagate_icb(records) -> list:
    """:263-269.  Names sharing (bbg extension, ric extension, market) share an
    index, so a name with no ICBIndex can borrow one from its group - but only
    where the group agrees on a single value."""
    groups = {}
    for r in records:
        icb = (r.get("icb") or "").strip()
        if not icb:
            continue
        if ext_ric(r.get("ric", "")) in _ICB_SKIP_RICS:
            continue
        if r.get("market") in _ICB_SKIP_MARKETS:
            continue
        key = (ext_bbg(r.get("bbg", "")), ext_ric(r.get("ric", "")),
               r.get("market"))
        groups.setdefault(key, set()).add(icb)

    out = []
    for r in records:
        icb = (r.get("icb") or "").strip()
        if icb:
            out.append(icb)
            continue
        if (ext_ric(r.get("ric", "")) in _ICB_SKIP_RICS
                or r.get("market") in _ICB_SKIP_MARKETS):
            out.append("")
            continue
        key = (ext_bbg(r.get("bbg", "")), ext_ric(r.get("ric", "")),
               r.get("market"))
        found = groups.get(key, set())
        out.append(next(iter(found)) if len(found) == 1 else "")
    return out


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("columns --self-test\n\ncapi buckets, at the boundaries")
    check("exactly 300m is MICRO", capi_bucket(Decimal("300000000")), "MICRO")
    check("a penny over is SMALL", capi_bucket(Decimal("300000001")), "SMALL")
    check("exactly 2bn is still SMALL", capi_bucket(Decimal("2000000000")), "SMALL")
    check("over 2bn is MID", capi_bucket(Decimal("2000000001")), "MID")
    check("exactly 10bn is still MID", capi_bucket(Decimal("10000000000")), "MID")
    check("over 10bn is BIG", capi_bucket(Decimal("10000000001")), "BIG")
    check("zero is MICRO, as the R job's na-to-0 makes it",
          capi_bucket(Decimal("0")), "MICRO")
    check("no market cap means no bucket", capi_bucket(None), "")

    print("\nsector prefers GICS and falls back")
    check("GICS wins", sector("Financials", "Banks"), "Financials")
    check("blank GICS falls back", sector("", "Banks"), "Banks")
    check("commas become pipes, per :89", sector("", "Oil, Gas"), "Oil| Gas")
    check("neither means blank", sector("", ""), "")

    print("\nASX segments")
    check("A goes in A-B", segment_asx("ANZ", "Equity"), "A-B")
    check("B goes in A-B", segment_asx("BHP", "Equity"), "A-B")
    check("C goes in C-F", segment_asx("CBA", "Equity"), "C-F")
    check("G goes in G-M", segment_asx("GMG", "Equity"), "G-M")
    check("N goes in N-R", segment_asx("NAB", "Equity"), "N-R")
    check("S goes in S-Z", segment_asx("STO", "Equity"), "S-Z")
    check("lowercase is still bucketed", segment_asx("bhp", "Equity"), "A-B")
    check("a digit falls outside the letters", segment_asx("10X", "Equity"), "")
    check("an ETF is forced to A-B whatever its ticker",
          segment_asx("STW", "ETF"), "A-B")

    print("\nChina segments")
    check("an ETF on SSC is NO_CAS", segment_cn("SSC-MAIN", "ETF"), "NO_CAS")
    check("and on SHA", segment_cn("SHA-MAIN", "ETF"), "NO_CAS")
    check("and on SHH", segment_cn("SHH-MAIN", "ETF"), "NO_CAS")
    check("an equity is untouched", segment_cn("SHA-MAIN", "Equity"), None)
    check("an ETF elsewhere is untouched", segment_cn("SZA-MAIN", "ETF"), None)

    print("\nsplitting codes for the ICB propagation")
    check("the RIC extension is after the last dot",
          ext_ric("600001.SS"), "SS")
    check("no dot means the whole thing", ext_ric("NoRIC"), "NoRIC")
    check("the BBG extension is after the last space",
          ext_bbg("600001 CG"), "CG")
    check("no space means the whole thing", ext_bbg("ABC"), "ABC")

    print("\nICB propagation, per :263-269")
    recs = [
        {"ric": "1.SS", "bbg": "1 CG", "market": "SHA-MAIN", "icb": "SHCOMP"},
        {"ric": "2.SS", "bbg": "2 CG", "market": "SHA-MAIN", "icb": ""},
        {"ric": "3.HK", "bbg": "3 HK", "market": "HKG-MAIN", "icb": ""},
    ]
    check("a blank row takes its group's value",
          propagate_icb(recs), ["SHCOMP", "SHCOMP", ""])

    recs = [
        {"ric": "1.SS", "bbg": "1 CG", "market": "SHA-MAIN", "icb": "A"},
        {"ric": "2.SS", "bbg": "2 CG", "market": "SHA-MAIN", "icb": "B"},
        {"ric": "3.SS", "bbg": "3 CG", "market": "SHA-MAIN", "icb": ""},
    ]
    check("an ambiguous group propagates nothing",
          propagate_icb(recs), ["A", "B", ""])

    recs = [
        {"ric": "1.SZ", "bbg": "1 C2", "market": "SZC-MAIN", "icb": "X"},
        {"ric": "2.SZ", "bbg": "2 C2", "market": "SZC-MAIN", "icb": ""},
    ]
    check("SZC-MAIN is excluded, per the :264 comment",
          propagate_icb(recs), ["X", ""])

    recs = [
        {"ric": "NoRIC", "bbg": "1 HK", "market": "HKG-MAIN", "icb": "Y"},
        {"ric": "NoRIC", "bbg": "2 HK", "market": "HKG-MAIN", "icb": ""},
    ]
    check("NoRIC is excluded too", propagate_icb(recs), ["Y", ""])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
