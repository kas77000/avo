#!/usr/bin/env python3
"""The fixture's day, exported by AB's REAL extract.py on fake kdb.

make_fixture.py writes the zip by hand, in the order its tables are in.
This one hands the same day - the same master rows, prints, quotes,
equity_master fields and ladders - to extract.build() through fake
connections, so the zip is exactly what AB writes: market by market,
\\r\\n line ends, AB's manifest.  Running the three R jobs on it proves they
read what AB really writes, not what the fixture assumes it writes.

    python make_ab_zip.py C:/path/to/Phase1/AB C:/path/to/out
        -> C:/path/to/out/phase1-20260925.zip, and its staging folder

The CrossCode is tests/fixture/CrossCode.csv; give the R jobs that one.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_fixture as fx                                   # noqa: E402

DAY = dt.date(2026, 9, 25)


def rows_of(table):
    """make_fixture's [header, row, ...] -> dicts."""
    return [dict(zip(table[0], r)) for r in table[1:]]


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    ab, out = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    sys.path.insert(0, str(ab))
    import extract
    import logs
    import qattsource
    import refdata
    import settings

    T = qattsource.TIME_FIELD
    master = rows_of(fx.MASTER)
    #  sym_bpipe is the dotted code (7203.JT), which the first pass matches.
    for r in master:
        r["sym_bpipe"] = r["BloombergCode"].replace(" ", ".")
        r["sym_mbpipe"] = r["BloombergCode"] + " EQUITY"
    equity = {}
    for r in rows_of(fx.EQUITY):
        equity.setdefault(r["sym"], {f: (r[f] or None) for f in
                                     refdata.EQUITY_FIELDS})
    #  One ladder per sym: the fixture repeats 7203.JP's under both codes.
    ladders, owner = {}, {}
    for r in rows_of(fx.LADDERS):
        if owner.setdefault(r["sym"], r["BloombergCode"]) == r["BloombergCode"]:
            ladders.setdefault(r["sym"], []).append((r["price"],
                                                     r["ticksize"]))
    ids = {s: str(i) for i, s in enumerate(sorted(ladders), 1)}
    ticks = rows_of(fx.TICKS)
    quotes = rows_of(fx.QUOTE_ONLY)

    def em(q, *args):
        if q in (qattsource.MAXDATE_CLIENT_Q, qattsource.MAXDATE_SERVER_Q):
            return DAY
        s = set(args[-1]) if args else set()
        for query, col in ((qattsource.MASTER_BPIPE_Q, "sym_bpipe"),
                           (qattsource.MASTER_MBPIPE_Q, "sym_mbpipe"),
                           (qattsource.MASTER_SYM_Q, "sym")):
            if q == query:
                return [r for r in master if r[col] in s]
        if q == refdata.EQUITY_Q:
            return [dict(v, sym=k) for k, v in equity.items() if k in s]
        if q == refdata.IDS_Q:
            return [{"sym": k, "id": v} for k, v in ids.items() if k in s]
        if q == refdata.TBL_Q:
            return [{"id": ids[k], "price": Decimal(p), "ticksize": Decimal(t)}
                    for k, lad in ladders.items() for p, t in lad]
        raise AssertionError(f"equity_master was asked {q}")

    def qatt(q, *args):
        if q == qattsource.PARTITIONS_Q:
            return [DAY - dt.timedelta(days=1), DAY]
        if q == qattsource.COLUMNS_Q:
            return ["sym", "time", T, "price", "size", "cond", "ex"]
        if "from qatt" in q:
            s = set(args[-1])
            #  A blank condition is what kdb has; the extract writes it as
            #  #N/A N.A.
            got = [{"sym": r["sym"], T: dt.time.fromisoformat(r["time"]),
                    "price": Decimal(r["price"]), "size": int(r["size"]),
                    "cond": "" if r["cond"] == fx.NA else r["cond"],
                    "ex": r["ex"]} for r in ticks if r["sym"] in s]
            if "0!select size:sum size by" not in q:
                return got
            #  CONDENSED, as kdb answers qattsource.ticks_q: size summed by
            #  sym, second, price, cond, ex, in that sort order, the time a
            #  q second - which pykx's .py() hands back as a timedelta.
            summed = {}
            for r in got:
                t = r[T]
                key = (r["sym"], t.hour * 3600 + t.minute * 60 + t.second,
                       r["price"], r["cond"], r["ex"])
                summed[key] = summed.get(key, 0) + r["size"]
            return [{"sym": k[0], T: dt.timedelta(seconds=k[1]),
                     "price": k[2], "cond": k[3], "ex": k[4], "size": v}
                    for k, v in sorted(summed.items())]
        raise AssertionError(f"qatt was asked {q}")

    def quote(q, *args):
        s = set(args[-1])
        return [{"sym": r["sym"], "time": dt.time.fromisoformat(r["time"]),
                 "bid": Decimal(r["bid"]), "ask": Decimal(r["ask"])}
                for r in quotes if r["sym"] in s]

    cfg = dict(settings.DEFAULTS, CROSSCODE_PATH=str(HERE / "fixture" /
                                                     "CrossCode.csv"),
               EXPORT_DIR=str(out), SYM_CHUNK=2)
    out.mkdir(parents=True, exist_ok=True)
    conns = {"em": em, "qatt": qatt, "quote": quote,
             "reconnect": lambda: qatt}
    zip_path = extract.build(cfg, DAY, conns, logs.Log(),
                             today=dt.date(2026, 9, 28), fresh=True)
    print(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
