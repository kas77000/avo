#!/usr/bin/env python3
"""Pick each stock's close: the last qatt trade carrying its market's close
condition code, equity_master's PX_LAST as the fallback.

WHAT "THE CLOSE" MEANS HERE.  config/close_conditions.csv (see its README)
names the condition codes that mark a market's closing trade - transcribed
from MarketConditionBBG.xml's closing phase, one row per Bloomberg exchange
code.  A name's close is the price of the LAST row in its day's ticks whose
condition carries one of those codes; Japan's `e` also marks the morning
close, which is why it is the LAST match and not the first.

WHY A UNION OF CODES, NOT ONE MARKET'S.  A composite name can resolve to
several CrossCode rows - Japan's primary board and an ATS, for instance -
and codes_for_sym takes every venue's close codes together, because the
day's closing trade can carry any one of them.

FALLBACK, NEVER SILENCE.  A name with no trade carrying a close code did not
necessarily sit out the day - AND a market this file has no row for is the
same situation from the other side.  Either way the fallback is
equity_master's PX_LAST, and `resolve` names the reason (no-close-codes,
no-trades, no-closing-trade) so the run's log can say why every single
fallback happened, per the README.

px_last ARRIVES ALREADY FILTERED.  The caller (extract.py) turns a null or
non-positive PX_LAST into None before calling resolve() - a zero or negative
last price is not a price - so resolve() only has to ask whether one
survived, not what shape it is in.

Consumes qattsource.shape()'s rows: {sym: [(secs|None, price_str, size_str,
cond_str, ex_str)]}.  cond is ticksfile.condition()'s cell - empty becomes
"#N/A N.A.", several codes are joined with "@" - so pick() splits on "@"
rather than assuming one code per row.

    python closes.py --self-test
"""

from __future__ import annotations

import csv
from collections import namedtuple
from pathlib import Path

Close = namedtuple("Close", "sym close source reason")


def load_conditions(path) -> dict:
    """BBGCode -> its close condition codes, in file order.

    CloseCondCodes is pipe-separated (`AUC|ACB`) because the codes
    themselves are plain text with no comma or pipe of their own; a blank
    cell or a code that is only whitespace contributes nothing."""
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("BBGCode") or "").strip()
            if not code:
                continue
            out[code] = [c.strip()
                         for c in (r.get("CloseCondCodes") or "").split("|")
                         if c.strip()]
    return out


def country_of(path) -> dict:
    """BBGCode -> Country, from the same file - for the fallback log line."""
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("BBGCode") or "").strip()
            if not code:
                continue
            out[code] = (r.get("Country") or "").strip()
    return out


def codes_for_sym(exts, conditions) -> list:
    """The union of close codes for every exchange code a composite
    resolves to, in first-seen order, deduplicated.

    `exts` is the BBG exchange codes of every CrossCode row that names this
    sym.  A market `conditions` has no row for simply contributes nothing -
    that is not an error here, it is what makes the caller's reason
    "no-close-codes" rather than a crash."""
    seen = set()
    out = []
    for ext in exts:
        for code in conditions.get(ext, []):
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def pick(rows, codes):
    """The price of the LAST row whose cond shares a code with `codes`, or
    None if none does.

    cond is qattsource.shape()'s formatted cell, so it is split on "@" -
    ticksfile.CONDITION_SEP - before matching, never compared whole."""
    wanted = set(codes)
    if not wanted:
        return None
    price = None
    for _, p, _, cond, _ in rows:
        if wanted.intersection((cond or "").split("@")):
            price = p
    return price


def resolve(sym, rows, codes, px_last) -> Close:
    """The close, and where it came from.

    A qatt hit wins outright.  Short of that, `pick` cannot tell an
    unconfigured market from a quiet one from a name with no trades at all -
    those are three different situations and the reason says which -  but
    every one of them still falls back to equity_master when px_last
    survived the caller's null/<=0 filter, and only "no-close" when nothing
    did."""
    price = pick(rows, codes)
    if price is not None:
        return Close(sym, price, "qatt", "")
    if not codes:
        reason = "no-close-codes"
    elif not rows:
        reason = "no-trades"
    else:
        reason = "no-closing-trade"
    if px_last:
        return Close(sym, px_last, "equity_master", reason)
    return Close(sym, "", "", "no-close")


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("closes --self-test\n\npicking the last close-condition trade")
    rows = [(32400, "100", "10", "#N/A N.A.", "T"), (54000, "101", "5", "CA", "T"),
            (54001, "102", "1", "#N/A N.A.", "T"), (54002, "103", "9", "T@CA", "T")]
    check("the last trade carrying a close code", pick(rows, ["CA"]), "103")
    check("no code in the list", pick(rows, ["GC"]), None)
    check("no codes at all is not a match against anything",
          pick(rows, []), None)
    check("no rows at all is nothing to pick from", pick([], ["CA"]), None)

    print("\nresolving the close, with the fallback and its reason")
    check("a qatt close", resolve("A", rows, ["CA"], "99"),
          Close("A", "103", "qatt", ""))
    check("traded, no close trade -> equity_master",
          resolve("A", rows, ["GC"], "99"),
          Close("A", "99", "equity_master", "no-closing-trade"))
    check("no trades -> equity_master", resolve("A", [], ["CA"], "99").reason,
          "no-trades")
    check("market not in the file", resolve("A", rows, [], "99").reason,
          "no-close-codes")
    check("nothing at all", resolve("A", [], ["CA"], None),
          Close("A", "", "", "no-close"))
    check("px_last already filtered to None by the caller behaves the same "
          "whichever reason produced it",
          resolve("A", rows, [], None), Close("A", "", "", "no-close"))

    print("\nthe union of a composite's venues")
    check("union of a composite's venues",
          codes_for_sym(["IS", "IB"], {"IS": ["AUC", "ACB"],
                                       "IB": ["AUC", "ACB"]}),
          ["AUC", "ACB"])
    check("order is first-seen, not sorted",
          codes_for_sym(["IB", "IS"], {"IS": ["AUC", "ACB"],
                                       "IB": ["ACB", "AUC"]}),
          ["ACB", "AUC"])
    check("a venue this file has no row for contributes nothing",
          codes_for_sym(["ZZ"], {"IS": ["AUC"]}), [])
    check("one venue, one list", codes_for_sym(["IS"], {"IS": ["AUC", "ACB"]}),
          ["AUC", "ACB"])

    print("\nreading close_conditions.csv")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "close_conditions.csv"
        p.write_text(
            "BBGCode,Country,Venue,CloseCondCodes\n"
            "IS,India,NSI-MAIN,AUC|ACB\n"
            "JT,Japan,TYO-MAIN,e|ES\n"
            "AT,Australia,ASX-MAIN,CA\n"
            "NA,Namibia,NAM-MAIN,NA\n",
            encoding="utf-8")
        conditions = load_conditions(p)
        check("split on the pipe", conditions["IS"], ["AUC", "ACB"])
        check("a single code is a one-element list", conditions["AT"], ["CA"])
        check("file order is kept, not sorted",
              list(conditions), ["IS", "JT", "AT", "NA"])
        check("csv reads plain text - an NA-LOOKING code is neither dropped "
              "nor turned into a null", conditions["NA"], ["NA"])
        countries = country_of(p)
        check("BBGCode to Country", countries["IS"], "India")
        check("an NA-looking BBGCode is kept as text, not read as a null",
              "NA" in countries, True)
        check("and its Country survives the same way",
              countries["NA"], "Namibia")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
