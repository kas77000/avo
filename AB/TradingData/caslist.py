#!/usr/bin/env python3
"""The India closing-auction lists, one per exchange.

Optional, like the msci mapping: no file means the Segment column keeps
whatever the market rules gave it, and the run says so rather than failing.

WHITESPACE-SEPARATED, NOT COMMA.  The R job reads these with sep=comma at
:415, and the delivered files are separated by spaces - so every line
arrives as ONE field, `select` cannot find its two columns, and the read
falls into the `error = function(e){FALSE}` branch, which logs and carries
on.  The symptom is not a crash.  It is that no Indian row is ever marked
CAS.

WHAT THE R JOB DOES WITH IT, at :402-441 and :580-581:

    read the list, keep the rows with eligible_in_closing_auction == 1 and a
    non-blank isin, then set Segment = "CAS" on every row whose
    FidessaMarket is this exchange AND whose ID_ISIN is on the list.

Called once for NSI-MAIN and once for BSE-MAIN, with a different file each
time, and it overwrites whatever Segment the market rules had already put
there.

The file has a header and at least these two columns:

    isin                          the key the segment is matched on
    eligible_in_closing_auction   1 for yes

Only those two are read, and by name, so more columns or another order is
fine.

    python caslist.py --self-test
"""

from __future__ import annotations

from pathlib import Path

#  :580-581.  One call per exchange, and the market name is what ties a list
#  to the rows it may mark.
NSE_MARKET = "NSI-MAIN"
BSE_MARKET = "BSE-MAIN"

ISIN_COLUMN = "isin"
ELIGIBLE_COLUMN = "eligible_in_closing_auction"


def load(path) -> set:
    """Every ISIN eligible for the closing auction, from one exchange's list.

    A missing or unreadable file is an empty set rather than an error, which
    is what the R job's tryCatch makes of it."""
    p = Path(path)
    if not p.exists():
        return set()

    out = set()
    isin_at = eligible_at = None
    with p.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            fields = line.split()
            if not fields:
                continue
            if isin_at is None:
                lower = [f.strip().lower() for f in fields]
                if ISIN_COLUMN not in lower or ELIGIBLE_COLUMN not in lower:
                    #  Not the header yet, or not a file we understand.  A
                    #  file whose header never arrives yields nothing, which
                    #  is the same answer as no file at all.
                    continue
                isin_at = lower.index(ISIN_COLUMN)
                eligible_at = lower.index(ELIGIBLE_COLUMN)
                continue
            if len(fields) <= max(isin_at, eligible_at):
                #  R reads these with fill=TRUE; a short line is not an error.
                continue
            isin = fields[isin_at].strip()
            if isin and _is_one(fields[eligible_at]):
                out.add(isin)
    return out


def _is_one(value) -> bool:
    """eligible_in_closing_auction == 1, however the file spells it.

    A flag column arrives as 1 or 1.0 depending on what wrote it, and both
    mean the same thing.  Anything else - 0, blank, NA - does not."""
    text = (value or "").strip()
    try:
        return float(text) == 1
    except ValueError:
        return False


def load_india(nse_path="", bse_path="") -> dict:
    """{market: {isin, ...}} for the exchanges that supplied a list."""
    out = {}
    for market, path in ((NSE_MARKET, nse_path), (BSE_MARKET, bse_path)):
        if not path:
            continue
        found = load(path)
        if found:
            out[market] = found
    return out


def segment(cas, market: str, isin: str) -> str:
    """CAS when this row is on its own exchange's list, blank otherwise.

    :429-431 matches on ID_ISIN and on the Fidessa market TOGETHER, so a
    Bombay ISIN cannot mark a National Exchange row."""
    if not cas or not isin:
        return ""
    return "CAS" if isin in cas.get(market, ()) else ""


# =============================================================================
# HONG KONG  - :330-372, and only HALF of it can be done here.
#
#   the dico list  ->  NO_CAS   a file, so it is below
#   TRADING_CONDITIONS_1  ->  CAS   an intraday Bloomberg call at :357, for
#                                   HK ETFs.  equity_master has no
#                                   equivalent and qatt.cond was ruled out,
#                                   so this half stays unfilled and the run
#                                   says so.  It is the one genuinely
#                                   unavailable field in this job.
# =============================================================================

HKEX_MARKETS = ("HKG-MAIN", "HKG-GEM")

#  :339.  Warrants are left alone whatever the list says.
HKEX_EXEMPT_TYPE = "Warrant"


def load_hkex(path) -> set:
    """The BloombergCodes on the HKEX closing-auction list.

    HEADERLESS AND ONE COLUMN (:334 reads it with header=F and names the
    column StockCodes), so every line is a bare stock code - 5, 700, 941 -
    and :338 pastes " HK" onto each before comparing.  That paste is done
    here, once, so the caller compares BloombergCode against BloombergCode.

    Only the first field of a line is taken, which reads a plain list and a
    file that carries the " HK" already, and ignores anything after it."""
    p = Path(path)
    if not p.exists():
        return set()

    out = set()
    with p.open(encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            code = line.replace(",", " ").split()
            if not code:
                continue
            first = code[0].strip()
            if not first or first.lower() == "stockcodes":
                #  Headerless per :334, but a file that carries the name
                #  anyway must not turn it into a stock.
                continue
            out.add(first if first.upper().endswith(" HK")
                    else f"{first} HK")
    return out


def segment_hkex(codes, market: str, bbg: str, sec_type: str) -> str:
    """NO_CAS for a Hong Kong name that is NOT on the list.

    THE MATCH IS INVERTED, which is easy to get backwards: :337-339 marks
    the rows whose BloombergCode is `!is.element` of the list.  The list
    names what DOES have a closing auction, so everything else on HKG-MAIN
    and HKG-GEM is NO_CAS.

    Two things narrow it.  Warrants are exempt (:339).  And an empty list
    marks NOTHING - :336 skips the whole block when the read failed or the
    file was empty - which matters far more here than for India: inverted,
    a list that failed to load would otherwise mark EVERY Hong Kong name
    NO_CAS."""
    if not codes or market not in HKEX_MARKETS:
        return ""
    if (sec_type or "").strip() == HKEX_EXEMPT_TYPE:
        return ""
    return "" if (bbg or "").strip() in codes else "NO_CAS"


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("caslist --self-test")
    print("\nreading one exchange's list")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "nse.txt"
        p.write_text("isin eligible_in_closing_auction\n"
                     "INE002A01018 1\n"
                     "INE009A01021 0\n"
                     "INE467B01029 1\n", encoding="utf-8")
        check("only the eligible ISINs come back",
              load(p), {"INE002A01018", "INE467B01029"})

        p.write_text("isin,eligible_in_closing_auction\n"
                     "INE002A01018,1\n", encoding="utf-8")
        check("a COMMA file yields nothing - one field, no columns found. "
              "That is how the R job fails on a space file: not loudly, "
              "just with no row marked CAS",
              load(p), set())

        p.write_text("isin   eligible_in_closing_auction\n"
                     "INE002A01018      1\n", encoding="utf-8")
        check("runs of spaces are one separator, not empty fields",
              load(p), {"INE002A01018"})

        p.write_text("ticker isin name eligible_in_closing_auction\n"
                     "RELIANCE INE002A01018 Reliance 1\n"
                     "INFY INE009A01021 Infosys 0\n", encoding="utf-8")
        check("the two columns are found by NAME, so extra ones and any "
              "order are fine", load(p), {"INE002A01018"})

        p.write_text("ISIN Eligible_In_Closing_Auction\n"
                     "INE002A01018 1\n", encoding="utf-8")
        check("the header is matched without case",
              load(p), {"INE002A01018"})

        p.write_text("isin eligible_in_closing_auction\n"
                     "INE002A01018 1.0\n"
                     "INE009A01021 1.5\n"
                     "INE467B01029 NA\n"
                     " \n"
                     "INE111A01011\n", encoding="utf-8")
        check("1.0 is 1; 1.5, NA, a blank line and a short line are not",
              load(p), {"INE002A01018"})

        check("no file is an empty set, not a failure",
              load(Path(d) / "absent.txt"), set())

    print("\nmarking a row")
    cas = {NSE_MARKET: {"INE002A01018"}, BSE_MARKET: {"INE467B01029"}}
    check("an NSE row on the NSE list",
          segment(cas, NSE_MARKET, "INE002A01018"), "CAS")
    check("a BSE row on the BSE list",
          segment(cas, BSE_MARKET, "INE467B01029"), "CAS")
    check("the lists do not cross - a Bombay ISIN cannot mark a National "
          "Exchange row", segment(cas, NSE_MARKET, "INE467B01029"), "")
    check("a row on neither", segment(cas, NSE_MARKET, "INE999"), "")
    check("a market with no list",
          segment(cas, "TYO-MAIN", "INE002A01018"), "")
    check("no lists at all", segment({}, NSE_MARKET, "INE002A01018"), "")
    check("a row with no ISIN cannot match, and must not match everything",
          segment(cas, NSE_MARKET, ""), "")

    print("\nhong kong, where the match is inverted")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "hkex.txt"
        p.write_text("5\n700\n941\n", encoding="utf-8")
        check("a bare stock code becomes a BloombergCode, per the paste "
              "at :338", load_hkex(p), {"5 HK", "700 HK", "941 HK"})

        p.write_text("700 HK\n5\n\n  \n", encoding="utf-8")
        check("a file that already carries the HK is not given it twice",
              load_hkex(p), {"700 HK", "5 HK"})

        p.write_text("StockCodes\n700\n", encoding="utf-8")
        check("the file is headerless, so a stray header is not a stock",
              load_hkex(p), {"700 HK"})

        check("no file is an empty set", load_hkex(Path(d) / "no.txt"),
              set())

    codes = {"700 HK", "5 HK"}
    check("a Hong Kong name ON the list keeps its segment - the list is "
          "what HAS an auction",
          segment_hkex(codes, "HKG-MAIN", "700 HK", "Equity"), "")
    check("one that is NOT on it is NO_CAS, which is the inverted match",
          segment_hkex(codes, "HKG-MAIN", "1234 HK", "Equity"), "NO_CAS")
    check("GEM is marked the same way as MAIN",
          segment_hkex(codes, "HKG-GEM", "1234 HK", "Equity"), "NO_CAS")
    check("a warrant is exempt however absent it is, per :339",
          segment_hkex(codes, "HKG-MAIN", "1234 HK", "Warrant"), "")
    check("a market that is not Hong Kong is untouched",
          segment_hkex(codes, "TYO-MAIN", "7203 JT", "Equity"), "")
    check("AN EMPTY LIST MARKS NOTHING - inverted, a failed read would "
          "otherwise make every Hong Kong name NO_CAS",
          segment_hkex(set(), "HKG-MAIN", "1234 HK", "Equity"), "")

    print("\nloading both")
    with tempfile.TemporaryDirectory() as d:
        n, b = Path(d) / "n.txt", Path(d) / "b.txt"
        n.write_text("isin eligible_in_closing_auction\nINE1 1\n",
                     encoding="utf-8")
        b.write_text("isin eligible_in_closing_auction\nINE2 1\n",
                     encoding="utf-8")
        check("each exchange keeps its own", load_india(n, b),
              {NSE_MARKET: {"INE1"}, BSE_MARKET: {"INE2"}})
        check("one supplied is one loaded", load_india(n, ""),
              {NSE_MARKET: {"INE1"}})
        check("neither supplied is nothing at all", load_india("", ""), {})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
