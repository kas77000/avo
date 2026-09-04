#!/usr/bin/env python3
"""Build limitUpDown.csv: ask Bloomberg for the band, except for Indonesia,
which we compute.

THE SPLIT.  Indonesia is pulled out of the universe and its band computed
from a tier table and a tick ladder; everything else comes from Bloomberg.
The division is declared in config/markets.csv, not written into this file:

  Source=bloomberg   MIN_LIMIT / MAX_LIMIT off B-PIPE
  Source=computed    band = f(previous close, tiers), rounded to the tick

BOTH BRANCHES USE THE REAL-TIME FIELD FAMILY, and they have to.  Our B-PIPE
entitlement does not serve the static reference fields - a probe on
2026-09-03 got "Field not permitted to datafeed users" for PX_MIN_LIMIT,
PX_MAX_LIMIT and PX_LAST while the real-time names answered on the same
request:

  wanted       barred (static)     used here
  the limits   PX_MAX/MIN_LIMIT    MAX_LIMIT / MIN_LIMIT
  the close    PX_YEST_CLOSE       PREV_CLOSE_VALUE_REALTIME, or the next
                                   candidate that answers
  last trade   PX_LAST             LAST_PRICE

  CrossCode.csv + markets.csv  ->  the universe, filtered by type, venue,
                                   cutoff and BloombergStatus, deduplicated
                                   on BloombergCode
  split by Source              ->  ask Bloomberg | compute
  temp file -> validate -> Test / Pilot / Prod

HOW THIS DIFFERS FROM v1.  v1 removes Bloomberg entirely and computes EVERY
market from rules against a kdb reference price.  v2 computes one market and
asks Bloomberg for the rest, so its bands.csv holds a single market and a
market whose rule nobody has written down is still publishable.

    python limit_up_down.py --self-test        arithmetic, no Bloomberg
    python limit_up_down.py --demo             a whole run on canned data
    python limit_up_down.py ""                 real run, publish nowhere
    python limit_up_down.py "Test|Pilot|Prod"  real run, publish
    python limit_up_down.py --compare OLD.csv  diff against another file

REPORT, NEVER SILENTLY DROP, and NOTHING PARTIALLY PUBLISHED - both carried
from v1, and both worth more here than there: when the numbers come from
outside, the count of names Bloomberg would not price IS the health check.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import bands
import bpipe
import crosscode
import mailer
import marketcfg
import ticks

OUT_HEADER = ["#ReutersCode", "BloombergCode", "LimitDate", "LimitUpPrice",
              "LimitDownPrice", "FidessaCode", "Venue"]

VALID_ENVS = ("Test", "Pilot", "Prod")

#  placeholders: override in local_settings.py beside this file
BPIPE_HOST = ""
BPIPE_PORT = ""
BPIPE_APP = ""
CROSSCODE_PATH = r"CHANGEME\CrossCode.csv"
#  Where spol_JKT.tsr lives.  Defaults to the copy shipped in config/ so the
#  job runs offline; point it at the ATS share so Indonesia's ladder cannot
#  drift from the trading system.
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


def _check_connection_settings():
    missing = [name for name, value in (("BPIPE_HOST", BPIPE_HOST),
                                        ("BPIPE_PORT", BPIPE_PORT),
                                        ("BPIPE_APP", BPIPE_APP))
               if not str(value).strip()]
    if missing:
        raise SystemExit(
            f"{', '.join(missing)} not set. Fill them in in "
            f"local_settings.py beside this script - copy "
            f"local_settings.py.example. Nothing here defaults to a working "
            f"value, because a job that connects somewhere other than where "
            f"you meant is worse than one that will not start.")


def _plain(d: Decimal) -> str:
    """No exponent, no trailing zeros: 1E+3 would be read as text by the ATS
    loader."""
    d = d.normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return format(d, "f")


def _out_row(r, low, high):
    return {"#ReutersCode": r.ric,
            "BloombergCode": r.bbg,
            "LimitDate": dt.date.today().isoformat(),
            "LimitUpPrice": _plain(high),
            "LimitDownPrice": _plain(low),
            "FidessaCode": r.fidessa_code,
            "Venue": r.venue_id}


#  Names shown per venue per reason before the line is truncated.  Enough to
#  paste one into a terminal and check it by hand; not so many that a report
#  about 3000 excluded names becomes unreadable.
SHOW_NAMES = 5


def _venue_summary(cfg, out, excluded):
    """One line per venue: published, excluded, and where its band comes from.

    EVERY CONFIGURED VENUE GETS A LINE, including one that published nothing.
    A venue used to appear only if it had output rows, so a market that lost
    its whole universe - the single thing most worth seeing - was the one
    thing the report could not say. A row of zeroes is the alarm."""
    published, dropped = {}, {}
    for r in out:
        published[r["Venue"]] = published.get(r["Venue"], 0) + 1
    for e in excluded:
        for venue, rows in e.by_venue().items():
            dropped[venue] = dropped.get(venue, 0) + len(rows)

    lines = [f"  {'venue':<12} {'published':>9} {'excluded':>9}  source"]
    for v in sorted(set(cfg.venues) | set(published) | set(dropped)):
        src = cfg.venues[v].source if v in cfg.venues else "not configured"
        flag = "   <- nothing published" if (published.get(v, 0) == 0
                                             and dropped.get(v, 0)) else ""
        lines.append(f"  {v:<12} {published.get(v, 0):9d} "
                     f"{dropped.get(v, 0):9d}  {src}{flag}")
    return lines


def _exclusion_lines(excluded):
    """One line per reason, then one per venue underneath it.

    The venue breakdown is the point.  "excluded 412 no MIN_LIMIT" does not
    tell you a whole market has vanished; the same count split by venue
    does, immediately."""
    lines = []
    for e in excluded:
        lines.append(f"  excluded {len(e.rows):6d}  {e.reason}")
        for venue, dropped in sorted(e.by_venue().items()):
            shown = ", ".join(str(d) for d in dropped[:SHOW_NAMES])
            more = (f" (+{len(dropped) - SHOW_NAMES} more)"
                    if len(dropped) > SHOW_NAMES else "")
            lines.append(f"    {venue:<12} {len(dropped):6d}  {shown}{more}")
    return lines


def _excluded(by_reason):
    return [crosscode.Excluded(reason=k, rows=v)
            for k, v in sorted(by_reason.items())]


def price_from_bloomberg(rows, values, refused=None):
    """The band straight off Bloomberg: MAX_LIMIT is the up price and
    MIN_LIMIT the down."""
    refused = refused or {}
    out, by_reason = [], {}

    def drop(reason, r):
        by_reason.setdefault(reason, []).append(
            crosscode.Dropped(ric=r.ric, bbg=r.bbg, venue_id=r.venue_id))

    for r in rows:
        if r.security in refused:
            drop(f"Bloomberg refused the security: {refused[r.security]}", r)
            continue
        fields = values.get(r.security)
        if fields is None:
            drop("no answer from Bloomberg", r)
            continue
        band, reason = bpipe.band_from(fields)
        if band is None:
            drop(reason, r)
            continue
        out.append(_out_row(r, band[0], band[1]))

    return out, _excluded(by_reason)


def price_computed(cfg, rows, closes, refused=None):
    """The band computed from a tier table, for a venue Bloomberg does not
    price for us.

    Order matters: pick the tier from the previous close, take the band,
    floor the down leg at MinPrice, and only THEN round to the tick.
    Rounding before flooring would move prices near a tier boundary.  The
    tick too is chosen from the close, not from the limit being rounded.

    Returns the rows, the exclusions, and a tally of which previous-close
    field actually answered - the run report prints it because which of the
    candidates works is not yet known."""
    refused = refused or {}
    out, by_reason = [], {}
    ref_fields = {}

    def drop(reason, r, detail=""):
        by_reason.setdefault(reason, []).append(
            crosscode.Dropped(ric=r.ric, bbg=r.bbg, venue_id=r.venue_id,
                              detail=detail))

    for r in rows:
        venue = cfg.venues[r.venue_id]
        if r.security in refused:
            drop(f"Bloomberg refused the security: {refused[r.security]}", r)
            continue
        fields = closes.get(r.security)
        if fields is None:
            drop("no answer from Bloomberg", r)
            continue

        ref, which = bpipe.prev_close_from(fields)
        if ref is None:
            drop("no previous close", r)
            continue
        ref_fields[which] = ref_fields.get(which, 0) + 1

        tick = None
        if venue.rounding != "none":
            tick = ticks.tick_for(cfg.ticks[r.venue_id], ref)
            if tick is None:
                drop("no tick tier for the previous close", r)
                continue
        try:
            high, low = bands.compute(cfg.bands[r.venue_id], r.ticker, ref,
                                      tick, venue.min_price, venue.rounding)
        except bands.BandError as e:
            drop(e.reason, r, e.detail)
            continue
        out.append(_out_row(r, low, high))

    return out, _excluded(by_reason), ref_fields


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
    """Differences between two output files, worst first: venue row counts,
    names present in one only, then prices that moved.  The cutover
    instrument - run it against yesterday's file, or against v1's."""
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


def run(envs_spec: str) -> int:
    mail = (SMTP_HOST, EMAIL_FROM, EMAIL_TO)
    session = None
    try:
        envs = parse_envs(envs_spec)
        _check_connection_settings()
        here = Path(__file__).resolve().parent
        cfg = marketcfg.load(here / "config", Path(TSR_DIR))
        now = dt.datetime.now().time()
        rows, excluded = crosscode.load(CROSSCODE_PATH, cfg.venues, now)

        if not rows:
            print("no venue has reached its cutoff yet - nothing to publish")
            for line in _exclusion_lines(excluded):
                print(line)
            return 0

        #  Indonesia is computed, the rest is asked for.  Two batched
        #  fetches, because the two halves want different fields.
        ask, compute = cfg.by_source(rows)

        def progress(what):
            def report(n, total, so_far):
                print(f"  {what}: batch {n}/{total}, {so_far} answered",
                      end="\r")
            return report

        session, identity = bpipe.connect(BPIPE_HOST, BPIPE_PORT, BPIPE_APP)
        print(f"connected to {BPIPE_HOST}:{BPIPE_PORT} as {BPIPE_APP}")
        print(f"{len(ask)} from Bloomberg, {len(compute)} computed")

        limits, refused, field_problems = {}, {}, {}
        if ask:
            limits, refused, field_problems = bpipe.fetch(
                session, identity, [r.security for r in ask],
                progress=progress("limits"))
            print()
        closes, close_refused, close_problems = {}, {}, {}
        if compute:
            closes, close_refused, close_problems = bpipe.fetch(
                session, identity, [r.security for r in compute],
                fields=bpipe.PREV_CLOSE_FIELDS,
                progress=progress("closes"))
            print()
    except Exception as e:                           # noqa: BLE001
        mailer.send("LimitUpDown FAILED", f"{type(e).__name__}: {e}", *mail)
        print(f"FATAL {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.stop()

    out, more = price_from_bloomberg(ask, limits, refused)
    computed_out, computed_excluded, ref_fields = price_computed(
        cfg, compute, closes, close_refused)
    out = out + computed_out
    excluded = list(excluded) + list(more) + list(computed_excluded)

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
    report.extend(_venue_summary(cfg, out, excluded))
    report.extend(_exclusion_lines(excluded))

    #  Which previous-close field answered is not yet known, so say it out
    #  loud every run until it is.
    for field, count in sorted(ref_fields.items(), key=lambda kv: -kv[1]):
        report.append(f"  close from {count:6d}  {field}")

    #  Same for the status fields: MARKET_STATUS is static and may not be
    #  served at all, and the real-time candidates have unknown values.  One
    #  run of this tells us which to point STATUS_FIELD at - and, crucially,
    #  shows at a glance if a session field reads CLOSED for everything,
    #  which is what it will do at 07:30.
    for field, seen in sorted(bpipe.status_tally(limits).items()):
        for value, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            report.append(f"  status     {count:6d}  {field} = {value}")
    for field, (message, count) in sorted({**field_problems,
                                           **close_problems}.items()):
        report.append(f"  field      {count:6d}  {field}: {message}")

    text = "\n".join(report)
    print(text)
    if excluded:
        mailer.send(f"LimitUpDown report - {len(out)} rows", text, *mail)
    return 0


def _row(ric, bbg, code, venue_id, status="ACTV"):
    return crosscode.Row(ric=ric, bbg=bbg, ticker=crosscode.ticker_of(bbg),
                         security=crosscode.security_name(bbg),
                         fidessa_code=code, venue_id=venue_id, status=status)


def demo() -> int:
    """A whole run on canned data, BOTH branches: no Bloomberg, no shares.

    The shipped config is used as-is, so this also proves markets.csv and
    bands.csv load and that the split lands where it should."""
    import io
    here = Path(__file__).resolve().parent
    cfg = marketcfg.load(here / "config", here / "config")

    rows = [_row("7203.T", "7203 JT", "7203.JP", "TYO-MAIN"),
            _row("7203.JNX", "7203 JE", "7203.JE", "JNX-MAIN"),
            _row("600001.SS", "600001 CG", "600001.CN", "SHA-MAIN"),
            _row("NOPX.SS", "NOPX CG", "NOPX.CN", "SHA-MAIN"),
            _row("HALF.SS", "HALF CG", "HALF.CN", "SHA-MAIN"),
            _row("WIDE.SS", "WIDE CG", "WIDE.CN", "SHA-MAIN"),
            _row("DEAD.SS", "DEAD CG", "DEAD.CN", "SHA-MAIN"),
            _row("BBCA.JK", "BBCA IJ", "BBCA.ID", "JKT-MAIN"),
            _row("TLKM.JK", "TLKM IJ", "TLKM.ID", "JKT-MAIN"),
            _row("TINY.JK", "TINY IJ", "TINY.ID", "JKT-MAIN"),
            _row("NOCL.JK", "NOCL IJ", "NOCL.ID", "JKT-MAIN")]
    ask, compute = cfg.by_source(rows)

    #  7203 is the real answer the probe got on 2026-09-03.  The PTS line
    #  carries the same limits, which is what makes JNX and CHJ publishable.
    limits = {"7203 JT Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 3130.0,
                                 "MARKET_STATUS": "ACTV"},
              "7203 JE Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 3130.0},
              "600001 CG Equity": {"MIN_LIMIT": 11.106, "MAX_LIMIT": 13.574,
                                   "LAST_PRICE": 12.34},
              "HALF CG Equity": {"MAX_LIMIT": 13.574},
              "WIDE CG Equity": {"MIN_LIMIT": 11.106, "MAX_LIMIT": 13.574,
                                 "LAST_PRICE": 99.0},
              "DEAD CG Equity": {"MIN_LIMIT": 11.106, "MAX_LIMIT": 13.574,
                                 "MARKET_STATUS": "DLST"}}
    refused = {"NOPX CG Equity": "Unknown/Invalid Security"}

    closes = {"BBCA IJ Equity": {"PREV_CLOSE_VALUE_REALTIME": 8000.0},
              "TLKM IJ Equity": {"PRICE_PREVIOUS_CLOSE_RT": 3000.0},
              "TINY IJ Equity": {"PREV_CLOSE_VALUE_REALTIME": 10.0},
              "NOCL IJ Equity": {}}

    out, excluded = price_from_bloomberg(ask, limits, refused)
    computed, computed_excluded, ref_fields = price_computed(cfg, compute,
                                                             closes)
    out = out + computed

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OUT_HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(out)
    print(buf.getvalue(), end="")

    print(f"--- {len(ask)} asked, {len(compute)} computed ---",
          file=sys.stderr)
    for field, count in sorted(ref_fields.items()):
        print(f"  close from {count}  {field}", file=sys.stderr)
    every = list(excluded) + list(computed_excluded)
    for line in _venue_summary(cfg, out + computed, every):
        print(line, file=sys.stderr)
    for line in _exclusion_lines(every):
        print(line, file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Build limitUpDown.csv from Bloomberg limits over "
                    "B-PIPE.")
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

    def row(ric, bbg, code, venue_id="TYO-MAIN"):
        return _row(ric, bbg, code, venue_id)

    print("limit_up_down --self-test\n\nasking Bloomberg for the band")
    rows = [row("7203.T", "7203 JT", "7203.JP"),
            row("9984.T", "9984 JT", "9984.JP"),
            row("HALF.T", "HALF JT", "HALF.JP"),
            row("NONE.T", "NONE JT", "NONE.JP"),
            row("GONE.T", "GONE JT", "GONE.JP")]
    values = {"7203 JT Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 3130.0},
              "9984 JT Equity": {"MIN_LIMIT": 9000.0, "MAX_LIMIT": 11000.0},
              "HALF JT Equity": {"MAX_LIMIT": 3833.0}}
    refused = {"GONE JT Equity": "Unknown/Invalid Security"}

    out, excl = price_from_bloomberg(rows, values, refused)
    check("the names Bloomberg priced", [r["#ReutersCode"] for r in out],
          ["7203.T", "9984.T"])
    check("MAX_LIMIT is the up price and MIN_LIMIT the down - Toyota, as the "
          "probe returned it",
          (out[0]["LimitUpPrice"], out[0]["LimitDownPrice"]),
          ("3833", "2433"))
    check("a name with no last price is still published",
          (out[1]["LimitUpPrice"], out[1]["LimitDownPrice"]),
          ("11000", "9000"))
    check("the venue lands in the output", out[0]["Venue"], "TYO-MAIN")
    check("so does the fidessa code", out[0]["FidessaCode"], "7203.JP")
    check("and the bloomberg code, without the ' Equity' we added to ask",
          out[0]["BloombergCode"], "7203 JT")
    check("the date is today", out[0]["LimitDate"], dt.date.today().isoformat())

    reasons = {e.reason: e.rows for e in excl}
    check("half a band is reported",
          [d.ric for d in reasons["no MIN_LIMIT"]], ["HALF.T"])
    check("and it names the BLOOMBERG code and venue, so a whole market "
          "going missing is visible in the report itself",
          [(d.bbg, d.venue_id) for d in reasons["no MIN_LIMIT"]],
          [("HALF JT", "TYO-MAIN")])
    check("a name Bloomberg said nothing about",
          [d.ric for d in reasons["no answer from Bloomberg"]],
          ["NONE.T"])
    check("a name Bloomberg refused, with its own words",
          [d.ric for d in reasons[
              "Bloomberg refused the security: Unknown/Invalid Security"]],
          ["GONE.T"])

    print("\ncomputing Indonesia from the tier table")
    #  The shipped config, not a fixture: if markets.csv or bands.csv is
    #  edited into something Indonesia cannot be priced from, this fails.
    here = Path(__file__).resolve().parent
    cfg = marketcfg.load(here / "config", here / "config")

    idn = [row("BBCA.JK", "BBCA IJ", "BBCA.ID", "JKT-MAIN"),
           row("TLKM.JK", "TLKM IJ", "TLKM.ID", "JKT-MAIN"),
           row("MIDS.JK", "MIDS IJ", "MIDS.ID", "JKT-MAIN"),
           row("TINY.JK", "TINY IJ", "TINY.ID", "JKT-MAIN"),
           row("NOCL.JK", "NOCL IJ", "NOCL.ID", "JKT-MAIN")]
    closes = {"BBCA IJ Equity": {"PREV_CLOSE_VALUE_REALTIME": 8000.0},
              "TLKM IJ Equity": {"PRICE_PREVIOUS_CLOSE_RT": 3000.0},
              "MIDS IJ Equity": {"PREV_CLOSE_VALUE_REALTIME": 100.0},
              "TINY IJ Equity": {"PREV_CLOSE_VALUE_REALTIME": 10.0},
              "NOCL IJ Equity": {}}
    cout, cexcl, ref_fields = price_computed(cfg, idn, closes)

    check("the names with a previous close",
          [r["#ReutersCode"] for r in cout],
          ["BBCA.JK", "TLKM.JK", "MIDS.JK"])
    check("8000 rupiah takes the 20% tier, and both legs land on the 25 "
          "tick exactly",
          (cout[0]["LimitUpPrice"], cout[0]["LimitDownPrice"]),
          ("9600", "6400"))
    check("3000 takes the 25% tier, rounded inward to the 10 tick",
          (cout[1]["LimitUpPrice"], cout[1]["LimitDownPrice"]),
          ("3750", "2250"))
    check("100 takes the 35% tier",
          (cout[2]["LimitUpPrice"], cout[2]["LimitDownPrice"]),
          ("135", "65"))
    creasons = {e.reason: e.rows for e in cexcl}
    check("a name under Rp 50 matches no tier and is REPORTED rather than "
          "quietly lost",
          [d.ric for d in creasons["no band tier for the previous close"]],
          ["TINY.JK"])
    check("the PRICE rides along as per-name detail, so forty names under "
          "Rp 50 are one reason with forty names, not forty reasons",
          str(creasons["no band tier for the previous close"][0]),
          "TINY.JK (TINY IJ) price 10")
    check("a name Bloomberg gave no close for",
          [d.ric for d in creasons["no previous close"]], ["NOCL.JK"])
    check("the run knows which field supplied each close - counting every "
          "close resolved, including TINY's, which was dropped afterwards "
          "for a reason that had nothing to do with the close",
          ref_fields, {"PREV_CLOSE_VALUE_REALTIME": 3,
                       "PRICE_PREVIOUS_CLOSE_RT": 1})

    print("\nthe two branches meet the same output contract")
    check("a computed row has the same seven columns as an asked one",
          sorted(cout[0]), sorted(out[0]))
    check("and its venue is the computed one", cout[0]["Venue"], "JKT-MAIN")
    mixed = idn + [row("600001.SS", "600001 CG", "600001.CN", "SHA-MAIN")]
    asked, computed = cfg.by_source(mixed)
    check("the shipped config sends only Indonesia down the computed path",
          sorted({r.venue_id for r in computed}), ["JKT-MAIN"])
    check("and China to Bloomberg",
          [r.venue_id for r in asked], ["SHA-MAIN"])

    print("\nprices are written plainly, never in exponent form")
    check("a big round number", _plain(D("1E+3")), "1000")
    check("trailing zeros go", _plain(D("10.500")), "10.5")
    check("an integral decimal loses its point", _plain(D("3833.00")), "3833")
    check("a small tick keeps its places", _plain(D("0.0100")), "0.01")

    print("\nvalidating before publication")
    good = [{"#ReutersCode": "A", "BloombergCode": "7203 JT",
             "LimitDate": "2026-09-03", "LimitUpPrice": "3833",
             "LimitDownPrice": "2433", "FidessaCode": "A.JP",
             "Venue": "TYO-MAIN"}]
    check("a good file has nothing to say", validate(good), [])
    check("an empty file is never published", validate([]),
          ["output is empty"])
    check("an inverted band is fatal",
          validate([dict(good[0], LimitUpPrice="60")]),
          ["A: LimitUpPrice 60 <= LimitDownPrice 2433"])
    check("so is a negative price",
          validate([dict(good[0], LimitDownPrice="-1")]),
          ["A: LimitDownPrice -1 is not positive"])
    check("so is a blank one", validate([dict(good[0], LimitUpPrice="")]),
          ["A: LimitUpPrice '' is not a number"])

    print("\nparsing the environment argument")
    check("the pipe separated form", parse_envs("Test|Pilot|Prod"),
          ["Test", "Pilot", "Prod"])
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
        check("the header is the ATS contract, unchanged from v1",
              text.splitlines()[0], ",".join(OUT_HEADER))
        check("one row", len(text.splitlines()), 2)
        check("unix line endings", "\r\n" in text, False)

    print("\ncomparing two output files")
    old = [{"#ReutersCode": "A.T", "Venue": "TYO-MAIN",
            "LimitUpPrice": "3833", "LimitDownPrice": "2433"},
           {"#ReutersCode": "B.T", "Venue": "TYO-MAIN",
            "LimitUpPrice": "200", "LimitDownPrice": "100"}]
    check("identical files have nothing to report", compare(old, old), [])
    check("a name only the old file has", compare(old, old[:1]),
          ["TYO-MAIN: 2 old, 1 new", "only in old: B.T"])
    check("a price that moved",
          compare(old, [dict(old[0], LimitUpPrice="3900"), old[1]]),
          ["A.T LimitUpPrice: old 3833, new 3900"])
    check("the same price written differently is not a difference",
          compare(old, [dict(old[0], LimitUpPrice="3833.0"), old[1]]), [])

    print("\nthe venue summary")
    cfg2 = marketcfg.load(Path(__file__).resolve().parent / "config",
                          Path(__file__).resolve().parent / "config")
    rows = [{"Venue": "TYO-MAIN"}, {"Venue": "TYO-MAIN"}]
    gone = [crosscode.Excluded(
        reason="Bloomberg refused the security: Security Entitlement Check "
               "Failed! EID(s) needed: 64487 or 64488",
        rows=[crosscode.Dropped("A.KL", "A MK", "KLS-MAIN"),
              crosscode.Dropped("B.KL", "B MK", "KLS-MAIN")])]
    lines = _venue_summary(cfg2, rows, gone)
    kls = [l for l in lines if "KLS-MAIN" in l]
    check("a venue that published NOTHING still gets a line - it is the one "
          "thing the report most needs to be able to say",
          len(kls), 1)
    check("with its excluded count on it", "2" in kls[0], True)
    check("and flagged, so a whole market going missing is not just a zero "
          "in a column", "nothing published" in kls[0], True)
    tyo = [l for l in lines if "TYO-MAIN" in l][0]
    check("a healthy venue is not flagged", "nothing published" in tyo, False)
    check("every configured venue appears, published or not",
          len([l for l in lines if "-MAIN" in l]), len(cfg2.venues))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
