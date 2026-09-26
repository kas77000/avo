#!/usr/bin/env python3
"""The previous day's qatt prints for our universe, in ONE file that can be
carried to another machine.

    EXPORT_DIR/qatt-20260924.zip
        ticks.csv      sym,time,price,size,cond,ex     every print, kdb's clock
        master.csv     BloombergCode,sym,EQY_PRIM_EXCH_SHRT,...
        manifest.csv   key,value                        what, when, how many

qatt_dispatch.py, on the other machine, turns it into the tick store's
<code>/raw-<code>-<YYYYMMDD>.csv files.  That machine has no kdb, which is
why master.csv travels too: qatt is keyed on the COMPOSITE (7203.JP) and the
crosscode on the primary (7203 JT), and equity_master is the only thing that
links them.  Without it no file could be named.

THE UNIVERSE IS THE CROSSCODE.  Every crosscode code is resolved to a qatt
sym exactly as historical_ticks.py does it - equity_master in three passes,
config/markets.csv as the fallback - and only those syms are asked for.

THE PREVIOUS DAY IS THE NEWEST PARTITION BEFORE TODAY, not today-1: a
Monday run exports Friday, and a run after a holiday exports the last day
that traded.  --date picks another day: the newest partition on or before
it.

WHAT IS NOT DONE HERE.  No file naming, no collapse of venues, no timezone
conversion.  Those belong to the dispatch, which has the crosscode and the
config it will be writing against.  Every time in ticks.csv is kdb's own
clock, and manifest.csv says which zone that is.

    python qatt_export.py                     the previous day
    python qatt_export.py --date 2026-09-22   that day instead
    python qatt_export.py --log export.log    tee the log to a file
    python qatt_export.py --self-test         checks, no kdb, no files
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import crosscode
import logs
import marketcfg
import qattsource
import settings
import universe

HERE = Path(__file__).resolve().parent

TICKS, MASTER, MANIFEST = "ticks.csv", "master.csv", "manifest.csv"
TICK_COLUMNS = ["sym", "time", "price", "size", "cond", "ex"]
MASTER_COLUMNS = ["BloombergCode"] + list(qattsource.MASTER_FIELDS)

#  What "too big" looks like from here - see historical_ticks.run.
TOO_BIG = ("wsfull", "limit", "abort")


def bundle_name(date) -> str:
    return f"qatt-{date:%Y%m%d}.zip"


def pick_day(parts, today, date=None):
    """The partition to export: the newest before today, or the newest on
    or before --date.  None when qatt holds nothing that early."""
    if date:
        parts = [d for d in parts if d <= date]
    else:
        parts = [d for d in parts if d < today]
    return parts[-1] if parts else None


def candidates(rows, markets) -> dict:
    """{bloomberg code: (sym_bpipe, sym_mbpipe, sym) shapes} - the same
    three shapes historical_ticks.candidates hands equity_master."""
    out = {}
    for r in rows:
        comp = marketcfg.composite(r.market, markets)
        out[r.bbg] = (crosscode.bbg_dotted(r.bbg),
                      crosscode.bbg_full(r.bbg),
                      f"{r.ticker}.{comp}" if r.ticker and comp else "")
    return out


def wanted_syms(rows, master, markets) -> list:
    """Every qatt sym the crosscode resolves to, once each."""
    return sorted({s for s, _src in
                   (universe.resolve_sym(r, master, markets) for r in rows)
                   if s})


def too_big(e) -> bool:
    return (isinstance(e, (ConnectionError, TimeoutError))
            or any(k in str(e) for k in TOO_BIG))


def write_bundle(path, manifest: dict, master: dict, chunks) -> dict:
    """Write the zip, and return what went into it.

    `chunks` yields {sym: [(seconds or None, price, size, cond, ex), ...]} -
    qattsource.shape's answer, one chunk of syms at a time - so a whole day
    is never held at once.  One sym is only ever in one chunk, which keeps
    each sym's prints CONTIGUOUS in ticks.csv; the dispatch relies on that.

    Written as .part and renamed into place, so a zip under its real name
    is always a finished one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    stats = {"prints": 0, "syms with prints": 0}
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
        with z.open(TICKS, "w", force_zip64=True) as raw:
            fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            w = csv.writer(fh)
            w.writerow(TICK_COLUMNS)
            for by_sym in chunks:
                for sym, rows in by_sym.items():
                    if not rows:
                        continue
                    stats["syms with prints"] += 1
                    stats["prints"] += len(rows)
                    w.writerows((sym, "" if s is None else qattsource.hms(s),
                                 price, size, cond, ex)
                                for s, price, size, cond, ex in rows)
            fh.flush()
            fh.detach()
        z.writestr(MASTER, _csv([MASTER_COLUMNS] + [
            [bbg] + [m.get(f, "") for f in qattsource.MASTER_FIELDS]
            for bbg, m in sorted(master.items())]))
        z.writestr(MANIFEST, _csv([["key", "value"]] + [
            [k, str(v)] for k, v in {**manifest, **stats}.items()]))
    os.replace(part, path)
    return stats


def _csv(rows) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def fetch_chunks(conn, date, syms, cols, size, reconnect, log):
    """{sym: rows} per chunk of syms, in order.  A read that is too big is
    halved and asked again on a fresh connection, and the smaller size is
    kept - as in historical_ticks.run.  One sym that still fails stops the
    export: a bundle missing a name would look exactly like a quiet day."""
    size, pos, reads = max(1, int(size)), 0, 0
    while pos < len(syms):
        group = syms[pos:pos + size]
        t0 = time.monotonic()
        try:
            raw = qattsource.fetch_raw(conn, date, group, cols)
        except Exception as e:                              # noqa: BLE001
            if not too_big(e):
                raise
            if len(group) == 1:
                raise RuntimeError(f"qatt failed on ONE sym, {group[0]} on "
                                   f"{date}: {e}") from e
            size = max(1, len(group) // 2)
            log.warn(f"{len(group)} syms was too much for qatt "
                     f"({type(e).__name__}: {str(e)[:80]}); reconnecting "
                     f"and asking {size} at a time from here on")
            conn = reconnect()
            continue
        by_sym = qattsource.shape(raw)
        del raw
        pos += len(group)
        reads += 1
        log.info(f"read {reads}  {pos:,}/{len(syms):,} syms  "
                 f"{sum(len(v) for v in by_sym.values()):,} prints  "
                 f"{time.monotonic() - t0:.1f}s")
        yield by_sym


def connect_again(host, port, log, tries=6, wait=10.0):
    for n in range(1, tries + 1):
        time.sleep(wait)
        try:
            return qattsource.connect(host, port)
        except Exception as e:                              # noqa: BLE001
            log.warn(f"reconnect {n}/{tries} to {host}:{port} failed: "
                     f"{str(e)[:80]}")
    raise ConnectionError(f"{host}:{port} did not come back after {tries} "
                          f"tries")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The previous day's qatt prints, in one zip.")
    p.add_argument("--date", default="",
                   help="export the newest qatt day on or before this one, "
                        "YYYY-MM-DD, instead of the previous day")
    p.add_argument("--log", default="", help="tee the log to this file")
    p.add_argument("--self-test", action="store_true",
                   help="checks, with no kdb and no files")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()

    try:
        date = dt.date.fromisoformat(a.date) if a.date else None
    except ValueError:
        print(f"FAIL  --date {a.date!r} is not YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "EXPORT_DIR")
        em_host, em_port = settings.server(cfg, "EQUITY_MASTER_SERVER")
        q_host, q_port = settings.server(cfg, "QATT_SERVER")
    except settings.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    log = logs.Log(path=a.log or None)
    markets = marketcfg.load(HERE / "config" / "markets.csv")

    log.step(1, "crosscode")
    rows, dropped = crosscode.load(cfg["CROSSCODE_PATH"])
    log.kv("crosscode", logs.thousands(len(rows)) + " rows",
           str(cfg["CROSSCODE_PATH"]))
    for e in dropped:
        if e.rows:
            log.warn(f"{logs.thousands(len(e.rows))} rows dropped: {e.reason}")

    log.step(2, "equity_master")
    em = qattsource.connect(em_host, em_port)
    master_date = qattsource.resolve_master_date(em, dt.date.today())
    log.kv("equity_master date", master_date)
    cands = candidates(rows, markets)
    master, hits = {}, {"sym_bpipe": 0, "sym_mbpipe": 0, "sym": 0}
    keys = list(cands)
    for i in range(0, len(keys), int(cfg["MASTER_CHUNK"])):
        got = qattsource.fetch_master(
            em, master_date, {k: cands[k] for k in
                              keys[i:i + int(cfg["MASTER_CHUNK"])]})
        if got:
            master.update(got["rows"])
            for k, v in got["hits"].items():
                hits[k] += v
    log.kv("matched", f"{logs.thousands(len(master))} of "
                      f"{logs.thousands(len(cands))}",
           ", ".join(f"{k} {v}" for k, v in hits.items()))
    syms = wanted_syms(rows, master, markets)
    log.kv("syms to export", logs.thousands(len(syms)))

    log.step(3, "qatt")
    conn = qattsource.connect(q_host, q_port)
    parts = qattsource.partitions(conn)
    day = pick_day(parts, dt.date.today(), date)
    if day is None:
        log.fail(f"qatt has no partition "
                 f"{'on or before ' + str(date) if date else 'before today'}")
        return 1
    log.kv("day", str(day), f"newest partition {parts[-1]}")
    try:
        cols = qattsource.select_columns(qattsource.columns(conn))
    except ValueError as e:
        log.fail(str(e))
        return 1
    log.kv("columns asked for", ", ".join(cols))

    out = Path(cfg["EXPORT_DIR"]) / bundle_name(day)
    manifest = {"date": day, "equity_master date": master_date,
                "time column": qattsource.TIME_FIELD,
                "kdb timezone": cfg["KDB_TIMEZONE"],
                "crosscode": cfg["CROSSCODE_PATH"],
                "crosscode rows": len(rows), "syms asked": len(syms),
                "exported at": dt.datetime.now().isoformat(timespec="seconds")}
    stats = write_bundle(out, manifest, master, fetch_chunks(
        conn, day, syms, cols, cfg["SYM_CHUNK"],
        lambda: connect_again(q_host, q_port, log), log))

    log.step(4, "result")
    log.kv("syms with prints", f"{logs.thousands(stats['syms with prints'])} "
                               f"of {logs.thousands(len(syms))}")
    log.kv("prints", logs.thousands(stats["prints"]))
    log.kv("written", f"{out.stat().st_size / 1e6:,.1f} MB", str(out))
    log.close()
    return 0


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("qatt_export --self-test\n\nwhich day")
    D = dt.date
    parts = [D(2026, 9, 18), D(2026, 9, 21), D(2026, 9, 22)]
    check("the newest partition before today",
          pick_day(parts, D(2026, 9, 23)), D(2026, 9, 22))
    check("a Monday exports Friday",
          pick_day(parts, D(2026, 9, 21)), D(2026, 9, 18))
    check("today is never exported, even if it were a partition",
          pick_day(parts, D(2026, 9, 22)), D(2026, 9, 21))
    check("--date takes the newest on or before it",
          pick_day(parts, D(2026, 9, 23), D(2026, 9, 20)), D(2026, 9, 18))
    check("nothing that early is None",
          pick_day(parts, D(2026, 9, 23), D(2026, 9, 1)), None)

    print("\nthe universe")
    R = crosscode.Row
    rows = [R("", "", "7203 JT", "7203", "JT", "", "", "TYO-MAIN", ""),
            R("", "", "7203 JE", "7203", "JE", "", "", "JNX-MAIN", ""),
            R("", "", "ZZZ XX", "ZZZ", "XX", "", "", "NOWHERE", "")]
    markets = {"TYO-MAIN": marketcfg.Market("TYO-MAIN", "JP",
                                            "Tokyo Standard Time")}
    check("the third candidate shape is ticker dot composite",
          candidates(rows, markets)["7203 JT"][2], "7203.JP")
    master = {"7203 JE": {"sym": "7203.JP"}}
    check("three venue rows ask for one sym, and an unresolvable one none",
          wanted_syms(rows, master, markets), ["7203.JP"])

    print("\nreading qatt")
    asked = []

    def fake(query, date, syms):
        asked.append(len(syms))
        if len(syms) > 2:
            raise ConnectionResetError("dropped")
        return [{"sym": s, qattsource.TIME_FIELD: dt.time(9, 0, 1),
                 "price": 1.5, "size": 100, "cond": "", "ex": "T"}
                for s in syms]

    quiet = logs.Log(stamps=False, quiet=True)
    got = list(fetch_chunks(fake, D(2026, 9, 22), ["A", "B", "C", "D", "E"],
                            ["sym"], 4, lambda: fake, quiet))
    check("a dropped read is halved, and the smaller size kept",
          asked, [4, 2, 2, 1])
    check("every sym comes back once, shaped",
          [s for c in got for s in c], ["A", "B", "C", "D", "E"])
    check("a row is (seconds, price, size, cond, ex)",
          got[0]["A"], [(32401, "1.5", "100", "#N/A N.A.", "T")])

    print("\nthe bundle")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / bundle_name(D(2026, 9, 22))
        chunks = [{"7203.JP": [(32400, "3833", "100", "#N/A N.A.", "T"),
                               (None, "3834", "200", "T", "T")]},
                  {"9984.JP": [], "6758.JP": [(54000, "0.105", "1000",
                                               "", "T")]}]
        stats = write_bundle(out, {"date": D(2026, 9, 22)},
                             {"7203 JT": {"sym": "7203.JP",
                                          "ID_MIC_PRIM_EXCH": "XTKS"}},
                             iter(chunks))
        check("the name is the day", out.name, "qatt-20260922.zip")
        check("no .part is left behind",
              sorted(p.name for p in Path(tmp).iterdir()), [out.name])
        check("counts", stats, {"prints": 3, "syms with prints": 2})
        with zipfile.ZipFile(out) as z:
            check("three members", sorted(z.namelist()),
                  [MANIFEST, MASTER, TICKS])
            ticks = z.read(TICKS).decode().splitlines()
            master_csv = z.read(MASTER).decode().splitlines()
            manifest = dict(r for r in csv.reader(
                io.StringIO(z.read(MANIFEST).decode())))
        check("ticks: header, kdb's clock, a null time blank",
              ticks, ["sym,time,price,size,cond,ex",
                      "7203.JP,09:00:00,3833,100,#N/A N.A.,T",
                      "7203.JP,,3834,200,T,T",
                      "6758.JP,15:00:00,0.105,1000,,T"])
        check("master carries the code and every master field",
              master_csv, ["BloombergCode,sym,EQY_PRIM_EXCH_SHRT,"
                           "COMPOSITE_EXCH_CODE,ID_MIC_PRIM_EXCH",
                           "7203 JT,7203.JP,,,XTKS"])
        check("manifest says the day and the counts",
              (manifest["date"], manifest["prints"]), ("2026-09-22", "3"))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
