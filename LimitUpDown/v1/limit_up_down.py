#!/usr/bin/env python3
"""Build limitUpDown.csv - the daily price band file the Nova ATS consumes.

No Bloomberg: every band in scope is arithmetic on a reference price, and
the rules live in config/, not in this file.

  CrossCode.csv + markets.csv  ->  the universe we owe a price for
  kdb                          ->  a reference price for each name
  bands.csv + tick tables      ->  the band itself
  temp file -> validate -> Test / Pilot / Prod

REPORT, NEVER SILENTLY DROP.  Every name we cannot price leaves the universe
with a reason attached, and the reasons are counted in the run report.  A
name that quietly vanishes is a name nobody investigates.

NOTHING PARTIALLY PUBLISHED.  The file is written to a temp path and
validated before a single environment is touched, so a bad run leaves
yesterday's file in place rather than half of today's.

  python limit_up_down.py --self-test        arithmetic, no kdb, no files
  python limit_up_down.py --demo             a whole run on canned data
  python limit_up_down.py ""                 real run, publish nowhere
  python limit_up_down.py "Test|Pilot|Prod"  real run, publish
  python limit_up_down.py --compare OLD.csv  diff against another file
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import bands
import crosscode
import mailer
import marketcfg
import ticks

OUT_HEADER = ["#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice",
              "LimitDownPrice", "FidessaCode", "Venue"]

VALID_ENVS = ("Test", "Pilot", "Prod")

#  placeholders: override in local_settings.py beside this file
KDB_HOST = "CHANGEME"
KDB_PORT = 5010
CROSSCODE_PATH = r"CHANGEME\CrossCode.csv"
TSR_DIR = str(Path(__file__).resolve().parent / "config")
OUT_TEMP = str(Path(__file__).resolve().parent / "out" / "limitUpDown.csv")
OUT_TEST = ""
OUT_PILOT = ""
OUT_PROD = ""
SMTP_HOST = "CHANGEME"
EMAIL_FROM = "CHANGEME"
EMAIL_TO = []


def _apply_local_settings():
    """Servers and paths live beside this file, not in it, so a git pull is
    always clean.  A name the script does not define is an ERROR: EMAIL_T0
    with a zero would otherwise sit there sending mail to no one."""
    path = Path(__file__).resolve().parent / "local_settings.py"
    if not path.is_file():
        return []
    ns = {}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"),
             {"__file__": str(path)}, ns)
    except Exception as e:                           # noqa: BLE001
        raise SystemExit(f"{path}: {type(e).__name__}: {e}")
    changed, unknown = [], []
    for k, v in ns.items():
        if k.startswith("_"):
            continue
        if k not in globals():
            unknown.append(k)
            continue
        globals()[k] = v
        changed.append(k)
    if unknown:
        raise SystemExit(
            f"{path} sets {', '.join(sorted(unknown))}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not a setting this "
            f"script has. A name that does nothing is worse than one that "
            f"errors.")
    return changed


def _plain(d: Decimal) -> str:
    """No exponent, no trailing zeros: 1E+3 would be read as text by the ATS
    loader."""
    d = d.normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return format(d, "f")


def price_universe(cfg, rows, refs):
    """rows -> output dicts, plus what could not be priced and why."""
    out = []
    by_reason = {}
    today = dt.date.today().isoformat()

    def drop(reason, ric):
        by_reason.setdefault(reason, []).append(ric)

    for r in rows:
        venue = cfg.venues[r.venue_id]
        ref = refs.get(r.ric)
        if ref is None:
            drop("no reference price", r.ric)
            continue
        #  Only a venue that rounds has a tick table at all - most publish
        #  the band as computed.  See the note in bands.py.
        tick = None
        if venue.rounding != "none":
            tick = ticks.tick_for(cfg.ticks[r.venue_id], ref)
            if tick is None:
                drop("no tick tier for the reference price", r.ric)
                continue
        try:
            up, down = bands.compute(cfg.bands[r.venue_id], r.ticker, ref,
                                     tick, venue.min_price, venue.rounding)
        except bands.BandError as e:
            drop(e.reason, r.ric)
            continue
        out.append({"#ReutersCode": r.ric,
                    "BloombergCode": r.bbg,
                    "LimitDate": today,
                    "LimitUpPrice": _plain(up),
                    "LimitDownPrice": _plain(down),
                    "FidessaCode": r.fidessa_code,
                    "Venue": r.venue_id})

    return out, [crosscode.Excluded(reason=k, rows=v)
                 for k, v in sorted(by_reason.items())]


def dedupe(out_rows):
    """Drop repeated BloombergCodes, keeping the first.  The list is
    reported and emailed rather than silently applied."""
    seen = set()
    kept, removed = [], []
    for r in out_rows:
        code = r["BloombergCode"]
        if code in seen:
            removed.append(r["#ReutersCode"])
        else:
            seen.add(code)
            kept.append(r)
    return kept, removed


def validate(out_rows):
    """Fatal problems only.  Empty list means the file may be published."""
    if not out_rows:
        return ["output is empty"]
    problems = []
    for r in out_rows:
        ric = r["#ReutersCode"]
        vals = {}
        for col in ("LimitUpPrice", "LimitDownPrice"):
            raw = r.get(col, "")
            try:
                vals[col] = Decimal(raw)
            except Exception:                        # noqa: BLE001
                problems.append(f"{ric}: {col} {raw!r} is not a number")
        if len(vals) < 2:
            continue
        for col in ("LimitUpPrice", "LimitDownPrice"):
            if vals[col] <= 0:
                problems.append(f"{ric}: {col} {vals[col]} is not positive")
        if vals["LimitUpPrice"] <= vals["LimitDownPrice"]:
            problems.append(
                f"{ric}: LimitUpPrice {vals['LimitUpPrice']} <= "
                f"LimitDownPrice {vals['LimitDownPrice']}")
    return problems


def write_csv(path, out_rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_HEADER, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)


def parse_envs(spec: str):
    out = [p.strip() for p in (spec or "").split("|") if p.strip()]
    bad = [e for e in out if e not in VALID_ENVS]
    if bad:
        raise ValueError(
            f"unknown environment(s) {bad}; expected any of {VALID_ENVS}")
    return out


def copy_to_envs(temp, envs, targets):
    import shutil
    failures = []
    for env in envs:
        target = targets.get(env)
        if not target:
            failures.append(f"{env}: no output path configured")
            continue
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temp, target)
        except OSError as e:
            failures.append(f"{env}: {e}")
    return failures


def compare(old_rows, new_rows):
    """Differences between two output files, worst first: venue row
    counts, names present in one only, then prices that moved."""
    out = []
    old = {r["#ReutersCode"]: r for r in old_rows}
    new = {r["#ReutersCode"]: r for r in new_rows}

    venues = sorted({r["Venue"] for r in old_rows} |
                    {r["Venue"] for r in new_rows})
    for v in venues:
        o = sum(1 for r in old_rows if r["Venue"] == v)
        n = sum(1 for r in new_rows if r["Venue"] == v)
        if o != n:
            out.append(f"{v}: {o} old, {n} new")

    for ric in sorted(set(old) - set(new)):
        out.append(f"only in old: {ric}")
    for ric in sorted(set(new) - set(old)):
        out.append(f"only in new: {ric}")

    for ric in sorted(set(old) & set(new)):
        for col in ("LimitUpPrice", "LimitDownPrice"):
            a, b = old[ric].get(col), new[ric].get(col)
            if a != b and Decimal(a) != Decimal(b):
                out.append(f"{ric} {col}: old {a}, new {b}")
    return out


def _fetch_refs(conn, cfg, rows):
    """One round trip per reference-price source, never per symbol."""
    today = dt.date.today().isoformat()
    wanted = {"close_print": [], "last_trade": []}
    for r in rows:
        wanted[cfg.venues[r.venue_id].ref_price].append(r.ric)
    import kdbsource
    refs = {}
    refs.update(kdbsource.close_prices(conn, today,
                                       sorted(set(wanted["close_print"]))))
    refs.update(kdbsource.last_prices(conn,
                                      sorted(set(wanted["last_trade"]))))
    return refs


def run(envs_spec: str) -> int:
    mail = (SMTP_HOST, EMAIL_FROM, EMAIL_TO)
    try:
        envs = parse_envs(envs_spec)
        here = Path(__file__).resolve().parent
        cfg = marketcfg.load(here / "config", Path(TSR_DIR))
        now = dt.datetime.now().time()
        rows, excluded = crosscode.load(CROSSCODE_PATH, cfg.venues, now)

        import kdbsource
        conn = kdbsource.connect(KDB_HOST, KDB_PORT)
        refs = _fetch_refs(conn, cfg, rows)
    except Exception as e:                           # noqa: BLE001
        mailer.send("LimitUpDown FAILED", f"{type(e).__name__}: {e}", *mail)
        print(f"FATAL {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    out, more = price_universe(cfg, rows, refs)
    excluded = list(excluded) + list(more)
    out, dropped = dedupe(out)

    problems = validate(out)
    if problems:
        body = ("Output failed validation, nothing published:\n\n"
                + "\n".join(problems[:200]))
        mailer.send("LimitUpDown FAILED validation", body, *mail)
        print(body, file=sys.stderr)
        return 1

    try:
        write_csv(OUT_TEMP, out)
    except OSError as e:
        mailer.send("LimitUpDown FAILED to write", str(e), *mail)
        print(f"FATAL {e}", file=sys.stderr)
        return 1

    targets = {"Test": OUT_TEST, "Pilot": OUT_PILOT, "Prod": OUT_PROD}
    failures = copy_to_envs(OUT_TEMP, envs, targets)
    if failures:
        mailer.send("LimitUpDown FAILED to publish", "\n".join(failures),
                    *mail)
        print("\n".join(failures), file=sys.stderr)
        return 1

    report = [f"{len(out)} rows -> {OUT_TEMP}",
              f"published to {', '.join(envs) if envs else 'nowhere'}"]
    for e in excluded:
        report.append(f"  excluded {len(e.rows):6d}  {e.reason}")
    if dropped:
        report.append(f"  deduped  {len(dropped):6d}  duplicate BloombergCode")
    text = "\n".join(report)
    print(text)
    if excluded or dropped:
        mailer.send(f"LimitUpDown report - {len(out)} rows", text, *mail)
    return 0


def demo() -> int:
    """A whole run on canned data: no kdb, no network shares, no licence."""
    import io
    from datetime import time
    venue = marketcfg.Venue(
        country="Indonesia", venue_id="JKT-MAIN", bbg_venue="IJ",
        bbg_composite="IJ", cutoff=time(7, 59), ref_price="close_print",
        tick_source="config", min_price=Decimal("50"), rounding="inward")
    cn = marketcfg.Venue(
        country="China", venue_id="SHA-MAIN", bbg_venue="CG",
        bbg_composite="CH", cutoff=time(9, 3), ref_price="close_print",
        tick_source="", min_price=None, rounding="none")
    cfg = marketcfg.Config(
        venues={"JKT-MAIN": venue, "SHA-MAIN": cn},
        bands={"JKT-MAIN": [bands.Tier("pct", "", Decimal("50"),
                                       Decimal("0.35"), Decimal("0.35")),
                            bands.Tier("pct", "", Decimal("200"),
                                       Decimal("0.25"), Decimal("0.25")),
                            bands.Tier("pct", "", Decimal("5000"),
                                       Decimal("0.20"), Decimal("0.20"))],
               "SHA-MAIN": [bands.Tier("pct", "688", Decimal("0"),
                                       Decimal("0.20"), Decimal("0.20")),
                            bands.Tier("pct", "", Decimal("0"),
                                       Decimal("0.10"), Decimal("0.10"))]},
        ticks={"JKT-MAIN": [(Decimal("0"), Decimal("1")),
                            (Decimal("200"), Decimal("2")),
                            (Decimal("5000"), Decimal("25"))]})

    def row(ric, bbg, code, venue_id):
        return crosscode.Row(ric=ric, bbg=bbg, ticker=bbg.rsplit(" ", 1)[0],
                             fidessa_code=code, venue_id=venue_id,
                             sec_type="Equity")

    rows = [row("BBCA.JK", "BBCA IJ", "BBCA.ID", "JKT-MAIN"),
            row("TLKM.JK", "TLKM IJ", "TLKM.ID", "JKT-MAIN"),
            row("TINY.JK", "TINY IJ", "TINY.ID", "JKT-MAIN"),
            row("NOPX.JK", "NOPX IJ", "NOPX.ID", "JKT-MAIN"),
            row("600001.SS", "600001 CG", "600001.CN", "SHA-MAIN"),
            row("688001.SS", "688001 CG", "688001.CN", "SHA-MAIN")]
    refs = {"BBCA.JK": Decimal("8000"), "TLKM.JK": Decimal("3000"),
            "TINY.JK": Decimal("10"),
            "600001.SS": Decimal("12.34"), "688001.SS": Decimal("12.34")}

    out, excluded = price_universe(cfg, rows, refs)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OUT_HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(out)
    print(buf.getvalue(), end="")
    print("--- excluded ---", file=sys.stderr)
    for e in excluded:
        print(f"  {len(e.rows)}  {e.reason}  {e.rows}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Build limitUpDown.csv for the Nova ATS.")
    p.add_argument("envs", nargs="?", default="",
                   help='pipe separated, e.g. "Test|Pilot|Prod"')
    p.add_argument("--self-test", action="store_true",
                   help="run the arithmetic checks and exit")
    p.add_argument("--demo", action="store_true",
                   help="run the whole pipeline on canned data and exit")
    p.add_argument("--compare", metavar="OLD_CSV",
                   help="diff the last output against another file")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.demo:
        return demo()

    _apply_local_settings()

    if a.compare:
        def read(path):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                return list(csv.DictReader(fh))
        diffs = compare(read(a.compare), read(OUT_TEMP))
        for d in diffs:
            print(d)
        print(f"\n{len(diffs)} difference(s)")
        return 0

    return run(a.envs)


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    from datetime import time
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def check_raises(name, fn, exc):
        nonlocal ok
        try:
            fn()
            got = "no exception"
        except Exception as e:                       # noqa: BLE001
            got = type(e)
        good = got is exc
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {exc!r}"))

    D = Decimal

    venue = marketcfg.Venue(
        country="Indonesia", venue_id="JKT-MAIN", bbg_venue="IJ",
        bbg_composite="IJ", cutoff=time(7, 59), ref_price="close_print",
        tick_source="spol_JKT.tsr", min_price=D("50"), rounding="inward")
    cfg = marketcfg.Config(
        venues={"JKT-MAIN": venue},
        bands={"JKT-MAIN": [bands.Tier("pct", "", D("50"), D("0.35"),
                                       D("0.35")),
                            bands.Tier("pct", "", D("200"), D("0.25"),
                                       D("0.25"))]},
        ticks={"JKT-MAIN": [(D("0"), D("1"))]})

    def row(ric, bbg, code):
        return crosscode.Row(ric=ric, bbg=bbg, ticker=bbg.rsplit(" ", 1)[0],
                             fidessa_code=code, venue_id="JKT-MAIN",
                             sec_type="Equity")

    print("limit_up_down --self-test\n\npricing the universe")
    rows = [row("BBCA.JK", "BBCA IJ", "BBCA.ID"),
            row("TLKM.JK", "TLKM IJ", "TLKM.ID"),
            row("TINY.JK", "TINY IJ", "TINY.ID"),
            row("NOPX.JK", "NOPX IJ", "NOPX.ID")]
    refs = {"BBCA.JK": D("100"), "TLKM.JK": D("1000"), "TINY.JK": D("10")}

    out, excl = price_universe(cfg, rows, refs)
    check("two names priced", [r["#ReutersCode"] for r in out],
          ["BBCA.JK", "TLKM.JK"])
    check("a 100 rupiah name gets the 35% tier",
          (out[0]["LimitUpPrice"], out[0]["LimitDownPrice"]), ("135", "65"))
    check("a 1000 rupiah name gets the 25% tier",
          (out[1]["LimitUpPrice"], out[1]["LimitDownPrice"]),
          ("1250", "750"))
    check("the venue lands in the output", out[0]["Venue"], "JKT-MAIN")
    check("so does the fidessa code", out[0]["FidessaCode"], "BBCA.ID")
    check("the date is today", out[0]["LimitDate"],
          dt.date.today().isoformat())
    reasons = {e.reason: e.rows for e in excl}
    check("a name under every tier is reported",
          reasons["no band tier for price 10"], ["TINY.JK"])
    check("so is a name kdb had no price for", reasons["no reference price"],
          ["NOPX.JK"])

    print("\na venue that does not round needs no tick table")
    kr_venue = marketcfg.Venue(
        country="Korea", venue_id="KSC-MAIN", bbg_venue="KP",
        bbg_composite="KS", cutoff=time(7, 30), ref_price="close_print",
        tick_source="", min_price=None, rounding="none")
    kr_cfg = marketcfg.Config(
        venues={"KSC-MAIN": kr_venue},
        bands={"KSC-MAIN": [bands.Tier("pct", "", D("0"), D("0.30"),
                                       D("0.30"))]},
        ticks={})          # note: EMPTY, and nothing goes looking in it
    kr_rows = [crosscode.Row("005930.KS", "005930 KP", "005930", "005930.KR",
                             "KSC-MAIN", "Equity")]
    kr_out, kr_excl = price_universe(kr_cfg, kr_rows,
                                     {"005930.KS": D("71300")})
    check("Korea prices with no tick table in sight",
          (kr_out[0]["LimitUpPrice"], kr_out[0]["LimitDownPrice"]),
          ("92690", "49910"))
    check("and nothing was excluded for want of a tick", kr_excl, [])

    print("\nprices are written plainly, never in exponent form")
    check("a big round number", _plain(D("1E+3")), "1000")
    check("trailing zeros go", _plain(D("10.500")), "10.5")
    check("an integral decimal loses its point", _plain(D("65.00")), "65")
    check("a small tick keeps its places", _plain(D("0.0100")), "0.01")

    print("\nduplicate bloomberg codes")
    dup = [{"#ReutersCode": "A.JK", "BloombergCode": "X IJ"},
           {"#ReutersCode": "B.JK", "BloombergCode": "X IJ"},
           {"#ReutersCode": "C.JK", "BloombergCode": "Y IJ"}]
    kept, removed = dedupe(dup)
    check("the first of each code survives",
          [r["#ReutersCode"] for r in kept], ["A.JK", "C.JK"])
    check("and the loser is named", removed, ["B.JK"])

    print("\nvalidating before publication")
    good = [{"#ReutersCode": "A", "BloombergCode": "X IJ",
             "LimitDate": "2026-09-01", "LimitUpPrice": "135",
             "LimitDownPrice": "65", "FidessaCode": "A.ID",
             "Venue": "JKT-MAIN"}]
    check("a good file has nothing to say", validate(good), [])
    check("an empty file is never published", validate([]), ["output is empty"])
    check("an inverted band is fatal",
          validate([dict(good[0], LimitUpPrice="60")]),
          ["A: LimitUpPrice 60 <= LimitDownPrice 65"])
    check("so is a negative price",
          validate([dict(good[0], LimitDownPrice="-1")]),
          ["A: LimitDownPrice -1 is not positive"])
    check("so is a blank one",
          validate([dict(good[0], LimitUpPrice="")]),
          ["A: LimitUpPrice '' is not a number"])

    print("\nparsing the environment argument")
    check("the pipe separated form", parse_envs("Test|Pilot|Prod"),
          ["Test", "Pilot", "Prod"])
    check("one environment", parse_envs("Pilot"), ["Pilot"])
    check("empty means publish nowhere - a dry run", parse_envs(""), [])
    check("whitespace and blanks are ignored", parse_envs(" Test | | Prod "),
          ["Test", "Prod"])
    check_raises("an unknown environment is refused, not skipped",
                 lambda: parse_envs("Test|Staging"), ValueError)

    print("\ncopying to environments")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "temp.csv"
        src.write_text("a,b\n1,2\n", encoding="utf-8")
        targets = {"Test": str(Path(d) / "t" / "out.csv"),
                   "Prod": str(Path(d) / "p" / "out.csv")}
        check("no failures on a good copy",
              copy_to_envs(src, ["Test", "Prod"], targets), [])
        check("and the content arrived",
              Path(targets["Test"]).read_text(encoding="utf-8"), "a,b\n1,2\n")
        check("an environment with no configured target is a failure, not a "
              "silent skip", copy_to_envs(src, ["Pilot"], targets),
              ["Pilot: no output path configured"])

    print("\nwriting the file")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "out.csv"
        write_csv(p, good)
        text = p.read_text(encoding="utf-8")
        check("the header is the ATS contract, in order",
              text.splitlines()[0], ",".join(OUT_HEADER))
        check("one row", len(text.splitlines()), 2)
        check("unix line endings", "\r\n" in text, False)

    print("\ncomparing two output files")
    old = [{"#ReutersCode": "A.JK", "Venue": "JKT-MAIN",
            "LimitUpPrice": "135", "LimitDownPrice": "65"},
           {"#ReutersCode": "B.JK", "Venue": "JKT-MAIN",
            "LimitUpPrice": "200", "LimitDownPrice": "100"}]
    check("identical files have nothing to report", compare(old, old), [])
    check("a name only the old file has", compare(old, old[:1]),
          ["JKT-MAIN: 2 old, 1 new", "only in old: B.JK"])
    check("a name only the new file has", compare(old[:1], old),
          ["JKT-MAIN: 1 old, 2 new", "only in new: B.JK"])
    check("a price that moved",
          compare(old, [dict(old[0], LimitUpPrice="140"), old[1]]),
          ["A.JK LimitUpPrice: old 135, new 140"])
    check("the same price written differently is not a difference",
          compare(old, [dict(old[0], LimitUpPrice="135.0"), old[1]]), [])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
