#!/usr/bin/env python3
"""Reference data out of kdb: equity_master fields, tick ladders, and the
last quote of a name that did not trade.

This is qattsource.py's sibling, not a replacement for it - the two share
the same conn, the same `_rows`/`text` normalising and the same "no `$ cast,
no `by`, no cond in a where clause" rules that Phase0 paid for live (see
../../Phase0/AB/LimitUpDown/kdbclose.py and
../../Phase0/AB/TradingData/equitymaster.py).  What is new here is what
extract.py needs beyond a print: the day's equity_master row, a name's tick
ladder, and - for a name qatt never printed - the standing quote instead.

THE SYM.  A crosscode row carries the Bloomberg PRIMARY code (`7203 JT`);
equity_master and the tick tables key everything on the COMPOSITE
(`7203.JP`).  ref_candidates is the union of the two rules already in this
repo: LimitUpDown's own-suffix-then-composite, and TradingData's India
special case (NSI-MAIN tries .IS before .IN; BSE-MAIN tries only .IN, never
its own .IB) - plus, appended last, the sym extract.py already resolved
against equity_master for this row (universe.py's job, not this file's), so
a name found one way is not asked for twice.

THE TICK LADDER is the two tables the trading system itself rounds by,
exactly as kdbclose.fetch_ladders reads them - same two queries (IDS_Q,
TBL_Q), same id-as-text matching (_id_text), because ticksizeids and
ticksizetbl still do not agree on how an id is stored (a symbol built from
an int on one side, the int itself on the other).  What is DIFFERENT here:
the bounds are handed back RAW, sym -> [(price, ticksize), ...], not folded
into the floor form kdbclose.py's `ticks.from_kdb` produces.  R does that
conversion; Phase1 has no ticks.py to duplicate it in.

THE QUOTE.  `quote` is a keyed result - `select ... by sym` - and unlike
equity_master's plain select, a keyed table needs local q to unkey and this
box has no q licence.  So the QUERY does the unkeying: `0!select ... by sym
...` is sent, not the bare `by`, and it is the one difference from the
literal query text this job's brief first wrote down (noted here and in the
task report).  The HDB is asked with a date; the RDB, which carries no date
column at all, is asked without one - `date=None` selects between them,
never a flag.

pykx never crosses this file's imports.  Every function here takes an
already-open `conn` and a callable is all the self-test needs.

    python refdata.py --self-test
"""

from __future__ import annotations

from decimal import Decimal

import marketcfg
import qattsource
from ticksfile import _num

#  WHAT equity_master OWES US, beyond the close closes.py already reads.
#  Verbatim from the brief - the same nine names PX_LAST through
#  LONG_COMP_NAME, in this order, because the query text is built from it.
EQUITY_FIELDS = ("PX_LAST", "EQY_BETA", "volatility", "REL_INDEX",
                 "CUR_MKT_CAP", "fx_last", "ID_ISIN", "INDUSTRY_SECTOR",
                 "LONG_COMP_NAME")

#  India is looked up by fixed suffixes, in this order, and never by the
#  crosscode's own - equitymaster.py's rule, ported unchanged: equity_master
#  has no .IB syms, and an NSE name may sit under .IS before .IN.
INDIA_SUFFIXES = {"NSI-MAIN": ("IS", "IN"), "BSE-MAIN": ("IN",)}

#  NO `$ CAST, NO `by`.  pykx already sends symbols; `$ on one is itself a
#  'type, and a keyed answer needs a q licence this box does not have to
#  unkey.  A plain select of sym plus the nine fields needs neither.
EQUITY_Q = ("{[d;s] select sym," + ",".join(EQUITY_FIELDS) +
            " from equity_master where date=d, sym in s}")


def _py(value):
    """pykx atoms carry a .py(); plain python values do not.  Kept local
    rather than imported - this is the one place in refdata.py a raw kdb
    number is needed before it is text, and every kdb-facing module in this
    repo (kdbclose.py, equitymaster.py, qattsource.py) carries its own copy
    of exactly this rather than share a private one."""
    try:
        return value.py()
    except AttributeError:
        return value


def _field_text(value) -> str:
    """One kdb cell, formatted for the file.

    qattsource.text is right for a symbol or a string - it already unwraps
    a pykx atom and strips it.  A NUMBER goes through ticksfile._num
    instead: text() would call str() on it too, which is the same repr
    precision, but _num also catches the scientific notation a very large
    or very small value can arrive in (equity_master's CUR_MKT_CAP is easily
    a large enough number for one).

    A RAW FLOAT GOES THROUGH Decimal(str(v)) FIRST, and skipping that step
    is a real bug, not a style choice: _num's fallback is
    `format(value, "f")`, and a bare float's "f" format defaults to SIX
    decimal places - 1e-07 would come back "0.000000", the value silently
    gone.  Decimal has no such default; format(Decimal("1E-7"), "f") is the
    exact "0.0000001".  str(v) is still where the repr precision comes
    from - Decimal(str(v)) only carries it into a type _num renders in
    full."""
    v = _py(value)
    if isinstance(v, bool):
        return qattsource.text(v)
    if isinstance(v, Decimal):
        return _num(v)
    if isinstance(v, (int, float)):
        return _num(Decimal(str(v)))
    return qattsource.text(value)


def ref_candidates(row, markets, master_sym) -> list:
    """Its own suffix first, then the market's composite if that differs;
    India uses INDIA_SUFFIXES instead.  Then `master_sym` - the sym
    universe.py already resolved this row to against equity_master - if it
    is not already in the list.

    THE UNION OF TWO RULES, so every candidate either Phase0 script would
    have tried is still tried here, in the same order: LimitUpDown's own
    suffix then composite (kdbclose.sym_candidates), TradingData's India
    special case (equitymaster.sym_candidates).  Appending master_sym last
    costs nothing when it is one of the first two, and answers when it is
    not - equity_master itself is the authority on the sym, so the row's own
    guess should never be the ONLY thing tried."""
    ticker = (getattr(row, "ticker", "") or "").strip()
    out = []
    if ticker:
        india = INDIA_SUFFIXES.get(getattr(row, "market", ""))
        if india:
            out = [f"{ticker}.{s}" for s in india]
        else:
            ext = (getattr(row, "bbg_ext", "") or "").strip()
            if ext:
                out.append(f"{ticker}.{ext}")
            comp = marketcfg.composite(getattr(row, "market", ""), markets)
            if comp and f"{ticker}.{comp}" not in out:
                out.append(f"{ticker}.{comp}")
    master_sym = (master_sym or "").strip()
    if master_sym and master_sym not in out:
        out.append(master_sym)
    return out


def pick_ref(row, cands, found) -> str | None:
    """The first of `cands` that `found` actually has, or None.

    `found` is whatever fetch_equity or fetch_quotes came back with - a
    dict keyed on sym is what both produce, and `in` on a dict tests its
    keys.  `row` is accepted rather than used: it is here so a caller can
    add row-aware tie-breaking later without changing every call site, the
    way closes.resolve takes `sym` for its own report even though `pick`
    underneath does not need it."""
    for c in cands:
        if c in found:
            return c
    return None


def fetch_equity(conn, date, syms, log=None) -> dict:
    """sym -> {field: text}, for the syms that had a row.

    ONE ROUND TRIP for the whole chunk - same reasoning as every other kdb
    read in this repo: a universe is tens of thousands of names."""
    say = log or (lambda line: None)
    if not syms:
        return {}
    say(f"      {len(syms)} syms, first few {list(syms)[:5]}")
    rows = qattsource._rows(conn(EQUITY_Q, date, list(syms)))
    say(f"      got {len(rows)} rows")
    out = {}
    for row in rows:
        sym = qattsource.text(row.get("sym"))
        if not sym:
            continue
        out[sym] = {f: _field_text(row.get(f)) for f in EQUITY_FIELDS}
    return out


#  THE TICK LADDER, from the two tables the trading system itself rounds
#  by - ticksizeids (sym -> id) and ticksizetbl (id, price, ticksize),
#  neither partitioned by date because a tick ladder is reference data, not
#  a daily fact.  Ported unchanged from kdbclose.py: no cast, no cond in a
#  where clause, and TBL_Q asks for the WHOLE table rather than filtering by
#  id, because the two tables do not even agree on how an id is stored - see
#  _id_text - and matching on text in python sidesteps that entirely.
IDS_Q = "{[s] select sym, id from ticksizeids where sym in s}"
TBL_Q = "select id, price, ticksize from ticksizetbl"


def _id_text(value) -> str:
    """A tick table id as text, whatever either table stores it as.

    ticksizeids holds a symbol built from an int; ticksizetbl keeps the
    number.  So `6132, 6132 and 6132.0 all have to key the same - the float
    case is the one that would otherwise silently miss, since str() on it
    gives '6132.0'.  Ported unchanged from kdbclose.py."""
    t = qattsource.text(value)
    if not t:
        return ""
    try:
        d = Decimal(t)
    except Exception:                                          # noqa: BLE001
        return t
    if d.is_finite() and d == d.to_integral_value():
        return str(int(d))
    return t


def fetch_ladders(conn, syms, log=None) -> dict:
    """sym -> [(price, ticksize), ...] as text, for the syms that have a
    ladder.  RAW rows, in ascending price order - NOT the floor form
    kdbclose.py's `ticks.from_kdb` builds.  Phase1 has no ticks.py, and the
    brief is explicit that this conversion is R's to make, not python's."""
    say = log or (lambda line: None)
    if not syms:
        return {}

    ids = qattsource._rows(conn(IDS_Q, list(syms)))
    by_sym = {}
    for r in ids:
        sym, tid = qattsource.text(r.get("sym")), _id_text(r.get("id"))
        if sym and tid:
            by_sym[sym] = tid
    say(f"      {len(by_sym)} of {len(syms)} syms have a tick table")
    if not by_sym:
        return {}

    #  A PLAIN EXPRESSION, sent with no argument - see qattsource.py's own
    #  PARTITIONS_Q for why: a `{...}` LAMBDA sent bare comes back unapplied,
    #  not evaluated.  TBL_Q is not wrapped in one.
    tbl = qattsource._rows(conn(TBL_Q))
    say(f"      {len(tbl)} tick table rows")

    by_id = {}
    for r in tbl:
        tid = _id_text(r.get("id"))
        price, tick = _field_text(r.get("price")), _field_text(r.get("ticksize"))
        if tid and price and tick:
            by_id.setdefault(tid, []).append((price, tick))
    for tid, rows in by_id.items():
        rows.sort(key=lambda pt: Decimal(pt[0]))

    out = {sym: by_id[tid] for sym, tid in by_sym.items() if tid in by_id}
    say(f"      {len(out)} ladders built")
    return out


#  0! INSIDE THE LAMBDA, NOT THE LITERAL `by sym` QUERY THE BRIEF FIRST
#  WROTE DOWN.  `select ... by sym` comes back keyed, and unkeying a keyed
#  table needs local q - which is exactly the licence problem EQUITY_Q's own
#  comment (and kdbclose.py before it) already worked around by not using
#  `by` at all.  A quote's LAST time/bid/ask per sym has no substitute for
#  `by`, so the query unkeys itself instead: `0!select ...` runs the select
#  and then strips the key server-side, so the answer needs no local q
#  either.  See the task report for why this changes the query text.
QUOTE_HDB_Q = ("{[d;s] 0!select last time, last bid, last ask by sym "
               "from quote where date=d, sym in s}")
QUOTE_RDB_Q = ("{[s] 0!select last time, last bid, last ask by sym "
               "from quote where sym in s}")


def fetch_quotes(conn, date, syms, log=None) -> dict:
    """sym -> (seconds of day | None, bid, ask), for the syms that had a
    standing quote.  The HDB is asked when `date` is given; the RDB - which
    carries no date column at all, like qattsource's live_ticks_q - when it
    is None.  Never both, and nothing about which is asked is a flag: the
    date itself decides, the same way qattsource.fetch_live_ticks does."""
    say = log or (lambda line: None)
    if not syms:
        return {}
    if date is None:
        say(f"      RDB: {len(syms)} syms, first few {list(syms)[:5]}")
        rows = qattsource._rows(conn(QUOTE_RDB_Q, list(syms)))
    else:
        say(f"      HDB {date}: {len(syms)} syms, first few {list(syms)[:5]}")
        rows = qattsource._rows(conn(QUOTE_HDB_Q, date, list(syms)))
    say(f"      got {len(rows)} rows")
    out = {}
    for row in rows:
        sym = qattsource.text(row.get("sym"))
        if not sym:
            continue
        out[sym] = (qattsource._seconds_of(row.get("time")),
                    _field_text(row.get("bid")),
                    _field_text(row.get("ask")))
    return out


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    D = Decimal

    class Row:
        def __init__(self, ticker="", bbg_ext="", market=""):
            self.ticker, self.bbg_ext, self.market = ticker, bbg_ext, market

    class Mkt:
        def __init__(self, comp):
            self.bbg_composite = comp

    M = {"KSC-MAIN": Mkt("KS"), "SSC-MAIN": Mkt("CH"), "ASX-MAIN": Mkt("AU")}

    print("refdata --self-test\n\nbuilding the equity_master/qatt candidates")
    check("India tries .IS before .IN, never its own .IB",
          ref_candidates(Row("RELIANCE", "IB", "NSI-MAIN"), M, ""),
          ["RELIANCE.IS", "RELIANCE.IN"])
    check("Bombay tries .IN only",
          ref_candidates(Row("RELIANCE", "IB", "BSE-MAIN"), M, ""),
          ["RELIANCE.IN"])
    check("a normal market: its own suffix, then the composite",
          ref_candidates(Row("600000", "C1", "SSC-MAIN"), M, ""),
          ["600000.C1", "600000.CH"])
    check("a composite that matches its own suffix adds no duplicate",
          ref_candidates(Row("BHP", "AU", "ASX-MAIN"), M, ""), ["BHP.AU"])
    check("an unconfigured market gets one candidate only",
          ref_candidates(Row("ABC", "XX", "ZZZ-MAIN"), M, ""), ["ABC.XX"])
    check("no ticker and no master sym is nothing at all",
          ref_candidates(Row("", "XX", "ASX-MAIN"), M, ""), [])
    check("the resolved master sym is appended LAST when it is new",
          ref_candidates(Row("600000", "C1", "SSC-MAIN"), M, "600000.XX"),
          ["600000.C1", "600000.CH", "600000.XX"])
    check("and not duplicated when it is already one of the two",
          ref_candidates(Row("600000", "C1", "SSC-MAIN"), M, "600000.CH"),
          ["600000.C1", "600000.CH"])
    check("a master sym is still offered even with no ticker at all",
          ref_candidates(Row("", "", ""), M, "ZZZ.ZZ"), ["ZZZ.ZZ"])

    print("\npicking the reference row")
    cands = ["A.C1", "A.CH", "A.XX"]
    check("the first candidate actually found wins",
          pick_ref(None, cands, {"A.CH": {}, "A.XX": {}}), "A.CH")
    check("order is the candidate list's, not the found dict's",
          pick_ref(None, ["A.XX", "A.CH"], {"A.CH": {}, "A.XX": {}}), "A.XX")
    check("none of the candidates found is None, not KeyError",
          pick_ref(None, cands, {}), None)
    check("no candidates at all is None too",
          pick_ref(None, [], {"A.CH": {}}), None)

    print("\nthe query itself")
    check("no `$ cast - pykx sends symbols already",
          "`$" in EQUITY_Q, False)
    check("and no `by` - a keyed table needs a q licence to unkey",
          " by " in EQUITY_Q, False)
    check("sym is selected explicitly, and every field follows it",
          EQUITY_Q, "{[d;s] select sym,PX_LAST,EQY_BETA,volatility,"
                    "REL_INDEX,CUR_MKT_CAP,fx_last,ID_ISIN,INDUSTRY_SECTOR,"
                    "LONG_COMP_NAME from equity_master where date=d, "
                    "sym in s}")

    print("\nformatting a cell")
    check("a normal float keeps its own precision",
          _field_text(8000.0), "8000.0")
    check("a very small float drops the exponent",
          _field_text(1e-07), "0.0000001")
    check("a Decimal formats the same way",
          _field_text(D("83.64")), "83.64")
    check("text is left to qattsource.text",
          _field_text("Toyota Motor Corp"), "Toyota Motor Corp")
    check("bytes are decoded", _field_text(b"JP3633400001"), "JP3633400001")
    check("a null cell is blank", _field_text(None), "")
    check("a bool is not treated as a number",
          _field_text(True), "True")

    print("\nfetching equity_master")

    class EqConn:
        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return [{"sym": "600000.CH", "PX_LAST": 12.34, "EQY_BETA": -0.3,
                     "volatility": 1e-07, "REL_INDEX": None,
                     "CUR_MKT_CAP": 5.5e10, "fx_last": 1.0,
                     "ID_ISIN": "CNE000001SS8",
                     "INDUSTRY_SECTOR": "Financials",
                     "LONG_COMP_NAME": "A Bank"}]

    ec = EqConn()
    got = fetch_equity(ec, "2026.09.28", ["600000.CH"])
    check("one round trip for the whole chunk", len(ec.calls), 1)
    check("date and syms both travel",
          ec.calls[0][1], ("2026.09.28", ["600000.CH"]))
    check("every field comes back as text",
          got["600000.CH"],
          {"PX_LAST": "12.34", "EQY_BETA": "-0.3",
           "volatility": "0.0000001", "REL_INDEX": "",
           "CUR_MKT_CAP": "55000000000.0", "fx_last": "1.0",
           "ID_ISIN": "CNE000001SS8", "INDUSTRY_SECTOR": "Financials",
           "LONG_COMP_NAME": "A Bank"})
    check("no syms means no round trip", fetch_equity(ec, "d", []), {})
    check("and no extra call", len(ec.calls), 1)

    print("\nfetching the tick ladder, Korea's real table 6132")

    class TickConn:
        def __init__(self, ids=None, tbl=None):
            self.ids = ids if ids is not None else [
                {"sym": "000020.KS", "id": "6132"},
                {"sym": "005930.KS", "id": "6132"}]
            self.tbl = tbl if tbl is not None else [
                {"id": "6132", "price": 20000.0, "ticksize": 10.0},
                {"id": "6132", "price": 2000.0, "ticksize": 1.0},
                {"id": "6132", "price": 5000.0, "ticksize": 5.0}]
            self.asked = []

        def __call__(self, query, *args):
            self.asked.append((query, args))
            return self.ids if query is IDS_Q else self.tbl

    tc = TickConn()
    got = fetch_ladders(tc, ["000020.KS", "005930.KS"])
    check("the RAW rows, sorted by price, NOT the floor form "
          "kdbclose.py's from_kdb would build (which would start at 0)",
          got["000020.KS"],
          [("2000.0", "1.0"), ("5000.0", "5.0"), ("20000.0", "10.0")])
    check("names sharing a table share the same list",
          got["000020.KS"] == got["005930.KS"], True)
    check("TWO ROUND TRIPS FOR THE WHOLE UNIVERSE, not two per name",
          len(tc.asked), 2)
    check("and the tick tables are asked for with NO ARGUMENT",
          tc.asked[1], (TBL_Q, ()))
    check("so it is a plain expression, not a lambda that would come back "
          "unapplied", TBL_Q.startswith("{"), False)
    check("no `$ cast in either query", "`$" in IDS_Q or "`$" in TBL_Q, False)
    check("no `by` in either", " by " in IDS_Q or " by " in TBL_Q, False)
    check("neither carries a date - a tick ladder is reference data",
          "date" in IDS_Q or "date" in TBL_Q, False)
    check("no syms, no round trips at all",
          (fetch_ladders(TickConn(), []), len(TickConn().asked)), ({}, 0))

    mixed = TickConn(
        ids=[{"sym": "000020.KS", "id": "6132"}],
        tbl=[{"id": 6132, "price": 2000.0, "ticksize": 1.0}])
    check("a symbol id on one side and an int on the other still join",
          fetch_ladders(mixed, ["000020.KS"])["000020.KS"],
          [("2000.0", "1.0")])
    floats = TickConn(
        ids=[{"sym": "000020.KS", "id": "6132"}],
        tbl=[{"id": 6132.0, "price": 2000.0, "ticksize": 1.0}])
    check("and a float id joins too, which str() alone would miss by "
          "calling it '6132.0'",
          list(fetch_ladders(floats, ["000020.KS"])), ["000020.KS"])
    check("6132, `6132 and 6132.0 are one table",
          {_id_text("6132"), _id_text(6132), _id_text(6132.0)}, {"6132"})
    check("a sym with no id is absent, not guessed at",
          fetch_ladders(TickConn(ids=[]), ["000020.KS"]), {})
    check("nor is an id whose table has no rows",
          fetch_ladders(TickConn(tbl=[]), ["000020.KS"]), {})

    print("\nfetching the last quote of a name that did not trade")

    import datetime as dt

    class QuoteConn:
        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return [{"sym": "ZZZ.KS", "time": dt.time(9, 31, 33),
                     "bid": 4995.0, "ask": 5005.0}]

    qc = QuoteConn()
    got = fetch_quotes(qc, "2026.09.28", ["ZZZ.KS"])
    check("the HDB query is used when a date is given",
          qc.calls[0][0], QUOTE_HDB_Q)
    check("and it unkeys itself with 0! - a plain `by sym` would come back "
          "keyed, and this box has no q licence to fix that locally",
          "0!select" in QUOTE_HDB_Q, True)
    check("date and syms both travel",
          qc.calls[0][1], ("2026.09.28", ["ZZZ.KS"]))
    check("the row is shaped with seconds of day, not a time object",
          got["ZZZ.KS"], (34293, "4995.0", "5005.0"))

    qc2 = QuoteConn()
    fetch_quotes(qc2, None, ["ZZZ.KS"])
    check("a date of None asks the RDB instead",
          qc2.calls[0][0], QUOTE_RDB_Q)
    check("and it unkeys itself too", "0!select" in QUOTE_RDB_Q, True)
    check("the RDB takes only the syms - one argument, not two",
          qc2.calls[0][1], (["ZZZ.KS"],))
    check("no date crosses the wire to the RDB at all",
          "date" in QUOTE_RDB_Q, False)

    class NoQuote:
        def __call__(self, q, *args):
            return []

    check("a null time is None, not a bogus 0", fetch_quotes(
        lambda q, *a: [{"sym": "Y", "time": None, "bid": 1.0, "ask": 1.1}],
        "d", ["Y"])["Y"][0], None)
    check("no syms means no round trip",
          fetch_quotes(NoQuote(), "d", []), {})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
