#!/usr/bin/env python3
"""Reference prices out of kdb.  This is the ONLY module that touches kdb,
and the only thing it fetches is a price.

Bloomberg used to answer "what is the limit" directly.  Nothing does now -
the limit is computed from a rule - so all kdb owes us is the number the
rule is struck off:

  close_print.price    the previous session's official closing print
  qatt.lastPrice       the last traded price, for venues struck intraday

target_stock is deliberately NOT used.  Its orgclose/adjclose are cached
values; close_print is the print itself.

ONE ROUND TRIP PER SOURCE, not per symbol.  A universe is tens of thousands
of names and a per-symbol query would take longer than the window between
the cutoff and the open.

pykx is imported inside connect(), so every other module - and both
--self-test and --demo - runs on a machine with no kdb and no q licence.

    python kdbsource.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

CLOSE_Q = ("{[d;s] select last price by sym from close_print "
           "where date=d, sym in s}")
LAST_Q = ("{[s] select last lastPrice by sym from qatt "
          "where sym in s, not null lastPrice}")


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


def _to_decimal(value):
    """kdb hands back numpy floats, pykx atoms or bytes depending on the
    build.  Anything that will not become a positive finite Decimal is not a
    price and is dropped - the caller reports the symbol as unpriced."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    return d if d.is_finite() and d > 0 else None


def _as_map(result, price_col: str):
    if result is None:
        return {}
    try:
        items = result.items()
    except AttributeError:
        items = dict(result).items()
    out = {}
    for sym, row in items:
        if isinstance(sym, (bytes, bytearray)):
            sym = sym.decode()
        try:
            value = row[price_col]
        except (TypeError, KeyError, IndexError):
            value = row
        price = _to_decimal(value)
        if price is not None:
            out[str(sym)] = price
    return out


def close_prices(conn, date, syms):
    if not syms:
        return {}
    return _as_map(conn(CLOSE_Q, date, list(syms)), "price")


def last_prices(conn, syms):
    if not syms:
        return {}
    return _as_map(conn(LAST_Q, list(syms)), "lastPrice")


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

    class FakeConn:
        """Stands in for pykx.SyncQConnection: called with (query, *args) and
        returns something dict-like keyed by sym."""

        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def __call__(self, query, *args):
            self.calls.append((query, args))
            return self.payload

    print("kdbsource --self-test\n\nconverting what kdb returns")
    check("a plain number", _to_decimal(123.45), D("123.45"))
    check("a string", _to_decimal("10"), D("10"))
    check("bytes, as a symbol column can arrive", _to_decimal(b"7.5"),
          D("7.5"))
    check("a null is not a price", _to_decimal(None), None)
    check("nor is zero", _to_decimal(0), None)
    check("nor is a negative", _to_decimal(-1), None)
    check("nor is a nan", _to_decimal(float("nan")), None)
    check("nor is an infinity", _to_decimal(float("inf")), None)
    check("nor is nonsense", _to_decimal("n/a"), None)

    print("\nshaping the result into sym -> price")
    check("a dict of dicts, as a keyed table converts to",
          _as_map({"BBCA": {"price": 8000.0}, "TLKM": {"price": 3000.0}},
                  "price"),
          {"BBCA": D("8000.0"), "TLKM": D("3000.0")})
    check("bytes keys are decoded to str",
          _as_map({b"BBCA": {"price": 1.0}}, "price"), {"BBCA": D("1.0")})
    check("unpriceable rows are left out entirely, not zero filled",
          _as_map({"A": {"price": 0.0}, "B": {"price": 5.0}}, "price"),
          {"B": D("5.0")})
    check("an empty result is an empty map", _as_map({}, "price"), {})
    check("a null result is an empty map", _as_map(None, "price"), {})

    print("\nthe queries")
    c = FakeConn({"BBCA": {"price": 8000.0}})
    check("close_prices returns the map",
          close_prices(c, "2026-09-01", ["BBCA"]), {"BBCA": D("8000.0")})
    check("and asks close_print exactly once", [q for q, _ in c.calls],
          [CLOSE_Q])
    check("passing the date and the symbol list", c.calls[0][1],
          ("2026-09-01", ["BBCA"]))
    check("no symbols means no round trip at all",
          close_prices(FakeConn(None), "2026-09-01", []), {})

    c = FakeConn({"ABC": {"lastPrice": 12.5}})
    check("last_prices reads the lastPrice column", last_prices(c, ["ABC"]),
          {"ABC": D("12.5")})
    check("from qatt", [q for q, _ in c.calls], [LAST_Q])
    check("and it too skips an empty universe",
          last_prices(FakeConn(None), []), {})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
