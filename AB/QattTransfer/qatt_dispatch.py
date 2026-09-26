#!/usr/bin/env python3
"""A qatt_export.py zip -> the tick store, one file per name per day.

    python qatt_dispatch.py D:\\drop\\qatt-20260924.zip

    OUTPUT_DIR/7203 JP/raw-7203 JP-20260924.csv
    #Time,Last,Volume,Condition,Exchange,MicCode,Tokyo Standard Time
    09:00:01,3833,100,#N/A N.A.,T,XTKS

The same files, in the same folders, that historical_ticks.py writes - and
through the same code: universe.build names each file from the crosscode,
config/composites.csv and the equity_master rows the zip carries, and
ticksfile writes it.  A name's folder is created when it has none.

NO kdb HERE.  The zip holds equity_master as the export saw it, which is
what links a qatt sym (7203.JP) to the crosscode's code (7203 JT).

WHAT IS DONE ON THIS SIDE:

  naming    which crosscode row names the file, and whether its market is
            written under the composite (RIO AT -> RIO AU)
  clock     every print moved from kdb's zone - the manifest says which -
            into its market's own, per config/markets.csv
  skip      a file that already exists is left alone and counted

A name with no prints in the zip gets no file, as in historical_ticks.py.
A sym in the zip that this crosscode does not carry is counted and dropped.

    python qatt_dispatch.py ZIP               dispatch it
    python qatt_dispatch.py ZIP --log d.log   tee the log to a file
    python qatt_dispatch.py --self-test       the whole thing on canned data
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

import crosscode
import logs
import marketcfg
import qattsource
import settings
import ticksfile
import universe
from qatt_export import MANIFEST, MASTER, TICKS

HERE = Path(__file__).resolve().parent


def read_manifest(z) -> dict:
    return {r[0]: r[1] for r in csv.reader(
        io.StringIO(z.read(MANIFEST).decode("utf-8"))) if len(r) == 2}


def read_master(z) -> dict:
    """{bloomberg code: equity_master row}, as universe.build wants it."""
    rows = csv.DictReader(io.StringIO(z.read(MASTER).decode("utf-8")))
    return {r["BloombergCode"]: {f: r.get(f, "")
                                 for f in qattsource.MASTER_FIELDS}
            for r in rows}


def by_sym(z):
    """(sym, [row, ...]) per sym, streamed from ticks.csv.

    The export writes each sym's prints together, so one sym is held in
    memory at a time.  A sym that comes back after another has started
    means the zip was not written that way, and is refused rather than
    written twice."""
    done, sym, rows = set(), None, []
    with z.open(TICKS) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8",
                                             newline=""))
        next(reader)
        for r in reader:
            if r[0] != sym:
                if sym is not None:
                    yield sym, rows
                if r[0] in done:
                    raise ValueError(f"{TICKS}: {r[0]} appears twice, apart; "
                                     f"this zip was not written by "
                                     f"qatt_export.py")
                done.add(r[0])
                sym, rows = r[0], []
            rows.append(r[1:])
    if sym is not None:
        yield sym, rows


def seconds(cell):
    """'09:00:01' -> 32401, '' -> None."""
    if not cell:
        return None
    h, m, s = cell.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def venue_of(name) -> str:
    """The FidessaMarket of the row that names the file - historical_ticks'
    rule for which market's clock a name is converted into."""
    for r in name.rows:
        if r.bbg == name.crosscode_bbg:
            return r.market
    return name.rows[0].market if name.rows else ""


def tz_label(name, markets) -> str:
    """The seventh header cell - historical_ticks.tz_label."""
    m = markets.get(name.rows[0].market) if name.rows else None
    return getattr(m, "time_zone", "") if m else ""


def dispatch(zip_path, rows, markets, composites, out_dir, log) -> dict:
    """Write every name's file for the zip's day.  Returns the counts."""
    stats = {"files written": 0, "already there": 0, "new folders": 0,
             "prints": 0, "syms not in the crosscode": 0}
    with zipfile.ZipFile(zip_path) as z:
        manifest = read_manifest(z)
        date = dt.date.fromisoformat(manifest["date"])
        source = manifest.get("kdb timezone", "")
        master = read_master(z)
        log.kv("day", str(date), f"exported {manifest.get('exported at', '?')}")
        log.kv("in the zip", f"{logs.thousands(manifest.get('prints', 0))} "
                             f"prints, {logs.thousands(len(master))} "
                             f"equity_master rows")
        log.kv("kdb's clock", source or "(not given)", "from the manifest")

        names, excluded, tally = universe.build(rows, master, markets,
                                                composites)
        for e in excluded:
            log.warn(f"{logs.thousands(len(e.rows))} excluded: {e.reason}")
        log.kv("names", logs.thousands(len(names)),
               f"{logs.thousands(len(rows))} crosscode rows collapsed")
        if tally["markets.csv"]:
            log.warn(f"{tally['markets.csv']} names had no equity_master row "
                     f"in the zip and took config/markets.csv's composite; "
                     f"they carry no MIC")
        of_sym = {n.sym: n for n in names}

        shifts, told = {}, set()

        def shift(name):
            market = venue_of(name)
            if market not in shifts:
                target = marketcfg.tz_of(markets, market)
                if not target or not source:
                    if market not in told:
                        told.add(market)
                        log.warn(f"{market or '(no market)'}: no TimeZone to "
                                 f"convert to; its files keep kdb's clock")
                    shifts[market] = 0
                else:
                    shifts[market] = marketcfg.shift_seconds(date, source,
                                                             target)
            return shifts[market]

        seen = set()
        for sym, prints in by_sym(z):
            name = of_sym.get(sym)
            if name is None:
                stats["syms not in the crosscode"] += 1
                continue
            seen.add(sym)
            path = ticksfile.path(out_dir, name.crosscode_bbg, name.bbg, date)
            if path.exists():
                stats["already there"] += 1
                continue
            if not path.parent.exists():
                stats["new folders"] += 1
            stamp = qattsource.clock_maker(shift(name))
            stats["prints"] += ticksfile.write_rows(
                path, [(stamp(seconds(t)), price, size, cond, ex, name.mic)
                       for t, price, size, cond, ex in prints],
                tz_label(name, markets))
            stats["files written"] += 1
    stats["names with no prints"] = len(names) - len(seen)
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="A qatt_export.py zip -> one file per name, per day.")
    p.add_argument("zip", nargs="?", help="the qatt-YYYYMMDD.zip to dispatch")
    p.add_argument("--log", default="", help="tee the log to this file")
    p.add_argument("--self-test", action="store_true",
                   help="the whole thing on canned data, no files kept")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.zip:
        p.error("give the zip to dispatch")
    if not Path(a.zip).is_file():
        print(f"FAIL  {a.zip} is not a file", file=sys.stderr)
        return 2
    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "OUTPUT_DIR")
    except settings.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    log = logs.Log(path=a.log or None)
    markets = marketcfg.load(HERE / "config" / "markets.csv")
    composites = marketcfg.load_composites(HERE / "config" / "composites.csv")

    log.step(1, "crosscode")
    rows, dropped = crosscode.load(cfg["CROSSCODE_PATH"])
    log.kv("crosscode", logs.thousands(len(rows)) + " rows",
           str(cfg["CROSSCODE_PATH"]))
    for e in dropped:
        if e.rows:
            log.warn(f"{logs.thousands(len(e.rows))} rows dropped: {e.reason}")

    log.step(2, "dispatch")
    log.kv("zip", str(a.zip))
    try:
        stats = dispatch(a.zip, rows, markets, composites, cfg["OUTPUT_DIR"],
                         log)
    except (KeyError, ValueError, zipfile.BadZipFile) as e:
        log.fail(f"{a.zip}: {e}")
        return 1

    log.step(3, "result")
    for k, v in stats.items():
        log.kv(k, logs.thousands(v))
    log.kv("into", str(cfg["OUTPUT_DIR"]))
    log.close()
    return 0


def self_test() -> int:
    import tempfile
    from qatt_export import write_bundle
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("qatt_dispatch --self-test\n")
    D = dt.date(2026, 9, 22)
    R = crosscode.Row
    rows = [R("", "", "7203 JT", "7203", "JT", "", "", "TYO-MAIN", ""),
            R("", "", "7203 JE", "7203", "JE", "", "", "JNX-MAIN", ""),
            R("", "", "EAU AU", "EAU", "AU", "", "", "ASX-MAIN", ""),
            R("", "", "0005 HK", "0005", "HK", "", "", "HKG-MAIN", "")]
    markets = marketcfg.load(HERE / "config" / "markets.csv")
    composites = marketcfg.load_composites(HERE / "config" / "composites.csv")
    master = {"7203 JT": {"sym": "7203.JP", "EQY_PRIM_EXCH_SHRT": "JT",
                          "COMPOSITE_EXCH_CODE": "JP",
                          "ID_MIC_PRIM_EXCH": "XTKS"},
              "EAU AU": {"sym": "EAU.AU", "EQY_PRIM_EXCH_SHRT": "AU",
                         "COMPOSITE_EXCH_CODE": "AU",
                         "ID_MIC_PRIM_EXCH": "XASX"},
              "0005 HK": {"sym": "0005.HK", "EQY_PRIM_EXCH_SHRT": "HK",
                          "COMPOSITE_EXCH_CODE": "HK",
                          "ID_MIC_PRIM_EXCH": "XHKG"}}
    chunks = [{"7203.JP": [(32400, "3833", "100", "#N/A N.A.", "T"),
                           (32401, "3834", "200", "T", "T")],
               "EAU.AU": [(28800, "0.105", "1000", "#N/A N.A.", "T")],
               "NOTOURS.XX": [(30000, "1", "1", "#N/A N.A.", "T")]}]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zp = tmp / "qatt-20260922.zip"
        write_bundle(zp, {"date": D, "kdb timezone": "China Standard Time",
                          "prints": 4}, master, iter(chunks))
        out = tmp / "ticks"
        log = logs.Log(stamps=False, quiet=True)
        stats = dispatch(zp, rows, markets, composites, out, log)

        tokyo = out / "7203 JP" / "raw-7203 JP-20260922.csv"
        sydney = out / "EAU AU" / "raw-EAU AU-20260922.csv"
        check("three venue rows make one file, under the composite",
              sorted(p.name for p in out.iterdir()), ["7203 JP", "EAU AU"])
        check("Tokyo: its own header, +1h from kdb, the MIC on every row",
              tokyo.read_text().splitlines(),
              ["#Time,Last,Volume,Condition,Exchange,MicCode,"
               "Tokyo Standard Time",
               "10:00:00,3833,100,#N/A N.A.,T,XTKS",
               "10:00:01,3834,200,T,T,XTKS"])
        check("Sydney in September is +2h from kdb",
              sydney.read_text().splitlines()[1],
              "10:00:00,0.105,1000,#N/A N.A.,T,XASX")
        check("counts", stats,
              {"files written": 2, "already there": 0, "new folders": 2,
               "prints": 3, "syms not in the crosscode": 1,
               "names with no prints": 1})

        tokyo.write_text("kept\n")
        stats = dispatch(zp, rows, markets, composites, out, log)
        check("a second run skips what is there and changes nothing",
              (stats["files written"], stats["already there"],
               tokyo.read_text()), (0, 2, "kept\n"))

        bad = tmp / "bad.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr(TICKS, "sym,time,price,size,cond,ex\n"
                              "A.X,09:00:00,1,1,,T\nB.X,09:00:00,1,1,,T\n"
                              "A.X,09:00:01,1,1,,T\n")
        try:
            with zipfile.ZipFile(bad) as z:
                list(by_sym(z))
            got = "accepted"
        except ValueError as e:
            got = "refused" if "A.X appears twice" in str(e) else str(e)
        check("a sym split in two is refused, not written twice", got,
              "refused")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
