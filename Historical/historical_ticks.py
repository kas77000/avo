#!/usr/bin/env python3
"""Trade prints out of kdb, one CSV per name per day.

Replaces a Bloomberg HistoricalTickDataRequest.  Our B-PIPE entitlement is
real-time only - `LimitUpDown/other/bpipe_history.py` is the probe that
established it - so the prints come from the plant's own store, qatt, and are
written in the shape the Bloomberg add-in was writing:

    raw-EAU AU-20260817.csv
    #Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern Standard Time
    09:31:33,0.105,1000,T,T,XASX

ONE RULE, NOT TWO POPULATIONS.  Every name wants the last BACKFILL_DAYS
partitions minus whatever has already been TRIED, and the backfill and the
daily top-up fall out of that one subtraction.  "Tried" means two things:

    a file on disk           in OUTPUT_DIR, one per name per day
    a line in the miss cache kdb was asked and had nothing

There is no state file beyond those two, and both are readable and editable
by hand.  Delete a file and it comes back; delete a cache line and the name
is asked again.

THE MISS CACHE IS WHY THIS FINISHES.  qatt holds only the names we subscribe
to.  A crosscode name it has never carried has no prints on any date, ever -
and with no record of having asked, every run asks again for every day of the
backfill, forever.  See misscache.py, including what does NOT go in it.

WHAT IT DOES NOT DO.  It does not build the volume curve.  It lands the raw
prints another process turns into one, which is the same division the
Bloomberg version had.

    python historical_ticks.py --self-test     checks, no kdb, no files
    python historical_ticks.py --demo          the whole pipeline, canned
    python historical_ticks.py --dry-run       real reads, writes nothing
    python historical_ticks.py                 the daily run
    python historical_ticks.py --backfill 90   a deeper first run
    python historical_ticks.py --date 2026-09-02   as if that were today
    python historical_ticks.py --only "7203 JT"    one name, for a check
    python historical_ticks.py --retry-misses  ask again about the empties
    python historical_ticks.py --log run.log   tee the log to a file
    python historical_ticks.py --quiet         warnings and failures only

ONE NAME, ONE DAY, EVERY STAGE:

    python historical_ticks.py --trace "7203 JT" --date 2026-09-02

--trace prints the crosscode rows, the three candidate shapes, which
equity_master pass answered, the resolved sym and MIC, the collapse, the file
code, the date chosen, whether a real run would have skipped it, THE EXACT q
SENT, the rows back, the time span, and the head of the file written.  It
calls the same stage functions a real run calls, because a trace that walked
a parallel path would only prove the parallel path works.

It IGNORES the output directory and the miss cache and rewrites the file - it
is a diagnostic, not a run - and it takes either spelling of a Chinese name
(600000 C1 or 600000 CG).  Add --dry-run to see everything and write nothing.

Servers and paths come from local_settings.py via settings.py.  Nothing here
takes a host or a port on the command line.

The whole thing, stage by stage, with what each can get wrong: WORKFLOW.md

BEFORE THE FIRST REAL RUN, settle the clock: run qatt_time_probe.py and set
qattsource.TIME_FIELD.  It ships as a plausible name, not a checked one, and
a wrong answer produces files that are the right length with every timestamp
shifted by hours.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import crosscode
import logs
import marketcfg
import misscache
import qattsource
import settings
import ticksfile
import universe

HERE = Path(__file__).resolve().parent

# =============================================================================
# SETTINGS all live in settings.py, read from local_settings.py beside it.
# Nothing here takes a host or a port on the command line.
# =============================================================================

SettingError = settings.SettingError


# =============================================================================
# THE WORK.  Pure where it can be: plan() decides everything before a single
# file is written, so --dry-run and the real run take the same decisions.
# =============================================================================

def candidates(rows, markets) -> dict:
    """{bloomberg code: (sym_bpipe shape, sym_mbpipe shape, sym shape)}.

    Three shapes because equity_master is asked three ways - see
    qattsource.fetch_master.  The third is the ticker dot-joined to the
    market's composite, and it is the one that finds China: a Shanghai line
    is `600000 C1` here and `600000.CH` there.

    Keyed on the code and not the row, because three venue rows for one name
    are three DIFFERENT codes but still one round trip."""
    out = {}
    for r in rows:
        comp = marketcfg.composite(r.market, markets)
        out[r.bbg] = (crosscode.bbg_dotted(r.bbg),
                      crosscode.bbg_full(r.bbg),
                      f"{r.ticker}.{comp}" if r.ticker and comp else "")
    return out


def chunked(items, size):
    items = list(items)
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def plan(names, partitions, out_dir, backfill, cache=None) -> dict:
    """{date: [name, ...]} - who needs what, before anything is fetched.

    Inverted from per-name to per-date on purpose: qatt is partitioned by
    date, so one query per date serves every name that wants it, and a
    backfill of sixty days is sixty reads rather than sixty times the
    universe.

    A date counts as ALREADY TRIED if there is a file for it or a line in
    the miss cache.  Without the second half, a name qatt has never carried
    is re-asked for every day of the backfill on every run, forever."""
    cache = cache or {}
    by_date, per_name = {}, {}
    for name in names:
        have = (ticksfile.existing_dates(out_dir, name.bbg)
                | misscache.tried(cache, name.bbg))
        want = ticksfile.days_wanted(have, partitions, backfill)
        per_name[name.bbg] = want
        for d in want:
            by_date.setdefault(d, []).append(name)
    return {"by_date": dict(sorted(by_date.items())), "per_name": per_name}


def tz_label(name, markets) -> str:
    """The seventh header cell, from config/markets.csv.

    Taken off the crosscode row that names the file, because that is the row
    whose market the file's clock belongs to.  Blank is normal until the
    probe has settled what the clock IS - see ticksfile.py."""
    m = markets.get(name.rows[0].market) if name.rows else None
    return getattr(m, "time_zone", "") if m else ""


def run(conn, plan_, markets, out_dir, chunk, dry_run, cache=None,
        log=None) -> dict:
    """Fetch and write, one date at a time.

    A name with prints gets a file.  A name with none gets a line in the
    miss cache INSTEAD - not an empty file - so the output directory holds
    only real data and the record of absence lives in one place."""
    cache = cache if cache is not None else {}
    log = log or logs.Log(stamps=False, quiet=True)
    stats = {"files": 0, "rows": 0, "empty": 0, "reads": 0}
    for date, names in plan_["by_date"].items():
        by_sym = {}
        for n in names:
            by_sym.setdefault(n.sym, []).append(n)
        log.info(f"{date}  {len(names)} names, {len(by_sym)} syms, "
                 f"{-(-len(by_sym) // max(1, chunk))} read(s)")

        fetched = {}
        for group in chunked(sorted(by_sym), chunk):
            if not dry_run:
                fetched.update(qattsource.fetch_ticks(conn, date, group))
            stats["reads"] += 1

        for name in names:
            rows = fetched.get(name.sym, [])
            if dry_run:
                continue
            if not rows:
                #  kdb answered, and the answer was empty.  That is a fact
                #  about the data and it is worth remembering.  A query that
                #  RAISED never gets here - it takes the whole run down -
                #  which is what keeps an outage out of the cache.
                stats["empty"] += 1
                misscache.record(cache, name.bbg, name.sym, date)
                continue
            path = Path(out_dir) / ticksfile.filename(name.bbg, date)
            stats["rows"] += ticksfile.write(
                path, rows, name.mic, tz_label(name, markets))
            stats["files"] += 1
    return stats


def log_universe(rows, names, excluded, tally, log) -> None:
    log.kv("crosscode rows", logs.thousands(len(rows)))
    for e in excluded:
        if not e.rows:
            #  A note about the FILE, not about rows - the crosscode reader
            #  uses the same carrier to say which optional columns were
            #  absent.  Printing it as "0 excluded" reads like a bug.
            log.info(f"note: {e.reason}")
        else:
            log.warn(f"{logs.thousands(len(e.rows))} excluded: {e.reason}")
    log.kv("names to fetch", logs.thousands(len(names)),
           f"{logs.thousands(len(rows))} rows collapsed into "
           f"{logs.thousands(len(names))}")
    log.kv("sym from equity_master", logs.thousands(tally["equity_master"]))
    if tally["markets.csv"]:
        log.warn(f"{tally['markets.csv']} syms came from config/markets.csv, "
                 f"not equity_master. They carry no MIC, so nothing can "
                 f"rename their file.")
    if tally["no primary match"]:
        log.warn(f"{tally['no primary match']} names have no primary "
                 f"listing; each is named by its first crosscode row")
    if tally["renamed by MIC"]:
        log.kv("renamed CG/CS", tally["renamed by MIC"],
               "Shanghai and Shenzhen, named for the consumer")
    if tally["china without a MIC"]:
        log.warn(f"CHINA, NOT RENAMED: {tally['china without a MIC']} .CH "
                 f"syms had no MIC, so their files keep C1/C2 - the WRONG "
                 f"code. Fix before trusting the output.")


def log_plan(plan_, log) -> None:
    want = plan_["per_name"]
    log.kv("backfill", sum(1 for w in want.values() if len(w) > 1), "names")
    log.kv("one day", sum(1 for w in want.values() if len(w) == 1), "names")
    log.kv("up to date", sum(1 for w in want.values() if not w), "names")
    log.kv("dates to read", len(plan_["by_date"]))


def log_result(stats, dry_run, log) -> None:
    log.kv("qatt reads", stats["reads"])
    if dry_run:
        log.ok("--dry-run: nothing fetched, nothing written, cache untouched")
        return
    log.kv("files written", logs.thousands(stats["files"]))
    log.kv("prints written", logs.thousands(stats["rows"]))
    if stats["empty"]:
        log.kv("no prints", stats["empty"],
               "recorded in the miss cache, not written as empty files")


# =============================================================================
# THE PIPELINE, one function per stage.  main() and trace() both call THESE -
# a trace that re-implemented the pipeline would prove nothing about the
# pipeline.
# =============================================================================

def stage_crosscode(path, only, log):
    """Read the security master, optionally down to one name.

    `only` matches the crosscode's own BloombergCode (`600000 C1`) or the
    code the file ends up under (`600000 CG`).  Both are offered because a
    Chinese name is spelt differently in those two places and a reader
    holding one of them should not have to know which."""
    rows, dropped = crosscode.load(path)
    log.kv("crosscode", logs.thousands(len(rows)) + " rows", str(path))
    for e in dropped:
        if e.rows:
            log.warn(f"{logs.thousands(len(e.rows))} rows dropped: "
                     f"{e.reason}")
        else:
            log.info(f"note: {e.reason}")
    if not only:
        return rows, dropped

    want = only.strip().upper()
    kept = [r for r in rows if r.bbg.upper() == want]
    how = "BloombergCode"
    if not kept:
        #  Not a crosscode code - try it as a file code.  600000 CG is
        #  ticker + CG, so match on the ticker and let the MIC sort the rest
        #  out downstream.
        ticker = want.rsplit(" ", 1)[0]
        kept = [r for r in rows if r.ticker.upper() == ticker]
        how = "ticker (given as a file code)"
    if not kept:
        log.fail(f"{only!r} is in neither the BloombergCode nor the ticker "
                 f"column of {path}")
        return None, dropped
    log.kv("--only", f"{len(kept)} row(s)", f"matched on {how}")
    for r in kept:
        log.info(f"    {r.bbg:<14} {r.market:<12} {r.sec_type:<8} "
                 f"ric={r.ric}")
    return kept, dropped


def stage_master(conn, rows, markets, master_chunk, log):
    """Resolve every crosscode code to a qatt sym, in three passes."""
    master_date = qattsource.resolve_master_date(conn, dt.date.today())
    log.kv("equity_master date", master_date,
           "rolled back from today to the newest row")
    cands = candidates(rows, markets)
    master, hits = {}, {"sym_bpipe": 0, "sym_mbpipe": 0, "sym": 0}
    for group in chunked(cands, master_chunk):
        got = qattsource.fetch_master(
            conn, master_date, {k: cands[k] for k in group})
        if got:
            master.update(got["rows"])
            for k, v in got["hits"].items():
                hits[k] += v
    log.kv("matched", f"{logs.thousands(len(master))} of "
                      f"{logs.thousands(len(cands))}",
           f"sym_bpipe {hits['sym_bpipe']}, sym_mbpipe {hits['sym_mbpipe']}, "
           f"sym {hits['sym']}")
    missed = len(cands) - len(master)
    if missed:
        log.warn(f"{logs.thousands(missed)} codes equity_master has no row "
                 f"for; they fall back to config/markets.csv or drop out")
    return master, hits, master_date, cands


def stage_partitions(conn, date_text, log):
    """What days qatt actually holds, capped at --date when given."""
    parts = qattsource.partitions(conn)
    if not parts:
        log.fail("qatt holds no partitions at all")
        return None
    if date_text:
        try:
            cutoff = dt.date.fromisoformat(date_text)
        except ValueError:
            log.fail(f"--date {date_text!r} is not a date; write it as "
                     f"YYYY-MM-DD")
            return None
        parts = [d for d in parts if d <= cutoff]
        if not parts:
            log.fail(f"qatt has no partition on or before {cutoff}")
            return None
    log.kv("qatt partitions", logs.thousands(len(parts)),
           f"{parts[0]} .. {parts[-1]}")
    log.kv("time column", qattsource.TIME_FIELD,
           "set from qatt_time_probe.py")
    return parts


# =============================================================================
# TRACE  - one name, one date, every stage shown.
#
# It calls the SAME stage functions the real run does.  A trace that walked a
# parallel path would tell you that the parallel path works.
# =============================================================================

def trace(cfg, a, log=None) -> int:
    log = log or logs.Log(path=a.log or None)
    markets = marketcfg.load(HERE / "config" / "markets.csv")
    out_dir = cfg["OUTPUT_DIR"]

    log.info(f"TRACE  {a.trace!r}" + (f" on {a.date}" if a.date
                                      else " on the newest partition"))
    log.warn("a trace IGNORES the output directory and the miss cache, and "
             "rewrites the file. It is a diagnostic, not a run.")

    log.step(1, "crosscode  - which rows is this name")
    rows, _dropped = stage_crosscode(cfg["CROSSCODE_PATH"], a.trace, log)
    if rows is None:
        return 1

    log.step(2, "equity_master  - what is its qatt sym")
    em = qattsource.connect(*settings.server(cfg, "EQUITY_MASTER_SERVER"))
    master, hits, master_date, cands = stage_master(
        em, rows, markets, cfg["MASTER_CHUNK"], log)
    for bbg, (dotted, full, comp) in sorted(cands.items()):
        log.info(f"    {bbg:<14} sym_bpipe={dotted or '-':<14} "
                 f"sym_mbpipe={full or '-':<20} sym={comp or '-'}")
    for bbg in sorted(cands):
        row = master.get(bbg)
        if row is None:
            log.warn(f"{bbg}: equity_master has no row - it will fall back "
                     f"to config/markets.csv, or drop out")
            continue
        log.info(f"    {bbg:<14} -> " + "  ".join(
            f"{k}={row[k] or '-'}" for k in qattsource.MASTER_FIELDS))

    log.step(3, "universe  - collapse, rename, filter")
    names, excluded, tally = universe.build(rows, master, markets)
    for e in excluded:
        log.warn(f"excluded {e.reason}: {', '.join(e.rows)}")
    if not names:
        log.fail("nothing survived the universe stage - see the warnings "
                 "above. Nothing can be fetched.")
        return 1
    if len(names) > 1:
        log.warn(f"{len(names)} names, not one: "
                 f"{', '.join(n.bbg for n in names)}. A trace is clearer on "
                 f"one - narrow --trace.")
    for n in names:
        log.kv("file code", n.bbg,
               "the crosscode's own code" if n.bbg == n.rows[0].bbg
               else f"RENAMED from {n.rows[0].bbg} by MIC {n.mic}")
        log.kv("qatt sym", n.sym, "what kdb is asked for")
        log.kv("MIC", n.mic or "-", "the MicCode column of every row")
        log.kv("collapsed from", f"{len(n.rows)} crosscode row(s)",
               ", ".join(r.bbg for r in n.rows))
        log.kv("sym resolved by", n.source)
        label = tz_label(n, markets)
        log.kv("timezone label", label or "(blank)",
               "the 7th header cell" if label
               else "so the header has six cells - config/markets.csv "
                    "TimeZone is empty")

    log.step(4, "qatt  - which day")
    conn = qattsource.connect(*settings.server(cfg, "QATT_SERVER"))
    parts = stage_partitions(conn, a.date, log)
    if parts is None:
        return 1
    date = parts[-1]
    log.kv("date chosen", date,
           "--date" if a.date else "the newest partition")

    log.step(5, "what a real run would already have")
    for n in names:
        have = ticksfile.existing_dates(out_dir, n.bbg)
        cache = misscache.load(cfg["MISS_CACHE_PATH"]
                               or Path(out_dir) / "_no_data.csv")
        log.kv("file on disk",
               "yes" if date in have else "no",
               str(Path(out_dir) / ticksfile.filename(n.bbg, date)))
        log.kv("in the miss cache",
               "yes" if date in misscache.tried(cache, n.bbg) else "no")
        log.kv("a real run would",
               "fetch" if date not in (have | misscache.tried(cache, n.bbg))
               else "SKIP this date",
               "the trace fetches anyway")

    log.step(6, "the query")
    log.info(f"    {qattsource.ticks_q()}")
    log.kv("date", date)
    log.kv("syms", [n.sym for n in names])

    fetched = qattsource.fetch_ticks(conn, date, [n.sym for n in names])
    for n in names:
        got = fetched.get(n.sym, [])
        log.kv("rows returned", logs.thousands(len(got)), n.sym)
        if not got:
            log.warn(f"{n.sym} had NO prints on {date}. A real run would "
                     f"write a miss-cache line and no file. Check the sym "
                     f"above is what kdb knows this name as, and that {date} "
                     f"was a trading day for it.")
            continue
        stamps = [r["time"] for r in got if r["time"]]
        log.kv("time span", f"{min(stamps)} .. {max(stamps)}" if stamps
               else "(no timestamps!)",
               f"column {qattsource.TIME_FIELD}")
        if stamps and not a.date:
            log.info("    check that span against the exchange's session. "
                     "If it is out by whole")
            log.info("    hours, TIME_FIELD is the wrong column - see "
                     "qatt_time_probe.py")
        for r in got[:5]:
            log.info(f"    {r['time']}  {r['price']} x {r['size']}  "
                     f"cond={r['cond'] or '-'}  ex={r['ex'] or '-'}")
        if len(got) > 5:
            log.info(f"    ... {logs.thousands(len(got) - 5)} more")

    log.step(7, "write")
    if a.dry_run:
        log.ok("--dry-run: nothing written")
    else:
        for n in names:
            path = Path(out_dir) / ticksfile.filename(n.bbg, date)
            written = ticksfile.write(path, fetched.get(n.sym, []), n.mic,
                                      tz_label(n, markets))
            log.kv("written", f"{logs.thousands(written)} rows", str(path))
            for line in path.read_text(encoding="utf-8").splitlines()[:3]:
                log.info(f"    {line}")

    log.info()
    log.info(f"{log.counts[logs.WARN]} warning(s) above"
             if log.counts[logs.WARN] else "no warnings")
    log.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Trade prints out of kdb, one CSV per name per day.")
    p.add_argument("--backfill", type=int, default=None,
                   help=f"days for a name with no files (default "
                        f"{settings.DEFAULTS['BACKFILL_DAYS']})")
    p.add_argument("--date", default="",
                   help="treat this as the newest day, YYYY-MM-DD; for "
                        "reruns")
    p.add_argument("--only", default="",
                   help="one code - the crosscode's (600000 C1) or the "
                        "file's (600000 CG)")
    p.add_argument("--trace", default="",
                   help="ONE name, ONE date, every stage shown and the file "
                        "written whatever is already on disk.  Use --date "
                        "to pick the day.")
    p.add_argument("--log", default="",
                   help="tee the log to this file as well as the screen")
    p.add_argument("--quiet", action="store_true",
                   help="commentary off; warnings and failures still print")
    p.add_argument("--chunk", type=int, default=None,
                   help=f"syms per qatt read (default "
                        f"{settings.DEFAULTS['SYM_CHUNK']})")
    p.add_argument("--retry-misses", action="store_true",
                   help="ignore the miss cache for this run and rebuild it "
                        "from what today's run actually finds")
    p.add_argument("--dry-run", action="store_true",
                   help="read the crosscode and kdb, decide everything, "
                        "write nothing")
    p.add_argument("--demo", action="store_true",
                   help="the whole pipeline on canned data, no kdb")
    p.add_argument("--self-test", action="store_true",
                   help="checks, with no kdb and no files")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.demo:
        return demo()

    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "OUTPUT_DIR")
        em_host, em_port = settings.server(cfg, "EQUITY_MASTER_SERVER")
        q_host, q_port = settings.server(cfg, "QATT_SERVER")
    except SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    if a.trace:
        return trace(cfg, a)

    crosscode_path = cfg["CROSSCODE_PATH"]
    out_dir = cfg["OUTPUT_DIR"]
    miss_path = cfg["MISS_CACHE_PATH"] or Path(out_dir) / "_no_data.csv"
    backfill = cfg["BACKFILL_DAYS"] if a.backfill is None else a.backfill
    chunk = cfg["SYM_CHUNK"] if a.chunk is None else a.chunk

    log = logs.Log(path=a.log or None, quiet=a.quiet)
    markets = marketcfg.load(HERE / "config" / "markets.csv")

    log.step(1, "crosscode")
    rows, dropped = stage_crosscode(crosscode_path, a.only, log)
    if rows is None:
        return 1

    log.step(2, "equity_master")
    em = qattsource.connect(em_host, em_port)
    master, hits, master_date, cands = stage_master(
        em, rows, markets, cfg["MASTER_CHUNK"], log)

    log.step(3, "universe")
    names, excluded, tally = universe.build(rows, master, markets)
    excluded = list(dropped) + excluded
    log_universe(rows, names, excluded, tally, log)

    log.step(4, "qatt")
    conn = qattsource.connect(q_host, q_port)
    parts = stage_partitions(conn, a.date, log)
    if parts is None:
        return 1

    log.step(5, "what is already tried")
    cache = {} if a.retry_misses else misscache.load(miss_path)
    log.kv("miss cache", f"{logs.thousands(misscache.count(cache))} pairs",
           f"{miss_path}" + ("   IGNORED, --retry-misses"
                             if a.retry_misses else ""))
    plan_ = plan(names, parts, out_dir, backfill, cache)
    log_plan(plan_, log)

    log.step(6, "fetch and write")
    stats = run(conn, plan_, markets, out_dir, chunk, a.dry_run, cache, log)
    if not a.dry_run:
        n = misscache.save(miss_path, cache)
        log.kv("miss cache now", f"{logs.thousands(n)} pairs", str(miss_path))

    log.step(7, "result")
    log_result(stats, a.dry_run, log)
    log.info()
    log.info(f"{log.counts[logs.WARN]} warning(s) above" if
             log.counts[logs.WARN] else "no warnings")
    log.close()
    return 0


# =============================================================================
# DEMO  - the whole pipeline against canned data, so the shape of a run can
# be read on a machine with no kdb.
# =============================================================================

def demo() -> int:
    import tempfile
    from decimal import Decimal

    class Conn:
        """Toyota on two venues, BHP, and a Shanghai name that only the
        third resolution pass can find."""

        def __call__(self, q, *args):
            if "max date" in q:
                return dt.date(2026, 9, 3)
            if "distinct date" in q:
                return [dt.date(2026, 9, 1), dt.date(2026, 9, 2),
                        dt.date(2026, 9, 3)]
            #  Dispatch on the TABLE first.  The tick query also contains
            #  "sym in", so testing that before ruling out equity_master
            #  routes every tick read to the wrong branch - which is exactly
            #  what an earlier version of this fake did.
            if "equity_master" not in q:
                return self._ticks(*args)
            if "sym_bpipe in" in q:
                return [
                    {"sym_bpipe": "7203.JT", "sym": "7203.JP",
                     "EQY_PRIM_EXCH_SHRT": "JT", "COMPOSITE_EXCH_CODE": "JP",
                     "ID_MIC_PRIM_EXCH": "XTKS"},
                    {"sym_bpipe": "7203.JE", "sym": "7203.JP",
                     "EQY_PRIM_EXCH_SHRT": "JT", "COMPOSITE_EXCH_CODE": "JP",
                     "ID_MIC_PRIM_EXCH": "XTKS"},
                    {"sym_bpipe": "BHP.AU", "sym": "BHP.AU",
                     "EQY_PRIM_EXCH_SHRT": "AU", "COMPOSITE_EXCH_CODE": "AU",
                     "ID_MIC_PRIM_EXCH": "XASX"},
                    #  Resolves fine, but qatt has never carried it - the
                    #  case the miss cache exists for.
                    {"sym_bpipe": "ZZZ.SP", "sym": "ZZZ.SP",
                     "EQY_PRIM_EXCH_SHRT": "SP", "COMPOSITE_EXCH_CODE": "SP",
                     "ID_MIC_PRIM_EXCH": "XSES"},
                    ]
            if "sym_mbpipe in" in q:
                return []
            if "sym in" in q:
                #  Shanghai answers on NEITHER of the first two passes - its
                #  sym_bpipe would be 600000.C1 - and only the composite
                #  candidate 600000.CH finds it.
                return [{"sym": "600000.CH", "EQY_PRIM_EXCH_SHRT": "C1",
                         "COMPOSITE_EXCH_CODE": "CH",
                         "ID_MIC_PRIM_EXCH": "XSHG"}]
            return []

        def _ticks(self, date, syms):
            out = []
            for sym in syms:
                if sym == "7203.JP":
                    out += [{"sym": sym, "tradeTime": dt.time(9, 0, 1),
                             "price": 2500.0, "size": 100, "cond": "OA",
                             "ex": "T"},
                            {"sym": sym, "tradeTime": dt.time(14, 59, 58),
                             "price": 2530.0, "size": 900, "cond": "",
                             "ex": "H"}]
                elif sym == "BHP.AU":
                    out += [{"sym": sym, "tradeTime": dt.time(10, 0, 0),
                             "price": 40.5, "size": 300, "cond": "T",
                             "ex": "T"}]
                elif sym == "600000.CH":
                    out += [{"sym": sym, "tradeTime": dt.time(9, 30, 0),
                             "price": 12.34, "size": 500, "cond": "T",
                             "ex": "S"}]
            return out

    HDR = ("#FidessaCode,RicCode,Type,BloombergCode,BloombergSecurityType,"
           "FidessaMarket,Currency\n")
    BODY = ("7203.JP,7203.T,Equity,7203 JT,Equity,TYO-MAIN,JPY\n"
            "7203.JE,7203.CHJ,Equity,7203 JE,Equity,JNX-MAIN,JPY\n"
            "BHP.AU,BHP.AX,Equity,BHP AU,Equity,ASX-MAIN,AUD\n"
            "600000.CH,600000.SS,Equity,600000 C1,Equity,SHA-MAIN,CNY\n"
            "ZZZ.SP,ZZZ.SI,Equity,ZZZ SP,Equity,SES-MAIN,SGD\n")

    print("historical_ticks --demo\n"
          "Canned kdb, canned crosscode, a real output directory.\n")
    #  No stamps: a demo that printed the wall clock would differ on every
    #  run and could not be diffed against the last one.
    dlog = logs.Log(stamps=False)

    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR + BODY, encoding="utf-8")
        out = Path(d) / "out"

        markets = marketcfg.load(HERE / "config" / "markets.csv")
        rows, dropped = crosscode.load(cc)
        conn = Conn()

        cands = candidates(rows, markets)
        got = qattsource.fetch_master(conn, dt.date(2026, 9, 3), cands)
        names, excluded, tally = universe.build(rows, got["rows"], markets)
        parts = qattsource.partitions(conn)

        print(f"crosscode  {len(rows)} rows")
        print(f"qatt       {len(parts)} partitions, "
              f"{parts[0]} .. {parts[-1]}")

        cache = {}
        print("\n--- first run: nothing on disk, so everything backfills ---")
        plan_ = plan(names, parts, out, 2, cache)
        stats = run(conn, plan_, markets, out, 200, False, cache, dlog)
        log_universe(rows, names, list(dropped) + excluded, tally, dlog)
        log_plan(plan_, dlog)
        log_result(stats, False, dlog)

        print("\n--- the files ---")
        for f in sorted(out.iterdir()):
            print(f"  {f.name}")
        sample = out / ticksfile.filename("7203 JT", dt.date(2026, 9, 3))
        print(f"\n--- {sample.name} ---")
        for line in sample.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")

        print("\n--- the miss cache ---")
        misscache.save(out / "_no_data.csv", cache)
        for line in (out / "_no_data.csv").read_text(
                encoding="utf-8").splitlines():
            print(f"  {line}")
        print("  ZZZ SP resolved to a sym but qatt has never carried it.")
        print("  No empty files were written; these lines are the record.")

        print("\n--- second run, same day: everything is up to date ---")
        plan2 = plan(names, parts, out, 2, cache)
        stats2 = run(conn, plan2, markets, out, 200, False, cache, dlog)
        log_plan(plan2, dlog)
        log_result(stats2, False, dlog)
        print("  ZZZ SP was not re-queried: the cache says it was asked and "
              "had nothing.")

        print("\n--- a new day arrives ---")
        parts3 = parts + [dt.date(2026, 9, 4)]
        plan3 = plan(names, parts3, out, 2, cache)
        stats3 = run(conn, plan3, markets, out, 200, False, cache, dlog)
        log_plan(plan3, dlog)
        log_result(stats3, False, dlog)

        sha = out / ticksfile.filename("600000 CG", dt.date(2026, 9, 3))
        print(f"\n--- {sha.name} ---")
        for line in sha.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")

        print("\n=== --trace: ONE name, ONE day, every stage ===")
        print("The same stage functions the run above used.\n")

        class Args:
            trace, date, log, dry_run = "600000 C1", "2026-09-03", "", False

        real_connect = qattsource.connect
        qattsource.connect = lambda host, port: conn
        try:
            trace({"CROSSCODE_PATH": cc, "OUTPUT_DIR": out,
                   "MISS_CACHE_PATH": out / "_no_data.csv",
                   "EQUITY_MASTER_SERVER": "demo:1",
                   "QATT_SERVER": "demo:2", "MASTER_CHUNK": 5000},
                  Args(), logs.Log(stamps=False))
        finally:
            qattsource.connect = real_connect

    print("\nThe Shanghai name is spelt three ways and all three are right:")
    print("  600000 C1    in the crosscode")
    print("  600000.CH    the only shape equity_master and qatt answer to")
    print("  600000 CG    on disk, which is what the consumer reads")
    return 0


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

    D = dt.date

    print("historical_ticks --self-test")
    print("  (servers, paths and the strict loader are settings.py's - "
          "python settings.py --self-test)")

    print("\nbuilding equity_master candidates")

    class Row:
        def __init__(self, bbg, ticker="7203", market="TYO-MAIN"):
            self.bbg, self.ticker, self.market = bbg, ticker, market

    MK = marketcfg.load(HERE / "config" / "markets.csv")
    c = candidates([Row("7203 JT"), Row("7203 JE", market="JNX-MAIN"),
                    Row("BHP AU", "BHP", "ASX-MAIN"),
                    Row("600000 C1", "600000", "SHA-MAIN")], MK)
    check("one entry per code", sorted(c),
          ["600000 C1", "7203 JE", "7203 JT", "BHP AU"])
    check("each carrying all three shapes equity_master might answer to",
          c["7203 JT"], ("7203.JT", "7203 JT EQUITY", "7203.JP"))
    check("Shanghai's third candidate is the .CH that the other two cannot "
          "reach - the whole reason the pass exists",
          c["600000 C1"][2], "600000.CH")
    check("a market markets.csv does not list has no third candidate, and "
          "is simply not asked about on that pass", c["7203 JE"][2], "")
    check("three venue rows are three codes - they only collapse AFTER "
          "equity_master says they share a sym", len(c), 4)
    check("a repeated row makes one candidate, not two",
          len(candidates([Row("BHP AU"), Row("BHP AU")], MK)), 1)

    print("\nchunking")
    check("even split", [list(g) for g in chunked([1, 2, 3, 4], 2)],
          [[1, 2], [3, 4]])
    check("a short last chunk", [list(g) for g in chunked([1, 2, 3], 2)],
          [[1, 2], [3]])
    check("nothing chunks to nothing", list(chunked([], 5)), [])
    check("a chunk size of zero does not loop forever",
          [list(g) for g in chunked([1, 2], 0)], [[1], [2]])

    print("\nplanning, from an empty directory")

    class N:
        def __init__(self, bbg, sym):
            self.bbg, self.sym, self.mic, self.rows = bbg, sym, "X", ()

    import tempfile
    P = [D(2026, 9, 1), D(2026, 9, 2), D(2026, 9, 3)]
    toyota, bhp = N("7203 JT", "7203.JP"), N("BHP AU", "BHP.AU")

    with tempfile.TemporaryDirectory() as d:
        pl = plan([toyota, bhp], P, d, 2)
        check("both names backfill two days each",
              {k: [n.bbg for n in v] for k, v in pl["by_date"].items()},
              {D(2026, 9, 2): ["7203 JT", "BHP AU"],
               D(2026, 9, 3): ["7203 JT", "BHP AU"]})
        check("which is two reads, not four - the plan is by date, so one "
              "query serves every name that wants that day",
              len(pl["by_date"]), 2)

        ticksfile.write(Path(d) / ticksfile.filename("7203 JT", P[2]),
                        [], "XTKS", "")
        pl = plan([toyota, bhp], P, d, 2)
        check("the day it already has drops out of that date's read",
              [n.bbg for n in pl["by_date"][D(2026, 9, 3)]], ["BHP AU"])
        check("but the rest of its window is still wanted - the file it has "
              "does not make the name 'known' and stop the backfill",
              pl["per_name"]["7203 JT"], [D(2026, 9, 2)])
        check("so the older day is still read for both",
              [n.bbg for n in pl["by_date"][D(2026, 9, 2)]],
              ["7203 JT", "BHP AU"])

    print("\nplanning against the miss cache")
    with tempfile.TemporaryDirectory() as d:
        cache = {}
        misscache.record(cache, "BHP AU", "BHP.AU", D(2026, 9, 3))
        pl = plan([toyota, bhp], P, d, 2, cache)
        check("a date already known empty is not asked about again - the "
              "whole point of the cache",
              [n.bbg for n in pl["by_date"][D(2026, 9, 3)]], ["7203 JT"])
        check("but the day it has NOT been asked about still is",
              [n.bbg for n in pl["by_date"][D(2026, 9, 2)]],
              ["7203 JT", "BHP AU"])

        for day in P:
            misscache.record(cache, "BHP AU", "BHP.AU", day)
        pl = plan([toyota, bhp], P, d, 2, cache)
        check("a name qatt has never carried drops out entirely, and costs "
              "nothing on every run thereafter",
              pl["per_name"]["BHP AU"], [])
        check("while its neighbour is unaffected",
              pl["per_name"]["7203 JT"], [D(2026, 9, 2), D(2026, 9, 3)])

        check("no cache at all plans exactly as before - the argument is "
              "optional so nothing else had to change",
              plan([toyota], P, d, 2)["per_name"],
              plan([toyota], P, d, 2, {})["per_name"])

    print("\nrecording a miss during a run")

    class Silent:
        """kdb answers, and the answer is empty."""

        def __call__(self, q, *args):
            return []

    with tempfile.TemporaryDirectory() as d:
        cache = {}
        pl = plan([bhp], [D(2026, 9, 3)], d, 1, cache)
        stats = run(Silent(), pl, {}, d, 200, False, cache)
        check("nothing came back", stats["empty"], 1)
        check("no file was written - an empty CSV is not data",
              stats["files"], 0)
        check("and the directory is left clean",
              [f.name for f in Path(d).iterdir()], [])
        check("the miss is in the cache instead",
              misscache.tried(cache, "BHP AU"), {D(2026, 9, 3)})

        pl2 = plan([bhp], [D(2026, 9, 3)], d, 1, cache)
        stats2 = run(Silent(), pl2, {}, d, 200, False, cache)
        check("so the next run asks kdb nothing at all", stats2["reads"], 0)

    print("\na dry run records nothing")
    with tempfile.TemporaryDirectory() as d:
        cache = {}
        pl = plan([bhp], [D(2026, 9, 3)], d, 1, cache)
        run(Silent(), pl, {}, d, 200, True, cache)
        check("the cache is untouched, so --dry-run cannot poison it",
              cache, {})

    print("\nthe timezone label")

    class M:
        def __init__(self, tz):
            self.time_zone = tz

    class Named:
        def __init__(self, market):
            self.rows = (Row("X", "X", market),)

    check("comes from the market of the row that names the file",
          tz_label(Named("ASX-MAIN"), {"ASX-MAIN": M("AUS Eastern")}),
          "AUS Eastern")
    check("a market with no label writes none, rather than inventing one",
          tz_label(Named("ASX-MAIN"), {"ASX-MAIN": M("")}), "")
    check("nor does an unconfigured market",
          tz_label(Named("ZZZ-MAIN"), {}), "")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
