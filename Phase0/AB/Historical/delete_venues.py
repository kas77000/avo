#!/usr/bin/env python3
"""Delete the tick files historical_ticks.py wrote for one or more venues.

    python delete_venues.py SET-MAIN
    python delete_venues.py SET-MAIN NSI-MAIN --dry-run

A VENUE IS A FidessaMarket.  Every NewCrosscode.csv row on that market names
a folder in the store - its BloombergCode, spelt as ticksfile.folder() spells
it - and each of those folders loses its raw-<code>-<YYYYMMDD>.csv files and
is then removed.

THE PATHS ARE THE JOB'S OWN.  CROSSCODE_PATH and OUTPUT_DIR are read from
local_settings.py, the same two constants historical_ticks.py writes with, so
this cannot clean a different store from the one the job fills.

ONLY OUR FILES GO.  A file in a folder that is not a raw-...csv is left, and
so is its folder, and the run names it.  Two things are reported and NOT
touched: the venue's <VENUE>.zip from --compress_venues, and the miss cache.
While the zip exists historical_ticks.py counts its days as done.

    python delete_venues.py --self-test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import crosscode
import settings
import ticksfile


def folders_of(rows, venues) -> dict:
    """{venue: [folder, ...]} for each venue asked for, from the crosscode."""
    out = {v: [] for v in venues}
    for r in rows:
        if r.market in out:
            f = ticksfile.folder(r.bbg)
            if f not in out[r.market]:
                out[r.market].append(f)
    return out


def inside(root, name) -> bool:
    """Is <root>/<name> a folder directly inside the store?  A code that
    reads as '', '.' or '..' would otherwise delete from the store itself
    or above it."""
    root = Path(root).resolve()
    return (root / name).resolve().parent == root


def delete_folder(root, name, dry_run=False) -> dict:
    """The raw-...csv files in one folder, then the folder if it is empty.

    {"files": n deleted, "left": [names not ours], "removed": bool}."""
    d = Path(root) / name
    tally = {"files": 0, "left": [], "removed": False}
    if not inside(root, name) or not d.is_dir():
        return tally
    for e in sorted(d.iterdir()):
        if e.is_file() and ticksfile.parse_filename(e.name):
            if not dry_run:
                e.unlink()
            tally["files"] += 1
        else:
            tally["left"].append(e.name)
    if not tally["left"]:
        if not dry_run:
            d.rmdir()
        tally["removed"] = True
    return tally


def run(crosscode_path, out_dir, venues, dry_run=False) -> int:
    rows, _excluded = crosscode.load(crosscode_path)
    root = Path(out_dir)
    if not root.is_dir():
        print(f"OUTPUT_DIR {root} is not a directory")
        return 1
    status = 0
    note = "   (dry run, nothing deleted)" if dry_run else ""
    for venue, folders in folders_of(rows, venues).items():
        if not folders:
            print(f"{venue}: no row in {crosscode_path} has this "
                  f"FidessaMarket")
            status = 1
            continue
        files = removed = 0
        for f in folders:
            t = delete_folder(root, f, dry_run)
            files += t["files"]
            removed += t["removed"]
            if t["left"]:
                print(f"  {f}: kept, it also holds {', '.join(t['left'])}")
        print(f"{venue}: {files} file(s) deleted, {removed} of "
              f"{len(folders)} folder(s) removed{note}")
        z = root / f"{venue}.zip"
        if z.exists():
            print(f"  {z.name} is left in place - historical_ticks.py "
                  f"counts its days as done until it is removed")
    return status


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("delete_venues --self-test\n")
    NEW = ("#ReutersCode,FidessaCode,FidessaMarket,BloombergCode,Type\n"
           "PTT.BK,PTT.TH,SET-MAIN,PTT TB,\n"
           "LPNf.BK,LPN/F.TH,SET-MAIN,LPN/F TB,\n"
           "DOT.BK,DOT.TH,SET-MAIN,..,\n"
           "7203.T,7203.JP,TYO-MAIN,7203 JT,\n")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "NewCrosscode.csv"
        cc.write_text(NEW, encoding="utf-8")
        out = Path(d) / "ticks"
        for code in ("PTT TB", "LPN_F TB", "7203 JT"):
            (out / code).mkdir(parents=True)
            (out / code / f"raw-{code}-20260903.csv").write_text("x")
            (out / code / f"raw-{code}-20260904.csv").write_text("x")
        (out / "LPN_F TB" / "notes.txt").write_text("mine")

        rows, _ = crosscode.load(cc)
        check("a venue's folders are its rows' codes, spelt as on disk",
              folders_of(rows, ["SET-MAIN"])["SET-MAIN"][:2],
              ["PTT TB", "LPN_F TB"])
        check("and '..', or the '' safe() makes of it, is never a folder "
              "that gets emptied", (inside(out, ".."), inside(out, "")),
              (False, False))

        run(cc, out, ["SET-MAIN"], dry_run=True)
        check("a dry run deletes nothing",
              len(list(out.rglob("*.csv"))), 6)

        status = run(cc, out, ["SET-MAIN"])
        check("the venue's folder is gone", (out / "PTT TB").exists(), False)
        check("a folder holding a file not ours loses only the csv files",
              sorted(p.name for p in (out / "LPN_F TB").iterdir()),
              ["notes.txt"])
        check("another venue is untouched",
              len(list((out / "7203 JT").iterdir())), 2)
        check("the store itself is still there", out.is_dir(), True)
        check("and the run succeeded", status, 0)
        check("a venue no row has is an error",
              run(cc, out, ["XXX-MAIN"]), 1)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Delete the tick folders of one or more FidessaMarkets.")
    p.add_argument("venues", nargs="*", metavar="VENUE",
                   help="FidessaMarket, e.g. SET-MAIN")
    p.add_argument("--dry-run", action="store_true",
                   help="say what would go, delete nothing")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.venues:
        p.error("name at least one venue")
    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "OUTPUT_DIR")
    except settings.SettingError as e:
        print(e)
        return 1
    return run(cfg["CROSSCODE_PATH"], cfg["OUTPUT_DIR"], a.venues, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
