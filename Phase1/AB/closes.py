#!/usr/bin/env python3
"""Pick each stock's close: the last qatt trade carrying its market's close
condition code; else its last traded price; else equity_master's PX_LAST.

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

THE ORDER (resolve):
  1. the last print carrying a close code        source qatt, reason ""
  2. else, if it traded that day, its LAST TRADED PRICE: the last print by
     time (last_trade)                           source qatt, reason
                                                 last-trade
  3. else - no print: the market was shut, or the name suspended -
     equity_master's PX_LAST                     source equity_master,
                                                 reason no-trades
     (or none-before-cutoff: it traded, but only after its market's
     LastTradeBefore - Korea's after-market - which bounds step 2 only)
  4. else no close at all                        sym,,,no-close
A market with no close codes (not in this file, or a blank CloseCondCodes)
simply never reaches 1: a name there that traded closes at its last trade,
reason last-trade like any other - there is no separate reason for it; the
extract says once per market that it has no codes.  A day's own last trade
beats equity_master's PX_LAST, which may be yesterday's.

px_last ARRIVES ALREADY FILTERED.  The caller (extract.py) turns a null or
non-positive PX_LAST into None before calling resolve() - a zero or negative
last price is not a price - so resolve() only has to ask whether one
survived, not what shape it is in.

Consumes qattsource.shape()'s rows: {sym: [(secs|None, price_str, size_str,
cond_str, ex_str)]} - condensed lines, one per second, price, cond and ex,
in time order (see pick for two in one second).  cond is ticksfile.condition()'s cell - empty becomes
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
    that is not an error here: with no codes at all, resolve() goes
    straight to the last trade."""
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
    ticksfile.CONDITION_SEP - before matching, never compared whole.

    LAST IS STILL LATEST.  The rows are condensed lines in the order
    qattsource.ticks_q's `by` sorts them: second, then price, cond, ex.  So
    the last match is in the day's last second with a close code - and if
    two such lines share that second, the one sorted last, the HIGHER
    price, is taken."""
    wanted = set(codes)
    if not wanted:
        return None
    price = None
    for _, p, _, cond, _ in rows:
        if wanted.intersection((cond or "").split("@")):
            price = p
    return price


def load_cutoffs(path) -> dict:
    """BBGCode -> LastTradeBefore, for the rows that set one: a time in
    HKT (HH:MM or HH:MM:SS) after which a print is not a last trade -
    Korea's after-market.  Blank, or a file without the column: none."""
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("BBGCode") or "").strip()
            cut = (r.get("LastTradeBefore") or "").strip()
            if code and cut:
                out[code] = cut
    return out


def last_trade(rows, cut=None, all_null=None):
    """The last traded price: the price of the last row that has a time -
    with condensed rows, the last line of the day's last second - and, with
    a `cut` (seconds of day, in the rows' own clock), the last one at or
    before it.  A row with no time is never "the last" (kdb sorts a null
    time first, and it says nothing about when), nor before a cutoff,
    unless every row lacks one: then the last row.  `all_null` says so when
    `rows` is not the sym's whole day (the fast path's cut rows).  None for
    no rows, or when every timed print is after the cut."""
    timed = [r for r in rows if r[0] is not None
             and (cut is None or r[0] <= cut)]
    if timed:
        return timed[-1][1]
    if all_null is None:
        all_null = all(r[0] is None for r in rows)
    return rows[-1][1] if rows and all_null else None


def resolve(sym, rows, codes, px_last, cut=None, ltp_rows=None) -> Close:
    """The close, and where it came from - the module docstring's order: a
    closing print, the last trade, PX_LAST, nothing.

    `cut` is the market's LastTradeBefore, in the rows' clock: it limits
    the last trade only, never the closing print.  A sym that traded, but
    only after it, takes PX_LAST with the reason none-before-cutoff.
    `ltp_rows` are the rows the last trade is taken from, when they are not
    `rows` - the fast path asks kdb for them apart."""
    price = pick(rows, codes)
    if price is not None:
        return Close(sym, price, "qatt", "")
    price = last_trade(rows if ltp_rows is None else ltp_rows, cut,
                       all(r[0] is None for r in rows))
    if price is not None:
        return Close(sym, price, "qatt", "last-trade")
    reason = "none-before-cutoff" if rows else "no-trades"
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
    check("two condensed close lines in the day's last second: the one the "
          "`by` sorts last, the higher price",
          pick(rows + [(54002, "104", "3", "CA", "T")], ["CA"]), "104")

    print("\nthe last traded price")
    check("the last line by time", last_trade(rows), "103")
    check("with two lines in the last second, the last line of it",
          last_trade(rows + [(54002, "104", "3", "", "T")]), "104")
    check("a row with no time is never the last print",
          last_trade(rows + [(None, "999", "1", "", "T")]), "103")
    check("even when the null time sorts first, as kdb puts it",
          last_trade([(None, "999", "1", "", "T")] + rows), "103")
    check("but a row with no time is used when it is the only kind",
          last_trade([(None, "7", "1", "", "T"), (None, "8", "1", "", "T")]),
          "8")
    check("no rows, no last trade", last_trade([]), None)

    print("\nresolving the close, in order")
    check("1. a closing print", resolve("A", rows, ["CA"], "99"),
          Close("A", "103", "qatt", ""))
    check("2. traded, no closing print: the last traded price from qatt, "
          "not PX_LAST", resolve("A", rows, ["GC"], "99"),
          Close("A", "103", "qatt", "last-trade"))
    check("2. a market with no close codes that traded: the last trade too",
          resolve("A", rows, [], "99"), Close("A", "103", "qatt",
                                              "last-trade"))
    check("2. and it needs no PX_LAST", resolve("A", rows, ["GC"], None),
          Close("A", "103", "qatt", "last-trade"))
    check("3. no prints at all: equity_master's PX_LAST",
          resolve("A", [], ["CA"], "99"),
          Close("A", "99", "equity_master", "no-trades"))
    check("3. whether or not the market has close codes",
          resolve("A", [], [], "99"),
          Close("A", "99", "equity_master", "no-trades"))
    check("4. nothing at all", resolve("A", [], ["CA"], None),
          Close("A", "", "", "no-close"))

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

    print("\nthe last trade before a cutoff")
    #  A cutoff of 54000 (15:00:00 in kdb's clock).
    day = [(32400, "100", "1", "#N/A N.A.", "K"),
           (53990, "101", "1", "#N/A N.A.", "K"),
           (53990, "102", "1", "X", "K"),       # the last second before
           (54000, "103", "1", "#N/A N.A.", "K"),   # at the cutoff: counts
           (55000, "150", "1", "#N/A N.A.", "K")]   # after-market
    check("the last print at or before the cutoff, inclusive",
          last_trade(day, 54000), "103")
    check("one second earlier, the last line of the last second before it",
          last_trade(day, 53999), "102")
    check("no cutoff: the day's last print", last_trade(day), "150")
    check("a close-code print after the cutoff still wins",
          resolve("A", day + [(56000, "160", "1", "GC", "K")], ["GC"], "99",
                  cut=54000), Close("A", "160", "qatt", ""))
    check("the last trade before the cutoff",
          resolve("A", day, ["GC"], "99", cut=54000),
          Close("A", "103", "qatt", "last-trade"))
    after = [(55000, "150", "1", "", "K"), (56000, "151", "1", "", "K")]
    check("every print after the cutoff: PX_LAST, a reason of its own",
          resolve("A", after, ["GC"], "99", cut=54000),
          Close("A", "99", "equity_master", "none-before-cutoff"))
    check("and no PX_LAST either: no close",
          resolve("A", after, ["GC"], None, cut=54000),
          Close("A", "", "", "no-close"))
    check("a null time is not before the cutoff when another print has a "
          "time", resolve("A", [(None, "7", "1", "", "K")] + after, ["GC"],
                          "99", cut=54000).reason, "none-before-cutoff")
    check("but every print null: the last of them, as without a cutoff",
          resolve("A", [(None, "7", "1", "", "K"), (None, "8", "1", "", "K")],
                  ["GC"], "99", cut=54000),
          Close("A", "8", "qatt", "last-trade"))
    check("the last trade may come from other rows than the close codes "
          "(the fast path's cut query)",
          resolve("A", after, ["GC"], "99", cut=54000,
                  ltp_rows=[(53000, "98", "", "", "")]),
          Close("A", "98", "qatt", "last-trade"))

    print("\nreading close_conditions.csv")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "close_conditions.csv"
        p.write_text(
            "BBGCode,Country,Venue,CloseCondCodes,LastTradeBefore\n"
            "IS,India,NSI-MAIN,AUC|ACB,\n"
            "JT,Japan,TYO-MAIN,e|ES,\n"
            "AT,Australia,ASX-MAIN,CA,\n"
            "NA,Namibia,NAM-MAIN,NA,\n"
            "KQ,Korea,KOE-MAIN,GC,14:30:00\n",
            encoding="utf-8")
        check("the cutoffs: code -> the HKT time, the blank ones out",
              load_cutoffs(p), {"KQ": "14:30:00"})
        conditions = load_conditions(p)
        check("split on the pipe", conditions["IS"], ["AUC", "ACB"])
        check("a single code is a one-element list", conditions["AT"], ["CA"])
        check("file order is kept, not sorted",
              list(conditions), ["IS", "JT", "AT", "NA", "KQ"])
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
