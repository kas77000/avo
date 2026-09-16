#!/usr/bin/env python3
"""Build limitUpDown.csv: ask Bloomberg for the band, or compute it.

THE SPLIT IS CONFIG, NOT CODE.  Every venue names its own source in
config/markets.csv, and any of them can be switched by editing one word:

  Source=bloomberg   MIN_LIMIT / MAX_LIMIT off B-PIPE
  Source=computed    band = f(previous close, tiers), rounded to the tick

EACH SOURCE IS OPENED ONLY IF IT HAS WORK.  Switch every venue to computed
and this never touches B-PIPE; leave them all on bloomberg and it never
touches kdb.  That is what makes a market B-PIPE will not serve us - an
entitlement refusal, say - still publishable.

KDB RUNS FIRST.  The Bloomberg fetch is the long part, sixteen thousand
names and minutes of it, so putting it ahead of kdb meant every kdb fault
cost a whole run to see once.  The cheap, fragile side fails fast now.

WHERE THE COMPUTED CLOSE COMES FROM, and why not Bloomberg.  equity_master
in kdb is the Bloomberg Data Licence feed, so its PX_LAST on the previous
partition is a close of the same lineage as PX_YEST_CLOSE and ADJUSTED for
corporate actions - reached without a real-time entitlement.  The real-time
alternatives were worse: PX_YEST_CLOSE is static and refused to this
subscription, and PREV_CLOSE_VALUE_REALTIME is only served for names the
plant will serve at all, which is exactly the set that fails.

THE ASKED SIDE USES THE REAL-TIME FIELD FAMILY, and has to.  A probe on
2026-09-03 got "Field not permitted to datafeed users" for PX_MIN_LIMIT,
PX_MAX_LIMIT and PX_LAST while the real-time names answered on the same
request:

  wanted       barred (static)     used here
  the limits   PX_MAX/MIN_LIMIT    MAX_LIMIT / MIN_LIMIT
  last trade   PX_LAST             LAST_PRICE

  CrossCode.csv + markets.csv  ->  the universe, filtered by type, venue,
                                   cutoff and BloombergStatus, deduplicated
                                   on BloombergCode
  split by Source              ->  ask Bloomberg | compute from kdb
  + India's BSE secondary      ->  asked with the rest, published twice
  temp file -> validate -> Test / Pilot / Prod

TWO MARKETS NEED MORE THAN A CONFIG ROW, and india.py is why they are in
scope at all.  Thailand's /F and /Q lines are dropped in crosscode.py;
India's ATS-configured names are dropped there too, off a strategy file the
venue names in its ExcludeFile column, and India's BSE listings are
synthesised here because they have no crosscode line of their own.

HOW THIS DIFFERS FROM v1.  v1 computes EVERY market and has no Bloomberg at
all.  v2 can do the same - switch every venue - but does not have to, so a
market whose rule nobody has written down is still publishable.

    python limit_up_down.py --self-test        arithmetic, no Bloomberg
    python limit_up_down.py --demo             a whole run on canned data
    python limit_up_down.py ""                 real run, publish nowhere
    python limit_up_down.py "Test|Pilot|Prod"  real run, publish
    python limit_up_down.py --compare OLD.csv  diff against another file,
                                               printed and written to
                                               compare-report.csv
    python limit_up_down.py --kdb-check        only the kdb path, verbosely

REPORT, NEVER SILENTLY DROP, and NOTHING PARTIALLY PUBLISHED - both carried
from v1, and both worth more here than there: when the numbers come from
outside, the count of names a source would not price IS the health check.
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
import india
import kdbclose
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
#  host:port of the kdb holding equity_master.  Only a run with a computed
#  venue needs it; a config where Bloomberg prices everything never connects.
EQUITY_MASTER_SERVER = "CHANGEME:5010"
CROSSCODE_PATH = r"CHANGEME\CrossCode.csv"
#  The ATS share: spol_JKT.tsr and India's two .stra strategy files.
#  Defaults to the copy of the ladder shipped in config/ so the job runs
#  offline; point it at the share so Indonesia's ladder cannot drift from
#  the trading system - and so India's exclusions are read at all.
TSR_DIR = str(Path(__file__).resolve().parent / "config")
OUT_TEMP = str(Path(__file__).resolve().parent / "out" / "limitUpDown.csv")

#  Where --compare writes its full list of differences.  The printed lines
#  are a summary; this is the record.
COMPARE_REPORT = "compare-report.csv"
COMPARE_COLUMNS = ["status", "venue", "code", "column", "old", "new"]
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


#  Everything this script calls on its sibling modules.  Checked at startup,
#  never at the end.
REQUIRED = {
    "bpipe": ("band_from", "connect", "eids_in", "ENTITLEMENT_MARKER",
              "fetch", "status_tally"),
    "kdbclose": ("connect", "parse_server", "resolve_date", "fetch",
                 "closes_for", "sym_candidates", "date_text", "KdbError"),
    "crosscode": ("load", "Dropped", "Excluded"),
    "india": ("strategy_lists", "secondary_rows", "publish_both",
              "SECONDARY_VENUE", "IndiaError"),
    "marketcfg": ("load", "ConfigError"),
    "bands": ("compute", "BandError"),
    "ticks": ("tick_for",),
    "mailer": ("send",),
}


def _check_modules():
    """Fail in a second, not after twenty minutes.

    These modules are deployed as loose files - the target machine keeps
    them flat beside limit_up_down.py rather than in a package - so copying
    one and forgetting another is the ordinary failure, not an exotic one.
    A 2026-09-05 run got all the way through kdb AND a 16735 name Bloomberg
    fetch before dying on bpipe.ENTITLEMENT_MARKER, which an older bpipe.py
    did not have.  The work was already done and the report was lost."""
    missing = []
    for name, attrs in REQUIRED.items():
        module = globals().get(name)
        if module is None:
            missing.append(f"{name} (not imported)")
            continue
        for attr in attrs:
            if not hasattr(module, attr):
                missing.append(f"{name}.{attr}")
    if missing:
        raise SystemExit(
            f"These modules are out of step with this script:"
            f"{chr(10)}    " + f"{chr(10)}    ".join(missing)
            + f"{chr(10)}  They are deployed as loose files, so this is "
              f"almost always one that was not copied across."
            + f"{chr(10)}  Copy the whole folder rather than the files you "
              f"think changed.")


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


#  Written beside OUT_TEMP.  Not published to Test/Pilot/Prod - it is a
#  diagnostic for whoever owns the B-PIPE contract, not something the ATS
#  reads.
#  EVERY name that did not make the file, with the reason.  The run report
#  shows the first few per venue and then "(+N more)", which is right for
#  reading and useless for answering "which names, exactly".  This is the
#  list.  Written beside OUT_TEMP, not published.
EXCLUDED_CSV = "excluded.csv"
EXCLUDED_HEADER = ["ReutersCode", "BloombergCode", "Venue", "Reason",
                   "Detail"]

ENTITLEMENT_CSV = "entitlement_refused.csv"
ENTITLEMENT_HEADER = ["ReutersCode", "BloombergCode", "Venue", "EIDs",
                      "Message"]


def entitlement_rows(excluded):
    """Every name B-PIPE refused for want of an entitlement.

    Kept apart from the other exclusions because the fix is different in
    kind: no code change reaches these names.  Either the identity making
    the request is the wrong one, or the EIDs are not on the contract."""
    rows = []
    for e in excluded:
        if bpipe.ENTITLEMENT_MARKER not in e.reason:
            continue
        eids = " ".join(bpipe.eids_in(e.reason))
        for d in e.rows:
            rows.append({"ReutersCode": d.ric, "BloombergCode": d.bbg,
                         "Venue": d.venue_id, "EIDs": eids,
                         "Message": e.reason})
    rows.sort(key=lambda r: (r["Venue"], r["EIDs"], r["BloombergCode"]))
    return rows


def excluded_rows(excluded):
    """Every dropped name, one row each, worst-populated reason first is not
    attempted - the order is the order the run produced them, so the file
    reads the same way the report does."""
    rows = []
    for e in excluded:
        for d in e.rows:
            rows.append({"ReutersCode": d.ric, "BloombergCode": d.bbg,
                         "Venue": d.venue_id, "Reason": e.reason,
                         "Detail": d.detail})
    return rows


def write_excluded_csv(path, excluded):
    """Returns (path, count).  A run that dropped nothing still writes the
    header, which is the readable way to say it dropped nothing - and, more
    to the point, means an empty file is never yesterday's."""
    rows = excluded_rows(excluded)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=EXCLUDED_HEADER,
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path, len(rows)


def write_entitlement_csv(path, excluded):
    """Returns (path, count), or (None, 0) when nothing was refused - in
    which case no file is written and no stale one is left behind."""
    rows = entitlement_rows(excluded)
    path = Path(path)
    if not rows:
        if path.exists():
            path.unlink()
        return None, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ENTITLEMENT_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path, len(rows)


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

    lines = [f"  {'venue':<14} {'published':>9} {'excluded':>9}  source"]
    for v in sorted(set(cfg.venues) | set(published) | set(dropped)):
        src = cfg.venues[v].source if v in cfg.venues else "not configured"
        flag = "   <- nothing published" if (published.get(v, 0) == 0
                                             and dropped.get(v, 0)) else ""
        lines.append(f"  {v:<14} {published.get(v, 0):9d} "
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
            lines.append(f"    {venue:<14} {len(dropped):6d}  {shown}{more}")
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


def price_computed(cfg, rows, closes, ladders=None):
    """The band computed from a tier table and a close out of equity_master.

    `closes` is keyed on the RIC, because that is unique per row where an
    equity_master sym is not - two listings of one name resolve to the same
    composite.

    NO BLOOMBERG.  The close comes from kdb, which is what lets a market
    B-PIPE will not price for us be published anyway, and what makes every
    venue switchable by editing one word in markets.csv.

    Order matters: pick the tier from the previous close, take the band,
    floor the down leg at MinPrice, and only THEN round to the tick.
    Rounding before flooring would move prices near a tier boundary.  The
    tick too is chosen from the close, not from the limit being rounded."""
    out, by_reason = [], {}
    ladders = ladders or {}

    def drop(reason, r, detail=""):
        by_reason.setdefault(reason, []).append(
            crosscode.Dropped(ric=r.ric, bbg=r.bbg, venue_id=r.venue_id,
                              detail=detail))

    for r in rows:
        venue = cfg.venues[r.venue_id]
        ref = closes.get(r.ric)
        if ref is None:
            drop("no previous close in equity_master", r)
            continue

        tick = None
        if venue.rounding != "none":
            #  THE VENUE'S OWN LADDER WINS, and only Indonesia has one: its
            #  .tsr IS the ATS's file, so a ladder from anywhere else could
            #  disagree with what the trading system rounds by.  Everywhere
            #  else takes the per-name ladder out of kdb's ticksizeids /
            #  ticksizetbl - the same two tables blp_lib.q rounds by.
            ladder = cfg.ticks.get(r.venue_id) or ladders.get(r.ric)
            if not ladder:
                drop("no tick ladder for this name", r)
                continue
            tick = ticks.tick_for(ladder, ref)
            if tick is None:
                drop("no tick tier for the previous close", r, f"price {ref}")
                continue
        try:
            high, low = bands.compute(cfg.bands[r.venue_id], r.ticker, ref,
                                      tick, venue.min_price, venue.rounding)
        except bands.BandError as e:
            drop(e.reason, r, e.detail)
            continue
        out.append(_out_row(r, low, high))

    return out, _excluded(by_reason)


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


def _same_price(a, b) -> bool:
    """Two price cells, as values rather than as spellings: 3833 and 3833.0
    are one price.

    A cell that will not parse falls back to text.  An R file that does not
    carry the column at all hands us None, and Decimal(None) raises - which
    would take out the one tool the cutover depends on, at the moment it is
    being pointed at an unfamiliar file."""
    if a == b:
        return True
    try:
        return Decimal(a) == Decimal(b)
    except (TypeError, ArithmeticError):
        return False


def differences(old_rows, new_rows):
    """Differences between two output files, worst first: venue row counts,
    names present in one only, then prices that moved.  The cutover
    instrument - run it against yesterday's file, or against v1's.

    One record per difference, so the CSV report is the complete list.  The
    printed lines are rendered from these by compare(), which keeps the two
    from ever disagreeing about what was found."""
    out = []
    old = {r["#ReutersCode"]: r for r in old_rows}
    new = {r["#ReutersCode"]: r for r in new_rows}

    venues = sorted({r["Venue"] for r in old_rows} |
                    {r["Venue"] for r in new_rows})
    for v in venues:
        o = sum(1 for r in old_rows if r["Venue"] == v)
        n = sum(1 for r in new_rows if r["Venue"] == v)
        if o != n:
            out.append({"status": "rowcount", "venue": v, "code": "",
                        "column": "", "old": o, "new": n})

    for ric in sorted(set(old) - set(new)):
        out.append({"status": "only_in_old", "venue": old[ric].get("Venue", ""),
                    "code": _code(old[ric], ric), "ric": ric, "column": "",
                    "old": "", "new": ""})
    for ric in sorted(set(new) - set(old)):
        out.append({"status": "only_in_new", "venue": new[ric].get("Venue", ""),
                    "code": _code(new[ric], ric), "ric": ric, "column": "",
                    "old": "", "new": ""})

    for ric in sorted(set(old) & set(new)):
        for col in ("LimitUpPrice", "LimitDownPrice"):
            a, b = old[ric].get(col), new[ric].get(col)
            if not _same_price(a, b):
                out.append({"status": "price",
                            "venue": new[ric].get("Venue", ""),
                            "code": _code(new[ric], ric), "ric": ric,
                            "column": col, "old": a, "new": b})
    return out


def _code(row, ric: str) -> str:
    """What the report calls a name: its BloombergCode.

    The comparison still KEYS on #ReutersCode - that is what the two files
    agree on, and what the ATS contract puts first - but a report is read by
    people who work in Bloomberg codes, and 7203 JT says more at a glance
    than 7203.T.

    Falls back to the RIC when the column is absent, because an unidentified
    row in a cutover report is worse than one identified the old way."""
    return (row.get("BloombergCode") or "").strip() or ric


def _line(d) -> str:
    """One difference, as it has always printed - on the RIC, which is what
    the ATS contract puts first and what the two files are keyed on.  The
    report names the same row by its BloombergCode; both are carried so the
    two forms stay what each of their readers expects."""
    if d["status"] == "rowcount":
        return f"{d['venue']}: {d['old']} old, {d['new']} new"
    if d["status"] == "only_in_old":
        return f"only in old: {d['ric']}"
    if d["status"] == "only_in_new":
        return f"only in new: {d['ric']}"
    return f"{d['ric']} {d['column']}: old {d['old']}, new {d['new']}"


def compare(old_rows, new_rows):
    """The printed form.  Unchanged - the CSV is an addition, not a
    replacement."""
    return [_line(d) for d in differences(old_rows, new_rows)]


def write_compare_report(path, records) -> str:
    """Every difference, not the handful that fit on a screen.  A run with
    nothing to report still writes the header, which is the readable way to
    say there was nothing to report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COMPARE_COLUMNS,
                           lineterminator="\n")
        w.writeheader()
        for d in records:
            #  Selected explicitly: a record also carries the RIC, which the
            #  printed form uses and the report does not.
            w.writerow({c: d.get(c, "") for c in COMPARE_COLUMNS})
    return str(path)


def run(envs_spec: str) -> int:
    mail = (SMTP_HOST, EMAIL_FROM, EMAIL_TO)
    session = None
    try:
        envs = parse_envs(envs_spec)
        _check_modules()
        _check_connection_settings()
        here = Path(__file__).resolve().parent
        cfg = marketcfg.load(here / "config", Path(TSR_DIR))
        now = dt.datetime.now().time()
        #  READ BEFORE THE CROSSCODE, and only for the venues whose cutoff
        #  has passed.  An unreadable strategy file is fatal - an empty
        #  exclusion list is indistinguishable from a correct one, and the
        #  only symptom would be Indian names published a limit that
        #  overrides the one the ATS was configured with.  Scoping it to the
        #  cutoff is what keeps an Indian share being down from taking out
        #  the 07:30 Japan run.
        strat = india.strategy_lists(
            [v.venue_id for v in cfg.venues.values() if now >= v.cutoff],
            {v.venue_id: v.exclude_file for v in cfg.venues.values()})
        rows, excluded = crosscode.load(CROSSCODE_PATH, cfg.venues, now,
                                        strat)

        if not rows:
            print("no venue has reached its cutoff yet - nothing to publish")
            for line in _exclusion_lines(excluded):
                print(line)
            return 0

        #  The split is markets.csv's, not this file's.  Whether that means
        #  one computed venue or fifteen is a config question.
        ask, compute = cfg.by_source(rows)

        #  India's BSE listings, which live in three columns of somebody
        #  else's row rather than in rows of their own.  Priced by the same
        #  Bloomberg path as `ask` and asked in the same request - kept in
        #  their own list only because each priced row is published TWICE,
        #  under BSE-MAIN and BSE-SECONDARY.
        secondary = india.secondary_rows(rows)

        def progress(what):
            def report(n, total, so_far):
                print(f"  {what}: batch {n}/{total}, {so_far} answered",
                      end="\r")
            return report

        print(f"{len(compute)} computed, then {len(ask)} from Bloomberg"
              + (f" (+{len(secondary)} BSE secondary)" if secondary else ""))

        #  EACH SOURCE IS OPENED ONLY IF IT HAS WORK.  Switch every venue to
        #  computed and this never touches B-PIPE - which is the point of
        #  being able to switch them.  Leave them all on bloomberg and it
        #  never touches kdb.
        #
        #  KDB GOES FIRST, DELIBERATELY.  The Bloomberg fetch is the long
        #  part - sixteen thousand names, minutes - and running it ahead of
        #  kdb meant every kdb fault cost a whole run to see once. The
        #  cheap, fragile side now fails fast; the expensive side is only
        #  started once the other has worked.
        closes, sym_hits, unresolved = {}, {}, []
        ladders, no_ladder, fallback = {}, [], []
        date_asked = date_used = date_how = None
        if compute:
            host, port = kdbclose.parse_server(EQUITY_MASTER_SERVER)
            conn = kdbclose.connect(host, port)
            print(f"connected to kdb {host}:{port} for equity_master")
            date_asked = dt.date.today() - dt.timedelta(days=1)
            date_used, date_how = kdbclose.resolve_date(
                conn, date_asked, log=print)
            #  Every candidate for every name, in ONE round trip.  The
            #  wasted candidates cost a longer symbol list, not a second
            #  query; a per-name query would not finish before the open.
            wanted = []
            for r in compute:
                wanted.extend(kdbclose.sym_candidates(
                    r, cfg.venues.get(r.venue_id)))
            fetched = kdbclose.fetch(conn, date_used, sorted(set(wanted)),
                                     log=print)
            closes, sym_hits, unresolved = kdbclose.closes_for(
                compute, cfg.venues, fetched)
            print(f"  equity_master {kdbclose.date_text(date_used)} "
                  f"({date_how}): {len(closes)} closes for "
                  f"{len(compute)} names")

            #  A COMPUTED NAME WITH NO CLOSE IS ASKED OF BLOOMBERG RATHER
            #  THAN DROPPED.  A zero PX_LAST counts as no close and always
            #  has - kdbclose._to_decimal refuses anything <= 0, because a
            #  zero close would otherwise compute a band of zero to zero.
            #
            #  This is the whole reason the kdb side runs first: these
            #  names are known before the B-PIPE request is built, so they
            #  ride along in the SAME request rather than costing a second
            #  one.  They are removed from `compute` so that the run
            #  reports them once, under what actually happened to them,
            #  instead of dropping them here and publishing them there.
            fallback = [
                r for r in unresolved
                if cfg.venues[r.venue_id].no_close_fallback == "bloomberg"]
            if fallback:
                #  Built once, not once per row: `compute` is thousands of
                #  names and the set is hundreds.
                moved = set(fallback)
                compute = [r for r in compute if r not in moved]
                print(f"  no close for {len(unresolved)}; asking Bloomberg "
                      f"for {len(fallback)} of them")
            if len(fallback) != len(unresolved):
                #  The rest keep the old behaviour and are dropped by
                #  price_computed under "no previous close in
                #  equity_master", which is where they have always been
                #  reported.
                print(f"  {len(unresolved) - len(fallback)} with no close "
                      f"are on venues that do not ask Bloomberg")

            #  ONLY IF SOMETHING ACTUALLY ROUNDS.  Two more round trips on
            #  the connection already open, and none at all when every
            #  computed venue is Rounding=none - which is nine of the ten
            #  today, so this normally costs nothing.
            rounds = [r for r in compute
                      if cfg.venues[r.venue_id].rounding != "none"
                      and not cfg.venues[r.venue_id].tick_source]
            if rounds:
                want = []
                for r in rounds:
                    want.extend(kdbclose.sym_candidates(
                        r, cfg.venues.get(r.venue_id)))
                found = kdbclose.fetch_ladders(conn, sorted(set(want)),
                                               log=print)
                ladders, no_ladder, via_composite = kdbclose.ladders_for(
                    rounds, cfg.venues, found)
                print(f"  tick ladders: {len(ladders)} of {len(rounds)} "
                      f"names that round")
                #  A LADDER THAT CAME OFF THE COMPOSITE IS SUSPECT.  It is
                #  a different board's, and Korea is the case in point -
                #  000250 KQ is KOSDAQ and 000250.KS is not.  Counted here
                #  so a wrong tick is visible in the run rather than in a
                #  limit somebody queries days later.
                if via_composite:
                    print(f"  {len(via_composite)} took the COMPOSITE's "
                          f"ladder, not their own sym's - first few "
                          f"{[r.bbg for r in via_composite[:5]]}")
                #  Named here as well as in the exclusions, because a
                #  ladder that stopped resolving is a whole market about to
                #  go missing and it should not need the report to notice.
                if no_ladder:
                    print(f"  NO LADDER for {len(no_ladder)}, first few "
                          f"{[r.ric for r in no_ladder[:5]]}")

        limits, refused, field_problems = {}, {}, {}
        if ask or secondary or fallback:
            session, identity = bpipe.connect(BPIPE_HOST, BPIPE_PORT,
                                              BPIPE_APP)
            print(f"connected to {BPIPE_HOST}:{BPIPE_PORT} as {BPIPE_APP}")
            #  The fallback names ride in the SAME request.  Deduplicated
            #  because a security asked for twice is a wasted slot in a
            #  request already sixteen thousand long, and bpipe.fetch keys
            #  its answer on the security anyway.
            securities = list(dict.fromkeys(
                [r.security for r in ask]
                + [r.security for r in secondary]
                + [r.security for r in fallback]))
            limits, refused, field_problems = bpipe.fetch(
                session, identity, securities, progress=progress("limits"))
            print()
    except Exception as e:                           # noqa: BLE001
        #  A KdbError already names the query, the label and the q type of
        #  every argument. Printing only type(e).__name__ threw all of that
        #  away, which is how a live run produced a bare "QError: type".
        detail = str(e)
        mailer.send("LimitUpDown FAILED", f"{type(e).__name__}: {detail}",
                    *mail)
        print(f"FATAL {type(e).__name__}: {detail}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.stop()

    out, more = price_from_bloomberg(ask, limits, refused)
    computed_out, computed_excluded = price_computed(
        cfg, compute, closes, ladders)
    #  One set of limits, two venues.  R writes the same rows twice, once
    #  under each, and the ATS reads both.
    sec_out, sec_excluded = price_from_bloomberg(secondary, limits, refused)

    #  The computed names equity_master had no close for.  Priced off
    #  Bloomberg's OWN limits, not off a band - which is a DIFFERENT
    #  arithmetic from the venue's tiers, so the reason says where each one
    #  ended up rather than leaving a mixed venue unexplained.
    fb_out, fb_excluded = price_from_bloomberg(fallback, limits, refused)
    fb_excluded = [crosscode.Excluded(
        reason=f"no close in equity_master, then {e.reason}", rows=e.rows)
        for e in fb_excluded]
    if fallback:
        print(f"{len(fb_out)} of {len(fallback)} names with no close were "
              f"rescued from Bloomberg")

    out = out + computed_out + sec_out + fb_out + india.publish_both(sec_out)
    excluded = (list(excluded) + list(more) + list(computed_excluded)
                + list(sec_excluded) + list(fb_excluded))

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

    #  A DIAGNOSTIC MUST NOT TAKE DOWN THE REPORT.  By this point the file
    #  is already written and published; losing the report as well would
    #  leave the operator unable to tell whether the run worked at all.
    #  That is exactly what happened on 2026-09-05.
    try:
        exc_path, exc_count = write_excluded_csv(
            Path(OUT_TEMP).parent / EXCLUDED_CSV, excluded)
        report.append(f"  excluded    {exc_count:6d}  names with their "
                      f"reason written to {exc_path}")
    except Exception as e:                                  # noqa: BLE001
        report.append(f"  excluded csv FAILED: {type(e).__name__}: {e}")

    try:
        eid_path, eid_count = write_entitlement_csv(
            Path(OUT_TEMP).parent / ENTITLEMENT_CSV, excluded)
        if eid_count:
            report.append(f"  entitlement {eid_count:6d}  refused names "
                          f"written to {eid_path}")
    except Exception as e:                                  # noqa: BLE001
        report.append(f"  entitlement        NOT written: "
                      f"{type(e).__name__}: {e}")

    #  WHICH DATE the closes came from, always - a run that quietly used a
    #  stale partition is otherwise invisible, and a holiday is the normal
    #  way that happens.
    if date_used is not None:
        shown = kdbclose.date_text(date_used)
        stale = " <- NOT the day asked for" if shown != str(date_asked) else ""
        report.append(f"  close from        equity_master {shown} "
                      f"(asked {date_asked}, via {date_how}){stale}")
    #  And which symbol suffix resolved.  The day a market stops matching is
    #  the day its count here goes to zero.
    for suffix, count in sorted(sym_hits.items(), key=lambda kv: -kv[1]):
        report.append(f"  sym hit    {count:6d}  .{suffix}")

    #  Same for the status fields: MARKET_STATUS is static and may not be
    #  served at all, and the real-time candidates have unknown values.  One
    #  run of this tells us which to point STATUS_FIELD at - and, crucially,
    #  shows at a glance if a session field reads CLOSED for everything,
    #  which is what it will do at 07:30.
    for field, seen in sorted(bpipe.status_tally(limits).items()):
        for value, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            report.append(f"  status     {count:6d}  {field} = {value}")
    for field, (message, count) in sorted(field_problems.items()):
        report.append(f"  field      {count:6d}  {field}: {message}")

    text = "\n".join(report)
    print(text)
    if excluded:
        mailer.send(f"LimitUpDown report - {len(out)} rows", text, *mail)
    return 0


def _row(ric, bbg, code, venue_id, status="ACTV", **extra):
    return crosscode.Row(ric=ric, bbg=bbg, ticker=crosscode.ticker_of(bbg),
                         security=crosscode.security_name(bbg),
                         fidessa_code=code, venue_id=venue_id, status=status,
                         **extra)


def demo() -> int:
    """A whole run on canned data, BOTH branches: no Bloomberg, no shares.

    The shipped config is used as-is, so this also proves markets.csv and
    bands.csv load and that the split lands where it should."""
    import io
    here = Path(__file__).resolve().parent
    cfg = marketcfg.load(here / "config", here / "config")

    #  Japan exercises the ASKED branch, because Japan is now the only
    #  market Bloomberg prices.  Everything else exercises the COMPUTED one.
    rows = [_row("7203.T", "7203 JT", "7203.JP", "TYO-MAIN"),
            _row("7203.JNX", "7203 JE", "7203.JE", "JNX-MAIN"),
            _row("NOPX.T", "NOPX JT", "NOPX.JP", "TYO-MAIN"),
            _row("HALF.T", "HALF JT", "HALF.JP", "TYO-MAIN"),
            _row("WIDE.T", "WIDE JT", "WIDE.JP", "TYO-MAIN"),
            _row("DEAD.T", "DEAD JT", "DEAD.JP", "TYO-MAIN"),
            _row("600001.SS", "600001 CG", "600001.CN", "SHA-MAIN"),
            _row("688001.SS", "688001 CG", "688001.CN", "SHA-MAIN"),
            _row("NOCL.SS", "NOCL CG", "NOCL.CN", "SHA-MAIN"),
            _row("005930.KS", "005930 KP", "005930.KR", "KSC-MAIN"),
            _row("MAYBANK.KL", "MAYBANK MK", "MAYBANK.MY", "KLS-MAIN"),
            _row("BBCA.JK", "BBCA IJ", "BBCA.ID", "JKT-MAIN"),
            _row("TLKM.JK", "TLKM IJ", "TLKM.ID", "JKT-MAIN"),
            _row("TINY.JK", "TINY IJ", "TINY.ID", "JKT-MAIN"),
            _row("NOCL.JK", "NOCL IJ", "NOCL.ID", "JKT-MAIN"),
            #  Thailand.  Only the local line ever reaches here - the /F and
            #  /Q lines are dropped in crosscode.py, before the universe.
            _row("PTT.BK", "PTT TB", "PTT.TH", "SET-MAIN"),
            #  India.  The NSE row also carries a BSE listing that has no
            #  crosscode line of its own, in three columns of its own row.
            _row("RELI.NS", "RIL IN", "RELIANCE.IN", "NSI-MAIN",
                 venue_list="NSI-MAIN|BSE-SECONDARY",
                 bse_bbg="500325 IB", bse_ric="RELI.BO")]
    ask, compute = cfg.by_source(rows)
    secondary = india.secondary_rows(rows)

    #  7203 is the real answer the probe got on 2026-09-03.  The PTS line
    #  carries the same limits, which is what makes JNX and CHJ publishable.
    limits = {"7203 JT Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 3130.0,
                                 "MARKET_STATUS": "ACTV"},
              "7203 JE Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 3130.0},
              "HALF JT Equity": {"MAX_LIMIT": 3833.0},
              "WIDE JT Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "LAST_PRICE": 99000.0},
              "DEAD JT Equity": {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                                 "MARKET_STATUS": "DLST"},
              "PTT TB Equity": {"MIN_LIMIT": 28.0, "MAX_LIMIT": 40.0,
                                "LAST_PRICE": 34.0},
              "RIL IN Equity": {"MIN_LIMIT": 1200.0, "MAX_LIMIT": 1600.0,
                                "LAST_PRICE": 1400.0},
              "500325 IB Equity": {"MIN_LIMIT": 1190.0, "MAX_LIMIT": 1610.0,
                                   "LAST_PRICE": 1400.0},
              #  A COMPUTED name, on a computed venue, that equity_master
              #  has no close for.  Bloomberg does, so it is published
              #  rather than dropped.  Its Indonesian twin NOCL.JK has no
              #  answer here either, and is the one that stays lost.
              "NOCL CG Equity": {"MIN_LIMIT": 9.0, "MAX_LIMIT": 11.0,
                                 "LAST_PRICE": 10.0}}
    refused = {"NOPX JT Equity":
               "Security Entitlement Check Failed! EID(s) needed: 64487 "
               "or 64488 [nid:58106]"}

    #  What kdbclose.closes_for returns: keyed on the RIC, already Decimal.
    #  The NOCL pair have no row in equity_master at all.
    closes = {"600001.SS": Decimal("12.34"),
              "688001.SS": Decimal("50"),      # STAR board, the 688 prefix
              "005930.KS": Decimal("70000"),
              "MAYBANK.KL": Decimal("9.50"),
              "BBCA.JK": Decimal("8000"), "TLKM.JK": Decimal("3000"),
              "TINY.JK": Decimal("10")}

    #  Korea's real table 6132, as ticksizetbl carries it, through the same
    #  conversion a live run uses - so the demo exercises the kdb ladder
    #  rather than pretending rounding does not happen.
    ladders = {"005930.KS": ticks.from_kdb(
        [(Decimal(p), Decimal(t)) for p, t in
         (("2000", "1"), ("5000", "5"), ("20000", "10"), ("50000", "50"),
          ("200000", "100"), ("500000", "500"), ("1000001000", "1000"))])}

    #  The two NOCL names are the fallback: computed venues with no close.
    #  Bloomberg can price one of them and not the other, which is the pair
    #  worth showing - a rescue and a name that was beyond rescuing.
    fallback = [r for r in compute if r.ric not in closes]
    compute = [r for r in compute if r.ric in closes]

    out, excluded = price_from_bloomberg(ask, limits, refused)
    computed, computed_excluded = price_computed(cfg, compute, closes,
                                                 ladders)
    sec_out, sec_excluded = price_from_bloomberg(secondary, limits, refused)
    fb_out, fb_excluded = price_from_bloomberg(fallback, limits, refused)
    fb_excluded = [crosscode.Excluded(
        reason=f"no close in equity_master, then {e.reason}", rows=e.rows)
        for e in fb_excluded]
    out = out + computed + sec_out + fb_out + india.publish_both(sec_out)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OUT_HEADER, lineterminator="\n")
    w.writeheader()
    w.writerows(out)
    print(buf.getvalue(), end="")

    print(f"--- {len(ask)} asked, {len(compute)} computed, "
          f"{len(secondary)} BSE secondary ---", file=sys.stderr)
    every = (list(excluded) + list(computed_excluded) + list(sec_excluded)
             + list(fb_excluded))
    for line in _venue_summary(cfg, out, every):
        print(line, file=sys.stderr)
    for line in _exclusion_lines(every):
        print(line, file=sys.stderr)
    return 0


def kdb_check(sample: int = 5) -> int:
    """Exercise ONLY the kdb path, verbosely, on a handful of names.

    A real run spends its first minutes fetching sixteen thousand names from
    Bloomberg and only then touches kdb, so a kdb fault costs a whole run to
    see once.  This reaches the same code in seconds and prints what was
    sent, what pykx made of it, and what came back.

    Nothing is written and Bloomberg is never opened."""
    def say(line):
        print(line)

    here = Path(__file__).resolve().parent
    cfg = marketcfg.load(here / "config", Path(TSR_DIR))
    computed = [v for v in cfg.venues.values() if v.computed]
    print(f"{len(computed)} computed venues: "
          f"{', '.join(sorted(v.venue_id for v in computed))}")
    if not computed:
        print("nothing computes, so there is no kdb path to check")
        return 0

    host, port = kdbclose.parse_server(EQUITY_MASTER_SERVER)
    print(f"\nconnecting to {host}:{port}")
    conn = kdbclose.connect(host, port)
    print("  connected")

    asked = dt.date.today() - dt.timedelta(days=1)
    print(f"\nresolving the partition date (asked {asked})")
    try:
        date_used, how = kdbclose.resolve_date(conn, asked, log=say)
    except kdbclose.KdbError as e:
        print(f"\nFAILED\n{e}")
        return 1
    print(f"  resolved via {how}: {kdbclose.date_text(date_used)}")

    #  Real rows, not invented ones: the symbol candidates are the whole
    #  question and a made-up ticker would not exercise them.
    now = dt.time(23, 59, 59)
    rows, _ = crosscode.load(CROSSCODE_PATH, cfg.venues, now)
    _, to_compute = cfg.by_source(rows)
    if not to_compute:
        print("\nno computed rows in the crosscode")
        return 1
    chosen = to_compute[:max(1, sample)]
    print(f"\n{len(to_compute)} computed rows; using {len(chosen)}")

    wanted = []
    for r in chosen:
        cands = kdbclose.sym_candidates(r, cfg.venues.get(r.venue_id))
        print(f"  {r.ric:<14} {r.bbg:<16} {r.venue_id:<10} -> {cands}")
        wanted.extend(cands)

    print("\nfetching")
    try:
        fetched = kdbclose.fetch(conn, date_used, sorted(set(wanted)),
                                 log=say)
    except Exception as e:                                  # noqa: BLE001
        print(f"\nFAILED on the fetch: {type(e).__name__}: {e}")
        return 1

    print(f"\n{len(fetched)} of {len(set(wanted))} candidates answered")
    for sym, close in sorted(fetched.items())[:20]:
        print(f"  {sym:<18} {close}")

    closes, hits, missing = kdbclose.closes_for(chosen, cfg.venues, fetched)
    print(f"\n{len(closes)} of {len(chosen)} names resolved to a close")
    print(f"  suffix hits: {hits or 'none'}")
    if missing:
        print(f"  unresolved: {[r.ric for r in missing]}")

    #  THE TICK LADDER IS THE OTHER HALF OF THE KDB PATH, and it is the one
    #  worth checking before a real run: a name kdb has no ladder for is a
    #  name a rounding venue will not publish, and the count says how many
    #  that would be.
    rounds = [r for r in chosen
              if cfg.venues[r.venue_id].rounding != "none"
              and not cfg.venues[r.venue_id].tick_source]
    if not rounds:
        print("\nno sampled name is on a venue that rounds from kdb, so "
              "there is no ladder to check")
        return 0 if closes else 1

    print(f"\nfetching tick ladders for {len(rounds)} of them")
    want = []
    for r in rounds:
        want.extend(kdbclose.sym_candidates(r, cfg.venues.get(r.venue_id)))
    try:
        found = kdbclose.fetch_ladders(conn, sorted(set(want)), log=say)
    except Exception as e:                                  # noqa: BLE001
        print(f"\nFAILED on the ladder fetch: {type(e).__name__}: {e}")
        return 1

    ladders, no_ladder = kdbclose.ladders_for(rounds, cfg.venues, found)
    print(f"\n{len(ladders)} of {len(rounds)} names got a ladder")
    for r in rounds[:20]:
        ladder = ladders.get(r.ric)
        ref = closes.get(r.ric)
        if not ladder:
            print(f"  {r.ric:<14} NO LADDER")
            continue
        tier = ticks.tick_for(ladder, ref) if ref is not None else None
        print(f"  {r.ric:<14} {len(ladder)} tiers"
              + (f", tick {tier} at {ref}" if ref is not None else ""))
    if no_ladder:
        print(f"  without one: {[r.ric for r in no_ladder]}")
    return 0 if closes else 1


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
    p.add_argument("--report", default=COMPARE_REPORT, metavar="CSV",
                   help=f"where --compare writes every difference "
                        f"(default {COMPARE_REPORT})")
    p.add_argument("--kdb-check", action="store_true",
                   help="exercise ONLY the kdb path, verbosely, on a few "
                        "names. No Bloomberg, no files written.")
    p.add_argument("--sample", type=int, default=5,
                   help="how many names --kdb-check uses (default 5)")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.demo:
        return demo()

    _apply_local_settings()

    if a.kdb_check:
        return kdb_check(a.sample)

    if a.compare:
        def read(path):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                return list(csv.DictReader(fh))
        if not Path(OUT_TEMP).is_file():
            raise SystemExit(
                f"nothing to compare against: {OUT_TEMP} does not exist.\n"
                "--compare diffs the LAST run's output, it does not run the "
                "job.  Run it first.")
        records = differences(read(a.compare), read(OUT_TEMP))
        for d in records:
            print(_line(d))
        print(f"\n{len(records)} difference(s)")
        print(f"written to {write_compare_report(a.report, records)}")
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
    closes = {"BBCA.JK": Decimal("8000"), "TLKM.JK": Decimal("3000"),
              "MIDS.JK": Decimal("100"), "TINY.JK": Decimal("10")}
    cout, cexcl = price_computed(cfg, idn, closes)

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
    check("a name equity_master had no close for",
          [d.ric for d in creasons["no previous close in equity_master"]],
          ["NOCL.JK"])

    print("\nKorea, rounded on the ladder kdb keeps per name")
    #  Table 6132 as ticksizetbl carries it, through the same conversion a
    #  live run uses.  The shipped config again, not a fixture.
    kor = [row("000020.KS", "000020 KP", "000020.KR", "KSC-MAIN"),
           row("ZZZZ.KS", "ZZZZ KP", "ZZZZ.KR", "KSC-MAIN")]
    ladder = ticks.from_kdb(
        [(Decimal(p), Decimal(t)) for p, t in
         (("2000", "1"), ("5000", "5"), ("20000", "10"), ("50000", "50"),
          ("200000", "100"), ("500000", "500"), ("1000001000", "1000"))])
    kout, kexcl = price_computed(
        cfg, kor, {"000020.KS": Decimal("5150"), "ZZZZ.KS": Decimal("5150")},
        {"000020.KS": ladder})
    check("000020 KP at 5150 publishes 3610/6690 - the raw band is "
          "3605/6695, and the 10 tick at that close is the difference",
          (kout[0]["LimitUpPrice"], kout[0]["LimitDownPrice"]),
          ("6690", "3610"))
    check("A NAME KDB HAS NO LADDER FOR IS REPORTED, NOT PUBLISHED "
          "UNROUNDED - an unrounded limit is one the exchange will reject",
          [d.ric for d in
           {e.reason: e.rows for e in kexcl}["no tick ladder for this name"]],
          ["ZZZZ.KS"])

    print("\na computed name with no close falls back to Bloomberg")
    #  A zero PX_LAST is no close and always has been - kdbclose._to_decimal
    #  refuses anything <= 0 - so both of these arrive here the same way.
    fb = [row("AAA.KS", "AAA KP", "AAA.KR", "KSC-MAIN"),
          row("BBB.KS", "BBB KP", "BBB.KR", "KSC-MAIN")]
    fb_out, fb_exc = price_from_bloomberg(
        fb, {"AAA KP Equity": {"MIN_LIMIT": 900.0, "MAX_LIMIT": 1100.0,
                               "LAST_PRICE": 1000.0}}, {})
    check("the one Bloomberg can price is PUBLISHED rather than dropped, "
          "which is the whole point",
          [(r["#ReutersCode"], r["LimitUpPrice"], r["LimitDownPrice"])
           for r in fb_out],
          [("AAA.KS", "1100", "900")])
    check("and it is priced off Bloomberg's own limits, NOT the venue's "
          "band - a different arithmetic from its neighbours",
          fb_out[0]["Venue"], "KSC-MAIN")
    wrapped = [crosscode.Excluded(
        reason=f"no close in equity_master, then {e.reason}", rows=e.rows)
        for e in fb_exc]
    check("THE ONE IT CANNOT SAYS BOTH HALVES - dropping it under a bare "
          "Bloomberg reason would hide that kdb is what failed first",
          [(e.reason, [d.ric for d in e.rows]) for e in wrapped],
          [("no close in equity_master, then no answer from Bloomberg",
            ["BBB.KS"])])

    print("\nthe two branches meet the same output contract")
    check("a computed row has the same seven columns as an asked one",
          sorted(cout[0]), sorted(out[0]))
    check("and its venue is the computed one", cout[0]["Venue"], "JKT-MAIN")
    mixed = idn + [row("600001.SS", "600001 CG", "600001.CN", "SHA-MAIN")]
    asked, computed = cfg.by_source(mixed)
    check("the shipped config computes China too, not just Indonesia",
          sorted({r.venue_id for r in computed}), ["JKT-MAIN", "SHA-MAIN"])
    check("and asks Bloomberg for none of them",
          [r.venue_id for r in asked], [])
    japan = [row("7203.T", "7203 JT", "7203.JP", "TYO-MAIN")]
    asked2, computed2 = cfg.by_source(japan)
    check("Japan is the one that still goes to Bloomberg",
          ([r.venue_id for r in asked2], computed2), (["TYO-MAIN"], []))

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
    check("a column the other file does not carry at all does not take out "
          "the comparison itself",
          compare(old, [{k: v for k, v in old[0].items()
                         if k != "LimitUpPrice"}, old[1]]),
          ["A.T LimitUpPrice: old 3833, new None"])

    print("\nthe same differences, as the report carries them")
    #  with the BloombergCode the real output carries, which the RIC-only
    #  rows above deliberately do not have
    bbg = [dict(r, BloombergCode=b)
           for r, b in zip(old, ("A JT", "B JT"))]
    recs = differences(bbg, [dict(bbg[0], LimitUpPrice="3900"), bbg[1]])
    check("one record per difference", len(recs), 1)
    check("THE REPORT NAMES A ROW BY ITS BLOOMBERG CODE - it is read by "
          "people who work in those, and A JT says more than A.T",
          recs[0],
          {"status": "price", "venue": "TYO-MAIN", "code": "A JT",
           "ric": "A.T", "column": "LimitUpPrice",
           "old": "3833", "new": "3900"})
    check("a missing name carries its venue too, so the report reads by "
          "market without joining anything",
          differences(bbg, bbg[:1])[1],
          {"status": "only_in_old", "venue": "TYO-MAIN", "code": "B JT",
           "ric": "B.T", "column": "", "old": "", "new": ""})
    check("a file with no BloombergCode column falls back to the RIC, "
          "because an unidentified row in a cutover report is worse than "
          "one identified the old way",
          differences(old, old[:1])[1]["code"], "B.T")
    check("but the PRINTED form still says the RIC, which is what the ATS "
          "contract puts first and what the two files are keyed on",
          [_line(d) for d in differences(bbg, bbg[:1])],
          ["TYO-MAIN: 2 old, 1 new", "only in old: B.T"])
    check("and it is rendered from the same records, so the two can never "
          "disagree about what was found",
          [_line(d) for d in differences(old, old[:1])],
          compare(old, old[:1]))

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "report.csv"
        write_compare_report(p, recs)
        check("the report is the columns, then a row per difference",
              p.read_text(encoding="utf-8").splitlines(),
              [",".join(COMPARE_COLUMNS),
               "price,TYO-MAIN,A JT,LimitUpPrice,3833,3900"])
        check("and the RIC the record also carries is not one of them",
              "A.T" in p.read_text(encoding="utf-8"), False)
        write_compare_report(p, [])
        check("nothing to report still writes the header",
              p.read_text(encoding="utf-8").splitlines(),
              [",".join(COMPARE_COLUMNS)])

    print("\nthe excluded csv - which names, exactly")
    exc = [crosscode.Excluded(
        reason="no previous close in equity_master",
        rows=[crosscode.Dropped("A.KS", "A KP", "KSC-MAIN"),
              crosscode.Dropped("B.KS", "B KP", "KSC-MAIN")]),
        crosscode.Excluded(
            reason="no band tier for the previous close",
            rows=[crosscode.Dropped("T.JK", "T IJ", "JKT-MAIN",
                                    "price 10")])]
    rows = excluded_rows(exc)
    check("EVERY dropped name, not the handful the report shows before it "
          "says '+N more'",
          [r["ReutersCode"] for r in rows], ["A.KS", "B.KS", "T.JK"])
    check("each carrying the reason it was dropped for",
          rows[0]["Reason"], "no previous close in equity_master")
    check("and the per-name detail, where there is one",
          rows[2]["Detail"], "price 10")
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "sub" / EXCLUDED_CSV
        path, n = write_excluded_csv(target, exc)
        check("written under a directory that did not exist", n, 3)
        check("with the header we promised",
              Path(path).read_text(encoding="utf-8").splitlines()[0],
              ",".join(EXCLUDED_HEADER))
        path, n = write_excluded_csv(target, [])
        check("A RUN THAT DROPPED NOTHING STILL WRITES THE HEADER, so an "
              "empty file is never yesterday's left behind",
              (n, Path(path).read_text(encoding="utf-8").splitlines()),
              (0, [",".join(EXCLUDED_HEADER)]))

    print("\nthe entitlement csv")
    eid_reason = ("Bloomberg refused the security: Security Entitlement "
                  "Check Failed! EID(s) needed: 64487 or 64488")
    mixed = [crosscode.Excluded(reason=eid_reason,
                                rows=[crosscode.Dropped("B.KL", "B MK",
                                                        "KLS-MAIN"),
                                      crosscode.Dropped("A.KL", "A MK",
                                                        "KLS-MAIN")]),
             crosscode.Excluded(reason="no MIN_LIMIT",
                                rows=[crosscode.Dropped("C.T", "C JT",
                                                        "TYO-MAIN")])]
    got = entitlement_rows(mixed)
    check("only the entitlement refusals are written, not every exclusion",
          [r["BloombergCode"] for r in got], ["A MK", "B MK"])
    check("the EIDs are pulled out into their own column - they are what a "
          "market-data team acts on", got[0]["EIDs"], "64487 64488")
    check("and the venue rides along, so the CSV says which market needs "
          "them", got[0]["Venue"], "KLS-MAIN")
    check("nothing entitlement-related means no rows",
          entitlement_rows([mixed[1]]), [])
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "sub" / "entitlement_refused.csv"
        path, n = write_entitlement_csv(target, mixed)
        check("the file is written under a directory that did not exist",
              (path is not None, n, target.is_file()), (True, 2, True))
        with target.open(encoding="utf-8") as fh:
            body = list(csv.DictReader(fh))
        check("and reads back with the header we promised",
              (body[0]["ReutersCode"], body[0]["EIDs"]), ("A.KL", "64487 64488"))
        path, n = write_entitlement_csv(target, [mixed[1]])
        check("a clean run REMOVES the file rather than leaving yesterday's "
              "refusals looking like today's", (path, n, target.exists()),
              (None, 0, False))

    print("\nswitching a venue between sources")
    import tempfile as _tf
    real = Path(__file__).resolve().parent / "config"
    header = (real / "markets.csv").read_text(encoding="utf-8")
    with _tf.TemporaryDirectory() as d:
        (Path(d) / "bands.csv").write_text(
            (real / "bands.csv").read_text(encoding="utf-8"),
            encoding="utf-8")
        #  Every .tsr the share carries, and ticks.csv, which is where every
        #  venue but Indonesia keeps its ladder.
        for f in list(real.glob("*.tsr")) + [real / "ticks.csv"]:
            if f.is_file():
                (Path(d) / f.name).write_text(f.read_text(encoding="utf-8"),
                                              encoding="utf-8")

        #  EVERY venue can go to bloomberg, because asking needs no tiers.
        #  That is the direction which rescues a market whose rule turns out
        #  to be wrong: one word, no code.
        (Path(d) / "markets.csv").write_text(
            header.replace(",computed,", ",bloomberg,"), encoding="utf-8")
        cfg2 = marketcfg.load(Path(d), Path(d))
        check("every venue flips to bloomberg by editing ONE word in "
              "markets.csv, with no code change",
              [v.venue_id for v in cfg2.venues.values() if v.computed], [])
        check("and all nineteen are still there", len(cfg2.venues), 19)

        #  The other direction is not free, and must not silently be.
        (Path(d) / "markets.csv").write_text(
            header.replace(",bloomberg,", ",computed,"), encoding="utf-8")
        try:
            marketcfg.load(Path(d), Path(d))
            got = "loaded"
        except marketcfg.ConfigError as e:
            got = str(e)
        check("switching a venue that has NO tiers is refused loudly rather "
              "than publishing a made-up band - Tokyo's limits are an "
              "absolute step table nobody has written down here, and "
              "neither Thailand's rule nor India's is written either",
              "no band tiers in bands.csv" in got, True)

    shipped = marketcfg.load(real, real)
    check("as shipped, the markets Bloomberg prices are Japan, Thailand and "
          "India - the three whose rules are not in bands.csv",
          sorted({v.country for v in shipped.venues.values()
                  if not v.computed}), ["India", "Japan", "Thailand"])
    check("and not one of them has tiers, so none could be computed today "
          "even by accident",
          [v.venue_id for v in shipped.venues.values()
           if not v.computed and v.venue_id in shipped.bands], [])

    print("\nIndia, end to end through both halves of the fetch")
    ind = [_row("RELI.NS", "RIL IN", "RELIANCE.IN", "NSI-MAIN",
                venue_list="NSI-MAIN|BSE-SECONDARY", bse_bbg="500325 IB",
                bse_ric="RELI.BO"),
           _row("TCS.NS", "TCS IN", "TCS.IN", "NSI-MAIN")]
    sec = india.secondary_rows(ind)
    ind_values = {"RIL IN Equity": {"MIN_LIMIT": 1200.0, "MAX_LIMIT": 1600.0},
                  "TCS IN Equity": {"MIN_LIMIT": 10.0, "MAX_LIMIT": 20.0},
                  "500325 IB Equity": {"MIN_LIMIT": 1190.0,
                                       "MAX_LIMIT": 1610.0}}
    nse_out, _ = price_from_bloomberg(ind, ind_values)
    bse_out, _ = price_from_bloomberg(sec, ind_values)
    whole = nse_out + bse_out + india.publish_both(bse_out)
    check("the NSE rows, the BSE row, and the BSE row again under the "
          "secondary venue",
          [(r["#ReutersCode"], r["Venue"]) for r in whole],
          [("RELI.NS", "NSI-MAIN"), ("TCS.NS", "NSI-MAIN"),
           ("RELI.BO", "BSE-MAIN"), ("RELI.BO", "BSE-SECONDARY")])
    check("the BSE listing is priced off its OWN Bloomberg code, not the "
          "NSE one - two lines of one company, with their own limits",
          [(r["BloombergCode"], r["LimitUpPrice"]) for r in whole
           if r["Venue"].startswith("BSE")],
          [("500325 IB", "1610"), ("500325 IB", "1610")])
    check("both BSE rows carry the same numbers, which is the whole point "
          "of publishing one fetch twice",
          whole[2]["LimitDownPrice"] == whole[3]["LimitDownPrice"], True)
    check("and what this produces still validates", validate(whole), [])

    print("\nchecking the sibling modules before doing any work")
    check("the real modules satisfy the list, so the check cannot cry wolf",
          _check_modules(), None)
    check("and the list names every bpipe attribute this script uses",
          sorted(REQUIRED["bpipe"]),
          sorted({n.split(".", 1)[1] for n in
                  ["bpipe.band_from", "bpipe.connect", "bpipe.eids_in",
                   "bpipe.ENTITLEMENT_MARKER", "bpipe.fetch",
                   "bpipe.status_tally"]}))

    class Stale:
        """bpipe as it was before the entitlement CSV was added."""
        def __init__(self):
            for a in REQUIRED["bpipe"]:
                if a != "ENTITLEMENT_MARKER":
                    setattr(self, a, True)

    real, globals()["bpipe"] = globals()["bpipe"], Stale()
    try:
        _check_modules()
        got = "no error"
    except SystemExit as e:
        got = str(e)
    finally:
        globals()["bpipe"] = real
    check("a stale bpipe is caught at STARTUP, not after a twenty minute "
          "fetch has already been paid for",
          "bpipe.ENTITLEMENT_MARKER" in got, True)
    check("and the message says what actually goes wrong - one loose file "
          "not copied across", "not copied across" in got, True)

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
    check("every configured venue appears, published or not - matched by "
          "name, since BSE-SECONDARY is not spelled like the others",
          sorted(l.split()[0] for l in lines[1:]), sorted(cfg2.venues))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
