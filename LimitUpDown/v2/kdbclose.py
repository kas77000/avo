#!/usr/bin/env python3
"""The previous close, out of kdb's equity_master.  The ONLY module here
that touches kdb.

WHY NOT BLOOMBERG.  A computed venue needs one number per name - yesterday's
close - and there were three ways to get it, none of them free:

  PX_YEST_CLOSE              static, and CONFIRMED refused to this
                             subscription.  Not available at any price.
  PREV_CLOSE_VALUE_REALTIME  answers, but it is a real-time field: it is the
                             previous close as the ticker plant holds it,
                             and a name the plant will not serve us for
                             entitlement reasons has no close either.
  equity_master.PX_LAST      this.

equity_master is the Bloomberg DATA LICENCE feed loaded into kdb - the same
feed the R job read off disk as EquitiesDataLicence.rds.  So PX_LAST on the
previous partition is a Bloomberg close of the same lineage as
PX_YEST_CLOSE, ADJUSTED for corporate actions, reached without a real-time
entitlement.  That last part is the point: a market B-PIPE will not price
for us can still be computed, which is what makes every venue switchable.

TWO THINGS THIS GETS RIGHT AND A NAIVE VERSION WOULD NOT.

  THE DATE.  Yesterday lands on a Sunday every Monday, and on every holiday.
  The requested date is rolled back to the most recent partition that
  actually has rows, and BOTH dates are reported so a run that quietly used
  a stale close is visible.

  THE SYMBOL.  The crosscode carries the Bloomberg PRIMARY code - `600001
  CG` - while equity_master keys Shanghai on the COMPOSITE, `600001.CH`.
  Every name therefore gets up to two candidates, its own suffix first, and
  the run reports which one hit.  Neither is hardcoded.

ONE ROUND TRIP.  A universe is tens of thousands of names; a per-symbol
query would not finish inside the window before the open.

pykx is imported inside connect(), so --self-test and --demo run on a
machine with no kdb and no q licence.

    python kdbclose.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#  What we need, and nothing else.  equity_master carries a lot more; asking
#  for one column keeps the response small enough to read in a log.
CLOSE_FIELD = "PX_LAST"

#  TWO WAYS TO ASK FOR THE PARTITION, because the type of equity_master's
#  `date` column is NOT established and a 2026-09-04 run died on 'type here.
#
#    client   the bound is a python date, converted by pykx on the way out
#    server   the bound is computed IN q from .z.D, so no date crosses the
#             wire at all - only an integer, which cannot be mis-converted
#
#  If the first fails on type, the second removes the conversion from the
#  picture entirely.  If BOTH fail, the column is not a date and no amount
#  of client-side work will fix it: that is a schema question, and the error
#  says so and names the probe that answers it.
MAXDATE_CLIENT_Q = "{[d] exec max date from equity_master where date<=d}"
MAXDATE_SERVER_Q = ("{[n] exec max date from equity_master "
                    "where date<=.z.D-n}")

#  `$s casts the python list of strings to the symbol vector `sym` is.
FETCH_Q = ("{[d;s] select " + CLOSE_FIELD + " by sym from equity_master "
           "where date=d, sym in `$s}")


class KdbError(Exception):
    pass


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run with a computed venue needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


def parse_server(value: str):
    """'host:port' -> (host, port).  Refuses anything else by name, because
    a job that connects somewhere unintended is worse than one that will not
    start."""
    text = (value or "").strip()
    if not text:
        raise KdbError(
            "EQUITY_MASTER_SERVER is empty. Set it in local_settings.py as "
            "host:port - a computed venue cannot get a close without it.")
    if text.count(":") != 1:
        raise KdbError(
            f"EQUITY_MASTER_SERVER {value!r} is not host:port")
    host, port = text.split(":")
    host, port = host.strip(), port.strip()
    if not host or not port.isdigit():
        raise KdbError(f"EQUITY_MASTER_SERVER {value!r} is not host:port")
    return host, int(port)


def bbg_suffix(bbg: str) -> str:
    """'600001 CG' -> 'CG'.  The exchange code the crosscode already carries,
    which is the first candidate and usually the right one."""
    parts = (bbg or "").strip().rsplit(" ", 1)
    return parts[-1] if len(parts) == 2 else ""


def sym_candidates(row, venue) -> list:
    """Its own suffix first, then the venue's composite if that differs.

    Order matters: the primary is right for most markets, and trying it
    first means the composite is only reached for the ones that need it -
    which is how the run report can say which markets depend on it."""
    ticker = (getattr(row, "ticker", "") or "").strip()
    if not ticker:
        return []
    out = []
    ext = bbg_suffix(getattr(row, "bbg", ""))
    if ext:
        out.append(f"{ticker}.{ext}")
    composite = (getattr(venue, "bbg_composite", "") or "").strip()
    if composite and f"{ticker}.{composite}" not in out:
        out.append(f"{ticker}.{composite}")
    return out


def q_type_of(value) -> str:
    """What pykx turns this into, and what q calls it.

    THIS IS THE WHOLE QUESTION when a comparison dies on 'type.  The schema
    says equity_master.date is a q date (type d, -14 as an atom), so if a
    python date arrives as anything else - a timestamp, most likely - then
    `date<=d` is comparing two different types and q refuses.  Printing it
    beats reasoning about it."""
    try:
        import pykx
    except ImportError:
        return f"{type(value).__name__} (pykx not installed, cannot convert)"
    try:
        converted = pykx.toq(value)
    except Exception as e:                                  # noqa: BLE001
        return (f"{type(value).__name__} -> will not convert: "
                f"{type(e).__name__}: {e}")
    kind = getattr(converted, "t", "?")
    return (f"{type(value).__name__} -> {type(converted).__name__} "
            f"(q type {kind})")


def _py(value):
    """The python form of a q value, or the value itself if it has none."""
    try:
        return value.py()
    except AttributeError:
        return value


def date_text(value) -> str:
    got = _py(value)
    return "unknown" if got is None else str(got)


def resolve_date(conn, requested, days_back=1, log=None):
    """(date, how).  The most recent partition on or before the bound.

    THE DATE COMES BACK RAW, not converted to a python object, because it
    is passed straight back to q in the fetch.  Round-tripping it through
    python would reintroduce exactly the conversion this is working around.

    Tries both forms and reports which answered, the way the previous-close
    fields and the sym candidates do - one real run then settles it.

    `log` receives a line per attempt: the query, the argument, and what
    pykx made of it.  That is the evidence, not the guess."""
    say = log or (lambda line: None)
    errors = []
    attempts = ((f"client date {requested}", MAXDATE_CLIENT_Q, requested),
                (f"server .z.D-{days_back}", MAXDATE_SERVER_Q,
                 int(days_back)))
    for how, query, arg in attempts:
        say(f"    try {how}")
        say(f"      q   {query}")
        say(f"      arg {arg!r}  {q_type_of(arg)}")
        try:
            got = conn(query, arg)
        except Exception as e:                              # noqa: BLE001
            errors.append(f"{how}: {type(e).__name__}: {e}")
            say(f"      ERR {type(e).__name__}: {e}")
            continue
        if _py(got) is None:
            errors.append(f"{how}: no partition on or before the bound")
            say("      got null - no partition on or before the bound")
            continue
        say(f"      got {got!r}  ({date_text(got)})")
        return got, how

    NL = chr(10)
    raise KdbError(NL.join(
        ["equity_master: could not resolve a partition date."]
        + ["    " + e for e in errors]
        + ["  BOTH ways of asking failed, so this is most likely the SCHEMA",
           "  rather than the client: `date` may not be a q date column at",
           "  all. Run",
           "      python ../other/em_probe.py --server HOST:PORT --meta",
           "  and check what type `date` actually is."]))


def _to_decimal(value):
    """A close that is not a positive finite number is not a close.

    kdb nulls arrive as nan or 0w depending on the column type, and both
    would otherwise sail through as a Decimal and produce a band."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode().strip()
    return str(value).strip()


def _cell(row, field):
    """kdb hands rows back as dicts, pykx tables or tuples depending on the
    build.  A field the row does not carry is None, not an exception."""
    try:
        return row[field]
    except (TypeError, KeyError, IndexError):
        return None


def fetch(conn, date, syms, log=None) -> dict:
    """sym -> Decimal close, for the syms that had one.

    A sym with no row, or a null close, is simply absent; the caller reports
    it rather than guessing a price."""
    say = log or (lambda line: None)
    if not syms:
        return {}
    say(f"    q   {FETCH_Q}")
    say(f"      date {date!r}  {q_type_of(date)}")
    say(f"      syms {len(syms)}, first few {list(syms)[:5]}")
    try:
        result = conn(FETCH_Q, date, list(syms))
    except Exception as e:                                  # noqa: BLE001
        say(f"      ERR {type(e).__name__}: {e}")
        raise
    if result is None:
        say("      got nothing back")
        return {}
    try:
        items = result.items()
    except AttributeError:
        items = dict(result).items()
    out = {}
    say(f"      got {len(list(items))} rows" if hasattr(items, "__len__")
        else "      got rows")
    for sym, row in items:
        close = _to_decimal(_cell(row, CLOSE_FIELD))
        if close is not None:
            out[_text(sym)] = close
    return out


def closes_for(rows, venues, fetched):
    """(close per row key, which candidate hit, the rows with nothing).

    Keyed on the RIC, because that is unique per row where a sym is not: two
    listings can resolve to the same composite."""
    closes, hits, missing = {}, {}, []
    for r in rows:
        venue = venues.get(r.venue_id)
        found = None
        for candidate in sym_candidates(r, venue):
            if candidate in fetched:
                found = candidate
                break
        if found is None:
            missing.append(r)
            continue
        closes[r.ric] = fetched[found]
        suffix = found.rsplit(".", 1)[-1]
        hits[suffix] = hits.get(suffix, 0) + 1
    return closes, hits, missing


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

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except KdbError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                 f"{fragment!r}"))

    D = Decimal

    class Row:
        def __init__(self, ric, bbg, ticker, venue_id):
            self.ric, self.bbg = ric, bbg
            self.ticker, self.venue_id = ticker, venue_id

    class Venue:
        def __init__(self, composite):
            self.bbg_composite = composite

    print("kdbclose --self-test")
    print()
    print("reading the server setting")
    check("host and port", parse_server("a-host:5010"), ("a-host", 5010))
    check("whitespace does not count", parse_server("  a-host : 5010 "),
          ("a-host", 5010))
    raises("an empty setting is refused by NAME, so the fix is obvious",
           lambda: parse_server(""), "EQUITY_MASTER_SERVER is empty")
    raises("a bare host has no port to connect to",
           lambda: parse_server("a-host"), "is not host:port")
    raises("a non-numeric port", lambda: parse_server("a-host:abc"),
           "is not host:port")

    print()
    print("building the equity_master symbol")
    check("the crosscode's own exchange code comes first",
          sym_candidates(Row("600001.SS", "600001 CG", "600001", "SHA-MAIN"),
                         Venue("CH")), ["600001.CG", "600001.CH"])
    check("a venue whose composite IS its own code has one candidate, not a "
          "duplicate", sym_candidates(Row("BBCA.JK", "BBCA IJ", "BBCA",
                                          "JKT-MAIN"), Venue("IJ")),
          ["BBCA.IJ"])
    check("no composite configured still gives the primary",
          sym_candidates(Row("7203.T", "7203 JT", "7203", "TYO-MAIN"), None),
          ["7203.JT"])
    check("a row with no ticker resolves to nothing rather than '.JP'",
          sym_candidates(Row("X", "", "", "TYO-MAIN"), Venue("JP")), [])
    check("the suffix on its own", bbg_suffix("600001 CG"), "CG")
    check("a code with no exchange", bbg_suffix("600001"), "")

    print()
    print("a close that is not a close")
    check("a normal price", _to_decimal(8000.0), D("8000.0"))
    check("a string, as kdb sometimes hands them back", _to_decimal("3000"),
          D("3000"))
    check("zero is not a price", _to_decimal(0), None)
    check("negative is not a price", _to_decimal(-5), None)
    check("a kdb null arriving as nan", _to_decimal(float("nan")), None)
    check("infinity, which is how 0w can arrive",
          _to_decimal(float("inf")), None)
    check("None", _to_decimal(None), None)
    check("a bool is not a price even though it is a number",
          _to_decimal(True), None)

    print()
    print("saying what a value becomes on the q side")
    got = q_type_of("2026-09-03")
    check("the answer names the python type either way",
          got.startswith("str"), True)
    check("and when pykx is absent it SAYS so rather than reporting a "
          "wrong q type", ("pykx not installed" in got) or ("->" in got),
          True)

    print("resolving the partition date, which is where a live run died")

    class Conn:
        """A kdb that raises on one query shape and answers the other."""
        def __init__(self, fails, answer="2026.09.03"):
            self.fails, self.answer, self.asked = fails, answer, []

        def __call__(self, query, *args):
            self.asked.append(query)
            if self.fails in query:
                raise RuntimeError("type")
            return self.answer

    c = Conn(fails="nothing matches this")
    check("the client-side bound is tried FIRST, because it asks for the "
          "date we actually want rather than the server's idea of it",
          resolve_date(c, "2026-09-03")[1].startswith("client date"), True)
    check("and when it works the server form is never sent", len(c.asked), 1)

    c = Conn(fails="where date<=d")
    lines = []
    got, how = resolve_date(c, "2026-09-03", log=lines.append)
    check("the log shows BOTH attempts, so a failure is not a mystery",
          sum(1 for l in lines if l.strip().startswith("try")), 2)
    check("it prints the query that was actually sent",
          any("exec max date" in l for l in lines), True)
    check("and what the argument became on the q side, which IS the "
          "question when a comparison dies on type",
          any("arg " in l for l in lines), True)
    check("the failure is logged with its message",
          any("ERR RuntimeError: type" in l for l in lines), True)
    check("a 'type error on the python date falls back to computing the "
          "bound IN q, where no date crosses the wire at all",
          how.startswith("server .z.D"), True)
    check("and the date still comes back", got, "2026.09.03")

    c = Conn(fails="equity_master")
    try:
        resolve_date(c, "2026-09-03")
        got = "no error"
    except KdbError as e:
        got = str(e)
    check("when BOTH fail it is reported as a SCHEMA question rather than a "
          "bare QError", "most likely the SCHEMA" in got, True)
    check("and the error names the exact command that answers it",
          "em_probe.py" in got and "--meta" in got, True)
    check("both attempts are listed, so what was tried is not a mystery",
          got.count("RuntimeError") >= 2, True)

    class Empty:
        def __call__(self, query, *args):
            return None
    try:
        resolve_date(Empty(), "2026-09-03")
        got = "no error"
    except KdbError as e:
        got = str(e)
    check("a table with no partition on or before the bound is a DIFFERENT "
          "failure and says so", "no partition on or before" in got, True)

    check("a raw q value prints for the report", date_text("2026.09.03"),
          "2026.09.03")
    check("and a null one does not pretend to be a date",
          date_text(None), "unknown")

    print("resolving a universe against what kdb returned")
    rows = [Row("600001.SS", "600001 CG", "600001", "SHA-MAIN"),
            Row("BBCA.JK", "BBCA IJ", "BBCA", "JKT-MAIN"),
            Row("GONE.JK", "GONE IJ", "GONE", "JKT-MAIN")]
    venues = {"SHA-MAIN": Venue("CH"), "JKT-MAIN": Venue("IJ")}
    #  Shanghai answers only on the COMPOSITE, which is the whole reason the
    #  second candidate exists.
    fetched = {"600001.CH": D("12.34"), "BBCA.IJ": D("8000")}
    closes, hits, missing = closes_for(rows, venues, fetched)
    check("both resolved names get their close",
          closes, {"600001.SS": D("12.34"), "BBCA.JK": D("8000")})
    check("and the run can say WHICH suffix hit, so the day a market stops "
          "resolving is visible", hits, {"CH": 1, "IJ": 1})
    check("the name kdb had nothing for is handed back, not dropped",
          [r.ric for r in missing], ["GONE.JK"])

    check("nothing asked, nothing returned", fetch(None, "d", []), {})

    print()
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
