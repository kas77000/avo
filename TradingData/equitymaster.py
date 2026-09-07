#!/usr/bin/env python3
"""equity_master out of kdb.  This is the ONLY module that touches kdb.

equity_master is not a kdb-native table.  It is the Bloomberg Data Licence
feed loaded into kdb - the same feed the R job reads off disk as
EquitiesDataLicence.rds.  It carries TICKER_AND_EXCH_CODE, the R job's join
key at :90, and 13 of the 16 columns the R drops at :84-88.

TWO THINGS ARE UNCERTAIN AND BOTH ARE REPORTED RATHER THAN ASSUMED.

  sym    is the Bloomberg ticker dot-joined to an exchange code, but WHICH
         code is not settled.  config/markets.csv distinguishes the venue
         code (KP for KSC-MAIN) from the composite (KS), and the codes we
         were given mix the two.  So every row gets up to two candidates,
         its own suffix first, and the run reports which one hit.  The first
         live run settles it; until then neither is hardcoded.

  date   .z.D-1 lands on a Sunday every Monday and on every holiday, so the
         requested date is rolled back to the most recent one that actually
         has rows, and both dates are reported.

ONE ROUND TRIP.  A universe is tens of thousands of names and a per-symbol
query would take longer than the window before the open.

pykx is imported inside connect(), so every other module - and --self-test
and --demo - runs on a machine with no kdb and no q licence.

    python equitymaster.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

FIELDS = ("PX_LAST", "EQY_BETA", "volatility", "REL_INDEX", "CUR_MKT_CAP",
          "fx_last", "ID_ISIN", "INDUSTRY_SECTOR", "MARKET_STATUS", "CRNCY")

#  TWO WAYS TO ASK FOR THE PARTITION, because the type of equity_master's
#  `date` column is NOT established and a LimitUpDown run on 2026-09-04 died
#  on 'type.
#
#    client   the bound is a python date, converted by pykx on the way out
#    server   the bound is computed IN q from .z.D, so no date crosses the
#             wire at all - only an integer, which cannot be mis-converted
#
#  If BOTH fail, the column is not a date and no client-side work will fix
#  it: that is a schema question, and the error says so.
MAXDATE_CLIENT_Q = "{[d] exec max date from equity_master where date<=d}"
MAXDATE_SERVER_Q = ("{[n] exec max date from equity_master "
                    "where date<=.z.D-n}")

#  ONE QUERY, and every decoration on it was a mistake.  LimitUpDown paid
#  for these live on 2026-09-04 against this same table; see
#  ../LimitUpDown/v2/kdbclose.py, which records them:
#
#    sym in `$s              'type    - pykx sends symbol ATOMS, and `$
#                            applied to a symbol is itself a type error.
#                            This module used to carry that cast, with a
#                            comment claiming it made a symbol vector.
#    sym in $[11h=type s;..] 'rank    - the guard was worse than the bug; a
#                            cond in a where clause is an arity error.
#    select ... BY sym       licence  - a KEYED table has to be unkeyed by
#                            local q, and this box has no q licence.  IPC
#                            needs none; running q code does.
#
#  So: no cast, because the syms arrive as symbols already; and no `by`,
#  because a plain table reads back through pandas without a q licence.
FETCH_Q = ("{[d;s] select sym," + ",".join(FIELDS) + " from equity_master "
           "where date=d, sym in s}")


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


def sym_candidates(row, markets) -> list:
    """Its own suffix first, then the market's composite if that differs."""
    ticker = (getattr(row, "ticker", "") or "").strip()
    if not ticker:
        return []
    out = []
    ext = (getattr(row, "bbg_ext", "") or "").strip()
    if ext:
        out.append(f"{ticker}.{ext}")
    m = markets.get(getattr(row, "market", ""))
    comp = (getattr(m, "bbg_composite", "") or "").strip() if m else ""
    if comp and f"{ticker}.{comp}" not in out:
        out.append(f"{ticker}.{comp}")
    return out


def _py(value):
    """The python form of a q value, or the value itself if it has none."""
    try:
        return value.py()
    except AttributeError:
        return value


def date_text(value) -> str:
    got = _py(value)
    return "unknown" if got is None else str(got)


def resolve_date(conn, requested, days_back=1):
    """(date, how).  The most recent partition on or before the bound.

    THE DATE COMES BACK RAW, not converted to a python object, because it is
    passed straight back to q in the fetch.  Round-tripping it through python
    would reintroduce exactly the conversion this is working around.

    Both forms are tried and the run says which answered; one real run then
    settles it."""
    errors = []
    attempts = ((f"client date {requested}", MAXDATE_CLIENT_Q, requested),
                (f"server .z.D-{days_back}", MAXDATE_SERVER_Q,
                 int(days_back)))
    for how, query, arg in attempts:
        try:
            got = conn(query, arg)
        except Exception as e:                              # noqa: BLE001
            errors.append(f"{how}: {type(e).__name__}: {e}")
            continue
        if _py(got) is None:
            errors.append(f"{how}: no partition on or before the bound")
            continue
        return got, how

    NL = chr(10)
    raise SystemExit(NL.join(
        ["equity_master: could not resolve a partition date."]
        + ["    " + e for e in errors]
        + ["  BOTH ways of asking failed, so this is most likely the SCHEMA",
           "  rather than the client: `date` may not be a q date column at",
           "  all.  Run",
           "      python ../LimitUpDown/other/em_probe.py "
           "--server HOST:PORT --meta",
           "  and check what type `date` actually is."]))


def _to_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    return d if d.is_finite() else None


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


def _rows(result):
    """A pykx table -> a list of dicts, whatever the build hands back.

    pandas first because it does not need a q licence; .py() and plain
    iteration are the fallbacks.  This is what
    ../LimitUpDown/v2/kdbclose.py does, and it reads equity_master without
    trouble."""
    if result is None:
        return []
    try:
        return result.pd().to_dict("records")
    except AttributeError:
        pass
    try:
        return list(result.py())
    except AttributeError:
        return list(result)


def fetch(conn, date, syms) -> dict:
    """sym -> {field: raw value}, for the syms that had a row."""
    if not syms:
        return {}
    result = conn(FETCH_Q, date, list(syms))
    items = _rows(result)

    #  NO ROWS AT ALL IS THE SHAPE OF THE QUESTION, NOT THE DATA.  A universe
    #  of thousands always has reference data, so an empty answer means the
    #  partition is empty or `sym` does not look like what was asked for.  A
    #  wrong sym form returns exactly this - QUIETLY - and a silent empty
    #  answer reads like a holiday rather than a bug.
    if not items:
        NL = chr(10)
        raise SystemExit(NL.join([
            f"equity_master has NO rows for {len(syms)} syms on "
            f"{date_text(date)}.",
            "    A universe this size always has rows, so this is the shape "
            "of the question rather than the data:",
            f"    either that partition is empty, or `sym` does not look "
            f"like {list(syms)[:3]}.",
            "    Run",
            f"        python ../LimitUpDown/other/em_probe.py "
            f"--server HOST:PORT --sample {list(syms)[0]}",
            "    to see what the column actually holds."]))

    out = {}
    for row in items:
        sym = _text(_cell(row, "sym"))
        if sym:
            out[sym] = {f: _cell(row, f) for f in FIELDS}
    return out


def self_test() -> int:
    import datetime
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    class Row:
        def __init__(self, ticker, ext, market):
            self.ticker, self.bbg_ext, self.market = ticker, ext, market

    class Mkt:
        def __init__(self, comp):
            self.bbg_composite = comp

    M = {"KSC-MAIN": Mkt("KS"), "SSC-MAIN": Mkt("CH"), "ASX-MAIN": Mkt("AU")}

    print("equitymaster --self-test\n\nbuilding syms")
    check("the crosscode's own suffix comes first",
          sym_candidates(Row("005930", "KP", "KSC-MAIN"), M),
          ["005930.KP", "005930.KS"])
    check("a composite that matches adds no second candidate",
          sym_candidates(Row("BHP", "AU", "ASX-MAIN"), M), ["BHP.AU"])
    check("China's venue code and composite differ",
          sym_candidates(Row("600000", "C1", "SSC-MAIN"), M),
          ["600000.C1", "600000.CH"])
    check("an unconfigured market gets one candidate only",
          sym_candidates(Row("ABC", "XX", "ZZZ-MAIN"), M), ["ABC.XX"])
    check("no ticker means no candidates",
          sym_candidates(Row("", "AU", "ASX-MAIN"), M), [])

    print("\nnumbers out of kdb")
    check("a float becomes a Decimal", _to_decimal(83.64), Decimal("83.64"))
    check("bytes become a Decimal", _to_decimal(b"1.5"), Decimal("1.5"))
    check("a negative beta is legitimate and kept",
          _to_decimal(-0.3), Decimal("-0.3"))
    check("a null is None", _to_decimal(None), None)
    check("nan is None", _to_decimal(float("nan")), None)
    check("a bool is not a number", _to_decimal(True), None)
    check("garbage is None", _to_decimal("n/a"), None)

    print("\nrolling the date back")

    class Conn:
        """`client_ok=False` makes the client-side form raise the way a
        'type error does, so the server-side fallback is exercised."""

        def __init__(self, have, client_ok=True):
            self.have, self.calls, self.client_ok = have, [], client_ok

        def __call__(self, q, *args):
            self.calls.append((q, args))
            if "max date" not in q:
                return []
            if ".z.D-" in q:
                return max(self.have, default=None)
            if not self.client_ok:
                raise RuntimeError("type")
            return max((d for d in self.have if d <= args[0]), default=None)

    friday = datetime.date(2026, 8, 28)
    monday = datetime.date(2026, 8, 31)
    c = Conn([friday])
    got, how = resolve_date(c, monday - datetime.timedelta(days=1))
    check("a Sunday request rolls back to Friday", got, friday)
    check("and the client form answered", how.startswith("client"), True)

    c = Conn([monday])
    check("a date that has rows is used as-is",
          resolve_date(c, monday)[0], monday)

    c = Conn([friday], client_ok=False)
    got, how = resolve_date(c, monday)
    check("a 'type on the client form falls to the server form", got, friday)
    check("and the run says which answered", how.startswith("server"), True)

    c = Conn([])
    try:
        resolve_date(c, monday)
        check("raised when neither form answers", False, True)
    except SystemExit as exc:
        check("and points at the schema, not the client",
              "SCHEMA" in str(exc), True)

    print("\nthe query itself")
    check("no `$ cast - pykx sends symbols already", "`$" in FETCH_Q, False)
    check("no `by` - a keyed table needs a q licence to unkey",
          " by " in FETCH_Q, False)
    check("sym is selected, because the answer is no longer keyed by it",
          "select sym," in FETCH_Q, True)

    print("\nfetching")

    class FetchConn:
        def __init__(self, rows):
            self.rows, self.calls = rows, []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return self.rows

    fc = FetchConn([{"sym": "BHP.AU", "PX_LAST": 40.5, "EQY_BETA": 0.9}])
    got = fetch(fc, friday, ["BHP.AU"])
    check("one round trip, not one per symbol", len(fc.calls), 1)
    check("the date and the sym list are both passed",
          fc.calls[0][1], (friday, ["BHP.AU"]))
    check("a plain table is re-keyed on its own sym column",
          sorted(got), ["BHP.AU"])
    check("and the fields come with it", got["BHP.AU"]["PX_LAST"], 40.5)
    check("a field the row does not carry is None, not an error",
          got["BHP.AU"]["ID_ISIN"], None)
    check("no syms means no round trip at all", fetch(fc, friday, []), {})
    check("and no extra call", len(fc.calls), 1)

    empty = FetchConn([])
    try:
        fetch(empty, friday, ["BHP.AU", "005930.KS"])
        check("an empty answer raises rather than writing an empty file",
              False, True)
    except SystemExit as exc:
        check("and says it is the question, not the data",
              "does not look like" in str(exc), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
