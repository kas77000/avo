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

MAXDATE_Q = "{[d] exec max date from equity_master where date<=d}"

# `$s casts the python list of strings to a symbol vector, which is what the
# sym column is.
FETCH_Q = ("{[d;s] select " + ",".join(FIELDS) + " by sym from equity_master "
           "where date=d, sym in `$s}")


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


def resolve_date(conn, requested):
    got = conn(MAXDATE_Q, requested)
    if got is None:
        raise SystemExit(
            f"equity_master has no rows on or before {requested}.  "
            "Check the server and the date.")
    try:
        got = got.py()
    except AttributeError:
        pass
    if got is None:
        raise SystemExit(
            f"equity_master has no rows on or before {requested}.  "
            "Check the server and the date.")
    return got


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


def fetch(conn, date, syms) -> dict:
    if not syms:
        return {}
    result = conn(FETCH_Q, date, list(syms))
    if result is None:
        return {}
    try:
        items = result.items()
    except AttributeError:
        items = dict(result).items()
    return {_text(sym): {f: _cell(row, f) for f in FIELDS}
            for sym, row in items}


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
        def __init__(self, have):
            self.have, self.calls = have, []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            if "max date" in q:
                return max((d for d in self.have if d <= args[0]),
                           default=None)
            return {}

    friday = datetime.date(2026, 8, 28)
    monday = datetime.date(2026, 8, 31)
    c = Conn([friday])
    check("a Sunday request rolls back to Friday",
          resolve_date(c, monday - datetime.timedelta(days=1)), friday)

    c = Conn([monday])
    check("a date that has rows is used as-is", resolve_date(c, monday), monday)

    c = Conn([])
    try:
        resolve_date(c, monday)
        check("raised on an empty table", False, True)
    except SystemExit as exc:
        check("and says the table is empty", "no rows" in str(exc), True)

    print("\nfetching")

    class FetchConn:
        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return {"BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9}}

    fc = FetchConn()
    got = fetch(fc, friday, ["BHP.AU"])
    check("one round trip, not one per symbol", len(fc.calls), 1)
    check("the date and the sym list are both passed",
          fc.calls[0][1], (friday, ["BHP.AU"]))
    check("the result is keyed by sym", sorted(got), ["BHP.AU"])
    check("no syms means no round trip at all", fetch(fc, friday, []), {})
    check("and no extra call", len(fc.calls), 1)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
