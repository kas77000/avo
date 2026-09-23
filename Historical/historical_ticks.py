#!/usr/bin/env python3
"""Trade prints out of kdb, one CSV per name per day.

Replaces a Bloomberg HistoricalTickDataRequest.  Our B-PIPE entitlement is
real-time only - `LimitUpDown/other/bpipe_history.py` is the probe that
established it - so the prints come from the plant's own store, qatt, and are
written in the shape the Bloomberg add-in was writing:

    raw-EAU AU-20260817.csv
    #Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern Standard Time
    09:31:33,0.105,1000,T,T,XASX

TWO STORES, ONE SHAPE.  Every finished day is an HDB partition and is asked
for by date.  Today is not a partition at all - one appears when the day is
done - so it lives on the RDB, a different server with no date column, and
only --today goes there.  A finished day is settled once; today is refetched
and rewritten every run, and never recorded as a miss, because a session
still running is not an answer.

ONE RULE, NOT TWO POPULATIONS.  Every name wants the last BACKFILL_DAYS
partitions minus whatever has already been TRIED, and the backfill and the
daily top-up fall out of that one subtraction.  "Tried" means two things:

    a file on disk           OUTPUT_DIR/<code>/, one folder per name
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
    python historical_ticks.py --today         also today, from the RDB
    python historical_ticks.py --date 2026-09-02   as if that were today
    python historical_ticks.py --from 2026-08-22 --to 2026-09-21   a range
    python historical_ticks.py --only "7203 JT"    one name, for a check
    python historical_ticks.py --venues "NSI-MAIN|BSE-MAIN"   those markets only
    python historical_ticks.py --venues "SET-MAIN" --compress_venues   into SET-MAIN.zip
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
import time
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


def plan(names, partitions, out_dir, backfill, cache=None,
         zipped=None) -> dict:
    """{date: [name, ...]} - who needs what, before anything is fetched.

    Inverted from per-name to per-date on purpose: qatt is partitioned by
    date, so one query per date serves every name that wants it, and a
    backfill of sixty days is sixty reads rather than sixty times the
    universe.

    A date counts as ALREADY TRIED if there is a file for it or a line in
    the miss cache.  Without the second half, a name qatt has never carried
    is re-asked for every day of the backfill on every run, forever."""
    cache, zipped = cache or {}, zipped or {}
    by_date, per_name = {}, {}
    for name in names:
        #  `zipped` is ticksfile.zipped_dates(): a day --compress_venues has
        #  moved into SET-MAIN.zip is as done as one still in its folder.
        have = (ticksfile.existing_dates(out_dir, name.crosscode_bbg,
                                         name.bbg)
                | zipped.get((ticksfile.folder(name.crosscode_bbg),
                              ticksfile.safe(name.bbg)), set())
                | misscache.tried(cache, name.bbg))
        want = ticksfile.days_wanted(have, partitions, backfill)
        per_name[name.bbg] = want
        for d in want:
            by_date.setdefault(d, []).append(name)
    return {"by_date": dict(sorted(by_date.items())), "per_name": per_name}


#  What the demo pretends kdb is stamped in, and settings.py's default: the
#  plant's own clock.
DEMO_ZONE = "China Standard Time"


def shift_for(markets, source_zone, log=None):
    """(date, name) -> seconds to move kdb's clock into the name's market.

    KDB STAMPS EVERY PRINT IN ONE ZONE - the plant's, Hong Kong - and each
    file is read as the exchange's own local time; its seventh header cell
    says which.  So the two are reconciled here, per market and per date,
    and a market with no TimeZone in config/markets.csv is left alone and
    said so once.

    Cached per (market, date): a run is thousands of names over tens of
    days, and a handful of markets."""
    seen, quiet = {}, set()

    def shift(date, name):
        market = venue_of(name)
        key = (market, date)
        if key in seen:
            return seen[key]
        target = marketcfg.tz_of(markets, market)
        if not target:
            if log and market not in quiet:
                quiet.add(market)
                log.warn(f"{market or '(no market)'} has no TimeZone in "
                         f"config/markets.csv: its files keep kdb's clock, "
                         f"{source_zone}")
            seen[key] = 0
            return 0
        seen[key] = marketcfg.shift_seconds(date, source_zone, target)
        return seen[key]

    return shift


def venue_of(name) -> str:
    """The FidessaMarket of the crosscode row that names the folder - the
    zip a name's files go into under --compress_venues."""
    for r in name.rows:
        if r.bbg == name.crosscode_bbg:
            return r.market
    return name.rows[0].market if name.rows else ""


def stage_compress(names, out_dir, dry_run, log):
    """--compress_venues: each venue's day files into <VENUE>.zip."""
    by_venue = {}
    for n in names:
        by_venue.setdefault(venue_of(n), set()).add(
            ticksfile.folder(n.crosscode_bbg))
    if "" in by_venue:
        log.warn(f"{len(by_venue.pop(''))} name(s) have no FidessaMarket and "
                 f"are left unzipped")
    for venue, folders in sorted(by_venue.items()):
        t = ticksfile.compress_venue(out_dir, venue, folders, dry_run)
        log.kv(t["zip"].name,
               f"{logs.thousands(t['added'])} added, "
               f"{logs.thousands(t['replaced'])} replaced",
               str(t["zip"]) + ("   NOT WRITTEN, --dry-run" if dry_run
                                else ""))


def stage_migrate(names, out_dir, dry_run, log):
    """Move anything written before there were folders, so a day already
    fetched is not fetched again.

    Silent when there is nothing loose, which is every run after the first.
    """
    slashed = ticksfile.migrate_slashed(
        out_dir, [(n.crosscode_bbg, n.bbg) for n in names], dry_run)
    if slashed:
        log.kv("slash codes renamed", logs.thousands(slashed),
               "LPN/F TB's files, from nested folders to LPN_F TB"
               + ("   NOT MOVED, --dry-run" if dry_run else ""))
    tally = ticksfile.migrate_flat(
        out_dir, {n.bbg: n.crosscode_bbg for n in names}, dry_run)
    if not any(tally.values()):
        return
    log.kv("older files moved into folders",
           logs.thousands(tally["moved"]),
           "written before the store had one folder per name"
           + ("   NOT MOVED, --dry-run" if dry_run else ""))
    if tally["already there"]:
        log.warn(f"{tally['already there']} loose file(s) name a day the "
                 f"folder already has. The folder's copy is the one that "
                 f"counts; the loose ones are left for you to look at.")
    if tally["unknown code"]:
        log.info(f"    {tally['unknown code']} of them are codes this "
                 f"crosscode no longer carries, and keep their own name "
                 f"as the folder")


def add_today(plan_, names, today) -> dict:
    """Put today in the plan for every name, whatever is on disk.

    TODAY IS NOT SUBTRACTED THE WAY A FINISHED DAY IS.  days_wanted skips a
    date that has a file, which is right for a partition - it cannot change
    again - and wrong for today, whose file is a session still running.  So
    --today always refetches and always rewrites, and the file settles when
    the day does.

    It is not in `partitions` either: a partition appears when the day is
    done, so today is never in the window plan() worked from."""
    by_date = dict(plan_["by_date"])
    by_date[today] = list(names)
    per_name = dict(plan_["per_name"])
    for name in names:
        per_name[name.bbg] = sorted(set(per_name.get(name.bbg, [])) |
                                    {today})
    return {"by_date": dict(sorted(by_date.items())), "per_name": per_name}


def tz_label(name, markets) -> str:
    """The seventh header cell, from config/markets.csv.

    Taken off the crosscode row that names the file, because that is the row
    whose market the file's clock belongs to.  Blank is normal until the
    probe has settled what the clock IS - see ticksfile.py."""
    m = markets.get(name.rows[0].market) if name.rows else None
    return getattr(m, "time_zone", "") if m else ""


#  WHAT "TOO BIG" LOOKS LIKE FROM HERE.  On 2026-09-22 the first read of a
#  30-day backfill - 200 syms of one Asian day, every column - came back as
#  ConnectionResetError: the server dropped the socket rather than answer.
#  A q process out of memory says 'wsfull; one over its limits says 'limit.
#  Every one of those means "ask for less", which is what run() does.
TOO_BIG = ("wsfull", "limit", "abort")


def too_big(e) -> bool:
    return (isinstance(e, (ConnectionError, TimeoutError))
            or any(k in str(e) for k in TOO_BIG))


def connect_again(host, port, log, tries=6, wait=10.0):
    """A fresh connection after a reset, waiting for a server that may be
    coming back up.  A process that dropped us may have been restarted, and
    asking at once would fail for the same reason."""
    for n in range(1, tries + 1):
        time.sleep(wait)
        try:
            return qattsource.connect(host, port)
        except Exception as e:                              # noqa: BLE001
            log.warn(f"reconnect {n}/{tries} to {host}:{port} failed: "
                     f"{str(e)[:80]}")
    raise ConnectionError(f"{host}:{port} did not come back after "
                          f"{tries} tries, {tries * wait:.0f}s")


def run(conn, plan_, markets, out_dir, chunk, dry_run, cache=None,
        log=None, live_conn=None, live_date=None, cols=None,
        reconnect=None, live_cols=None, depth=3, shift_of=None) -> dict:
    """Extract, transform, load - three stages at once, one chunk apiece.

        extract    this thread's own kdb connection; asks for one chunk,
                   hands the raw answer on untouched, asks for the next
        transform  raw answer -> formatted rows per sym (qattsource.shape)
        load       the calling thread: writes the files, records the misses,
                   owns the stats, the miss cache and the log

    ONE CONNECTION, BUT NEVER IDLE.  qatt answers one query at a time, so
    extract is one thread.  What the pipeline buys is that it is never
    waiting on us: while kdb works on chunk n, chunk n-1 is being formatted
    and n-2 written.  The slowest stage sets the pace, not the sum of all
    three.

    MEMORY IS BOUNDED BY `depth`, the size of each hand-off queue.  At most
    2 x depth + 3 chunks exist at once, however large the run.

    A name with prints gets a file.  A name with none gets a line in the
    miss cache INSTEAD - not an empty file.  Today (--today, live_date) is
    asked of the RDB and never cached as a miss: a session still running is
    not an answer.

    THE CLOCK IS THE NAME'S, NOT THE CHUNK'S.  kdb stamps every print in
    one zone and each file carries its own market's - see shift_for() - so
    shape() hands the time on as seconds and the load stage formats it with
    that name's shift.  shift_of(date, name) -> seconds.

    A READ THAT IS TOO BIG IS HALVED, NOT FATAL.  reconnect(live) opens a
    fresh connection - a reset socket is dead - and the same syms are asked
    again in half the chunk, which size is then kept for the rest of the
    run.  Only a single sym that still fails stops the run, and nothing
    about it is cached.  Any error in any stage stops all three and is
    raised here, in the caller's thread."""
    import queue
    import threading

    cache = cache if cache is not None else {}
    log = log or logs.Log(stamps=False, quiet=True)
    stats = {"files": 0, "rows": 0, "empty": 0, "reads": 0, "halved": 0}
    per_date = [(date, names, sorted({n.sym for n in names}))
                for date, names in plan_["by_date"].items()]

    if dry_run:
        size = max(1, int(chunk))
        for date, names, syms in per_date:
            reads = -(-len(syms) // size)
            stats["reads"] += reads
            log.info(f"{date}  {len(names)} names, {len(syms)} syms, "
                     f"{reads} read(s)")
        return stats

    stop = threading.Event()
    raw_q, shaped_q = queue.Queue(depth), queue.Queue(depth)

    def put(q, item):
        #  A put that gives up when the pipeline is stopping, so a stage
        #  blocked on a full queue cannot outlive a failure downstream.
        while not stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    def extract():
        nonlocal conn, live_conn
        try:
            size, reads = max(1, int(chunk)), 0

            def reads_left(day, left_today):
                return (-(-left_today // size)
                        + sum(-(-len(s) // size) for _d, _n, s in
                              per_date[day + 1:]))

            for day, (date, names, syms) in enumerate(per_date):
                live = live_conn is not None and date == live_date
                by_sym = {}
                for n in names:
                    by_sym.setdefault(n.sym, []).append(n)
                if not put(raw_q, ("info", f"{date}  {len(names)} names, "
                                   f"{len(syms)} syms, "
                                   f"{-(-len(syms) // size)} read(s)"
                                   + ("  - from the RDB, today is not a "
                                      "partition" if live else ""))):
                    return
                pos, i = 0, 0
                while pos < len(syms):
                    group = syms[pos:pos + size]
                    t0 = time.monotonic()
                    try:
                        raw = (qattsource.fetch_live_raw(
                                   live_conn, group, live_cols or cols)
                               if live else
                               qattsource.fetch_raw(conn, date, group, cols))
                    except Exception as e:                  # noqa: BLE001
                        if not too_big(e) or reconnect is None:
                            raise
                        if len(group) == 1:
                            raise RuntimeError(
                                f"qatt failed on ONE sym, {group[0]} on "
                                f"{date}: {e}.  Nothing smaller can be "
                                f"asked; nothing was cached for it.") from e
                        size = max(1, len(group) // 2)
                        put(raw_q, ("halved", f"{date}  {len(group)} syms "
                                    f"was too much for qatt "
                                    f"({type(e).__name__}: {str(e)[:80]}); "
                                    f"reconnecting and asking {size} at a "
                                    f"time from here on"))
                        if live:
                            live_conn = reconnect(True)
                        else:
                            conn = reconnect(False)
                        continue
                    pos += len(group)
                    i += 1
                    reads += 1
                    label = (f"chunk {i}/{i + -(-(len(syms) - pos) // size)}"
                             f"  (run {reads}/"
                             f"{reads + reads_left(day, len(syms) - pos)})")
                    if not put(raw_q, ("chunk", date, live,
                                       [n for s in group for n in by_sym[s]],
                                       raw, label, time.monotonic() - t0)):
                        return
            put(raw_q, ("done",))
        except BaseException as e:                          # noqa: BLE001
            put(raw_q, ("error", e))

    def transform():
        try:
            while True:
                item = raw_q.get()
                if item[0] == "chunk":
                    kind, date, live, names, raw, label, t_read = item
                    t0 = time.monotonic()
                    by_sym = qattsource.shape(raw)
                    del raw
                    #  The MIC and the clock are both the NAME's, so two
                    #  names on one sym get their own rows either way.
                    files = []
                    for n in names:
                        stamp = qattsource.clock_maker(
                            shift_of(date, n) if shift_of else 0)
                        files.append((n, [(stamp(r[0]),) + r[1:] + (n.mic,)
                                          for r in by_sym.get(n.sym, ())]))
                    del by_sym
                    item = (kind, date, live, files, label, t_read,
                            time.monotonic() - t0)
                if not put(shaped_q, item) or item[0] in ("done", "error"):
                    return
        except BaseException as e:                          # noqa: BLE001
            put(shaped_q, ("error", e))

    workers = [threading.Thread(target=f, name=f"historical-{f.__name__}",
                                daemon=True) for f in (extract, transform)]
    for w in workers:
        w.start()
    try:
        while True:
            item = shaped_q.get()
            kind = item[0]
            if kind == "done":
                break
            if kind == "error":
                raise item[1]
            if kind == "info":
                log.info(item[1])
            elif kind == "halved":
                stats["halved"] += 1
                log.warn(item[1])
            else:
                _, date, live, files, label, t_read, t_shape = item
                t0 = time.monotonic()
                before = dict(stats)
                load_chunk(date, files, live, markets, out_dir, cache, stats)
                stats["reads"] += 1
                del files
                log.info(f"{date}  {label}  "
                         f"{stats['files'] - before['files']} files, "
                         f"{stats['empty'] - before['empty']} empty, "
                         f"{logs.thousands(stats['rows'] - before['rows'])} "
                         f"rows  read {t_read:.1f}s, transform "
                         f"{t_shape:.1f}s, write "
                         f"{time.monotonic() - t0:.1f}s")
    finally:
        stop.set()
    return stats


def load_chunk(date, files, live, markets, out_dir, cache, stats):
    """LOAD: write each name's file, or record its miss."""
    for name, rows in files:
        if not rows:
            #  kdb answered, and the answer was empty.  That is a fact
            #  about the data and it is worth remembering.  A query that
            #  RAISED never gets here - it takes the whole run down -
            #  which is what keeps an outage out of the cache.
            #
            #  EXCEPT FOR TODAY, which is not a fact yet.  A name that
            #  has not traded by 11am may trade at 2pm, and a miss
            #  cached now is never asked again.  Today is remembered by
            #  nothing, which is also why --today always refetches.
            stats["empty"] += 1
            if not live:
                misscache.record(cache, name.bbg, name.sym, date)
            continue
        path = ticksfile.path(out_dir, name.crosscode_bbg, name.bbg, date)
        stats["rows"] += ticksfile.write_rows(path, rows,
                                              tz_label(name, markets))
        stats["files"] += 1


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

def parse_venues(spec: str, known):
    """'NSI-MAIN|BSE-MAIN' -> the FidessaMarkets to work on, or [] for all.

    REFUSED BY NAME if markets.csv has never heard of one.  A typo that
    silently matched nothing would look exactly like a market with no rows.
    The same rule, and the same spelling, as LimitUpDown's --venues."""
    out = [v.strip() for v in (spec or "").split("|") if v.strip()]
    bad = [v for v in out if v not in known]
    if bad:
        raise ValueError(
            f"unknown venue(s) {bad}; config/markets.csv has "
            f"{', '.join(sorted(known))}")
    return out


def stage_crosscode(path, only, log, venues=()):
    """Read the security master, optionally down to some venues, then to
    one name.

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
    if venues:
        before = len(rows)
        rows = [r for r in rows if r.market in venues]
        log.kv("--venues", f"{logs.thousands(len(rows))} of "
                           f"{logs.thousands(before)} rows", "|".join(venues))
        if not rows:
            log.fail(f"no crosscode row is on {'|'.join(venues)}")
            return None, dropped
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


def parse_day(text, flag):
    """'2026-09-02' -> a date, or None when not given.  ValueError names the
    flag, so a typo is refused before any server is asked anything."""
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{flag} {text!r} is not a date; write it as "
                         f"YYYY-MM-DD") from None


def stage_partitions(conn, date_text, log, from_text=""):
    """What days qatt actually holds, capped at --to (--date) when given and
    starting at --from when given."""
    parts = qattsource.partitions(conn)
    if not parts:
        log.fail("qatt holds no partitions at all")
        return None
    try:
        cutoff = parse_day(date_text, "--to")
        start = parse_day(from_text, "--from")
    except ValueError as e:
        log.fail(str(e))
        return None
    if cutoff:
        parts = [d for d in parts if d <= cutoff]
        if not parts:
            log.fail(f"qatt has no partition on or before {cutoff}")
            return None
    if start:
        parts = [d for d in parts if d >= start]
        if not parts:
            log.fail(f"qatt has no partition from {start}"
                     + (f" to {cutoff}" if cutoff else ""))
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
    rows, _dropped = stage_crosscode(cfg["CROSSCODE_PATH"], a.trace, log,
                                     a.venue_list)
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
        have = ticksfile.existing_dates(out_dir, n.crosscode_bbg, n.bbg)
        cache = misscache.load(cfg["MISS_CACHE_PATH"]
                               or Path(out_dir) / "_no_data.csv")
        log.kv("file on disk",
               "yes" if date in have else "no",
               str(ticksfile.path(out_dir, n.crosscode_bbg, n.bbg, date)))
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
    #  WHAT q RECEIVES, not what python sent.  `where date=d` against a
    #  partition column is a comparison between two types, and if the date
    #  arrives as a timestamp it is false for every row - no error, no rows.
    log.kv("date reaches q as", qattsource.q_type_of(date))
    log.kv("syms reach q as", qattsource.q_type_of([n.sym for n in names]))
    #  The query names no column, so a wrong TIME_FIELD is a silent None
    #  rather than a q error.  Print what qatt actually has.
    try:
        cols = qattsource.sample_columns(conn, date)
    except Exception as e:                                  # noqa: BLE001
        cols = []
        log.warn(f"could not read qatt's columns: {e}")
    if cols:
        log.kv("qatt columns", ", ".join(cols))
        missing = [f for f in (qattsource.TIME_FIELD,) + qattsource.TICK_FIELDS
                   if f not in cols]
        if missing:
            log.warn(f"qatt has no column called {', '.join(missing)} - the "
                     f"CSV will be blank there. TIME_FIELD is a placeholder; "
                     f"pick the right name from the list above and set it in "
                     f"qattsource.py, or run qatt_time_probe.py.")
        else:
            log.kv("the assumed columns", "all present")

    shift_of = shift_for(markets, cfg["KDB_TIMEZONE"], log)
    for n in names:
        log.kv("clock", f"{cfg['KDB_TIMEZONE']} -> "
                        f"{marketcfg.tz_of(markets, venue_of(n)) or '(none)'}",
               f"{shift_of(date, n) / 3600:+.1f}h on {date}")
    fetched = qattsource.fetch_ticks(conn, date, [n.sym for n in names],
                                     shift=shift_of(date, names[0]))
    for n in names:
        got = fetched.get(n.sym, [])
        log.kv("rows returned", logs.thousands(len(got)), n.sym)
        if not got:
            log.warn(f"{n.sym} had NO prints on {date}. A real run would "
                     f"write a miss-cache line and no file.")
            #  An empty answer names no predicate, so ask again with one
            #  removed at a time rather than leaving the reader to guess.
            log.info("    narrowing it down:")
            for label, finding in qattsource.diagnose(
                    conn, date, [n.sym for n in names]):
                log.kv(f"  {label}", finding)
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
            path = ticksfile.path(out_dir, n.crosscode_bbg, n.bbg, date)
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
    p.add_argument("--date", "--to", dest="date", default="",
                   help="the last day, YYYY-MM-DD, as if it were the newest; "
                        "--to and --date are the same flag")
    p.add_argument("--from", dest="date_from", default="",
                   help="the first day, YYYY-MM-DD.  Every qatt day from "
                        "here to --to (or the newest) is wanted; replaces "
                        "--backfill")
    p.add_argument("--only", default="",
                   help="one code - the crosscode's (600000 C1) or the "
                        "file's (600000 CG)")
    p.add_argument("--venues", default="", metavar="VENUE|VENUE",
                   help="these FidessaMarkets only, pipe separated, e.g. "
                        '"NSI-MAIN|BSE-MAIN".  Names must be in '
                        "config/markets.csv.")
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
    p.add_argument("--today", action="store_true",
                   help="also fetch today, from QATT_RDB_SERVER.  Today is "
                        "not an HDB partition, so a normal run cannot see "
                        "it.  Always refetched and rewritten - a session "
                        "still running is not a finished day.")
    p.add_argument("--retry-misses", action="store_true",
                   help="ignore the miss cache for this run and rebuild it "
                        "from what today's run actually finds")
    p.add_argument("--compress_venues", "--compress-venues",
                   dest="compress_venues", action="store_true",
                   help="after writing, move each venue's files into "
                        "OUTPUT_DIR/<VENUE>.zip (SET-MAIN.zip) and delete "
                        "them.  An existing zip keeps what it holds and gains "
                        "only what was missing; days in it are never fetched "
                        "again.")
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

    markets = marketcfg.load(HERE / "config" / "markets.csv")
    try:
        a.venue_list = parse_venues(a.venues, markets)
    except ValueError as e:
        print(f"FAIL  --venues: {e}", file=sys.stderr)
        return 2
    try:
        start, end = parse_day(a.date_from, "--from"), parse_day(a.date, "--to")
    except ValueError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2
    if start and end and start > end:
        print(f"FAIL  --from {start} is after --to {end}", file=sys.stderr)
        return 2
    #  TWO WAYS TO SAY HOW FAR BACK, SO ONLY ONE AT A TIME.  Which one won
    #  would otherwise be a rule nobody remembers.
    if start and a.backfill is not None:
        print("FAIL  --from and --backfill both say how far back to go; "
              "give one", file=sys.stderr)
        return 2

    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "OUTPUT_DIR")
        em_host, em_port = settings.server(cfg, "EQUITY_MASTER_SERVER")
        q_host, q_port = settings.server(cfg, "QATT_SERVER")
        #  Only --today needs the RDB, so an unset QATT_RDB_SERVER is not a
        #  problem until it is asked for - and then it is a hard stop, not
        #  a run that quietly fetches no today at all.
        rdb = settings.server(cfg, "QATT_RDB_SERVER") if a.today else None
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

    log.step(1, "crosscode")
    rows, dropped = stage_crosscode(crosscode_path, a.only, log, a.venue_list)
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
    parts = stage_partitions(conn, a.date, log, a.date_from)
    if parts is None:
        return 1
    try:
        have = qattsource.columns(conn)
        cols = qattsource.select_columns(have)
    except ValueError as e:
        log.fail(str(e))
        return 1
    log.kv("columns asked for", ", ".join(cols),
           f"of the {len(have)} qatt has")
    if a.date_from:
        #  The window IS the range: every partition left after the cut.
        backfill = len(parts)
        log.kv("--from", f"{parts[0]} .. {parts[-1]}",
               f"{logs.thousands(backfill)} qatt day(s)")

    log.step(5, "what is already tried")
    stage_migrate(names, out_dir, a.dry_run, log)
    cache = {} if a.retry_misses else misscache.load(miss_path)
    log.kv("miss cache", f"{logs.thousands(misscache.count(cache))} pairs",
           f"{miss_path}" + ("   IGNORED, --retry-misses"
                             if a.retry_misses else ""))
    try:
        zipped = ticksfile.zipped_dates(out_dir)
    except ValueError as e:
        log.fail(str(e))
        return 1
    if zipped:
        log.kv("days in venue zips",
               logs.thousands(sum(len(v) for v in zipped.values())),
               "count as done, like files on disk")
    plan_ = plan(names, parts, out_dir, backfill, cache, zipped)
    today = None
    if a.today:
        today = dt.date.today()
        plan_ = add_today(plan_, names, today)
        log.kv("today", str(today),
               f"from the RDB at {rdb[0]}:{rdb[1]}; refetched and rewritten")
    log_plan(plan_, log)

    log.step(6, "fetch and write")
    live_conn = qattsource.connect(*rdb) if a.today else None
    if live_conn is not None:
        #  The RDB is another server and may name its columns differently;
        #  asking it the HDB's list would be a q error at the worst moment.
        live_cols = qattsource.select_columns(qattsource.columns(live_conn))
        if live_cols != cols:
            log.warn(f"the RDB's columns differ: {', '.join(live_cols)}")
    else:
        live_cols = cols

    def reconnect(live):
        return connect_again(*(rdb if live else (q_host, q_port)), log=log)

    source_zone = cfg["KDB_TIMEZONE"]
    try:
        shift_of = shift_for(markets, source_zone, log)
        sample = sorted({(marketcfg.tz_of(markets, venue_of(n)),
                          shift_of(parts[-1], n)) for n in names})
    except ValueError as e:
        log.fail(str(e))
        return 1
    log.kv("kdb's clock", source_zone, "KDB_TIMEZONE; every print is stamped "
                                       "in it")
    for target, secs in sample:
        log.kv("  -> " + (target or "(no TimeZone, left as kdb has it)"),
               f"{secs // 3600:+d}h" if secs % 3600 == 0
               else f"{secs / 3600:+.1f}h", f"on {parts[-1]}")

    stats = run(conn, plan_, markets, out_dir, chunk, a.dry_run, cache, log,
                live_conn, today, cols, reconnect, live_cols,
                shift_of=shift_of)
    if not a.dry_run:
        n = misscache.save(miss_path, cache)
        log.kv("miss cache now", f"{logs.thousands(n)} pairs", str(miss_path))

    if a.compress_venues:
        log.step(7, "compress venues")
        stage_compress(names, out_dir, a.dry_run, log)

    log.step(8 if a.compress_venues else 7, "result")
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
            if q.startswith(".Q.p"):
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
        stats = run(conn, plan_, markets, out, 200, False, cache, dlog,
                    shift_of=shift_for(markets, DEMO_ZONE))
        log_universe(rows, names, list(dropped) + excluded, tally, dlog)
        log_plan(plan_, dlog)
        log_result(stats, False, dlog)

        print("\n--- the files ---")
        for f in sorted(out.iterdir()):
            print(f"  {f.name}")
        sample = ticksfile.path(out, "7203 JT", "7203 JT",
                                dt.date(2026, 9, 3))
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
        stats2 = run(conn, plan2, markets, out, 200, False, cache, dlog,
                     shift_of=shift_for(markets, DEMO_ZONE))
        log_plan(plan2, dlog)
        log_result(stats2, False, dlog)
        print("  ZZZ SP was not re-queried: the cache says it was asked and "
              "had nothing.")

        print("\n--- a new day arrives ---")
        parts3 = parts + [dt.date(2026, 9, 4)]
        plan3 = plan(names, parts3, out, 2, cache)
        stats3 = run(conn, plan3, markets, out, 200, False, cache, dlog,
                     shift_of=shift_for(markets, DEMO_ZONE))
        log_plan(plan3, dlog)
        log_result(stats3, False, dlog)

        #  The folder keeps the crosscode's C1; the file takes the MIC's CG.
        sha = ticksfile.path(out, "600000 C1", "600000 CG",
                             dt.date(2026, 9, 3))
        print(f"\n--- {sha.name} ---")
        for line in sha.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")

        print("\n=== --trace: ONE name, ONE day, every stage ===")
        print("The same stage functions the run above used.\n")

        class Args:
            trace, date, log, dry_run = "600000 C1", "2026-09-03", "", False
            venue_list, date_from = [], ""

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

    print("\n--venues")
    check("pipe separated, spaces forgiven",
          parse_venues(" NSI-MAIN | BSE-MAIN ", MK), ["NSI-MAIN", "BSE-MAIN"])
    check("nothing given means every venue", parse_venues("", MK), [])
    try:
        parse_venues("NSE-MAIN", MK)
        check("a venue markets.csv does not know raised", False, True)
    except ValueError as e:
        check("a venue markets.csv does not know is refused, by name",
              "NSE-MAIN" in str(e), True)

    class _Quiet:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text("BloombergCode,FidessaMarket\n"
                      "GOLDSTAR IS,NSI-MAIN\n500325 IB,BSE-MAIN\n"
                      "7203 JT,TYO-MAIN\n", encoding="utf-8")
        got, _ = stage_crosscode(cc, "", _Quiet(), ["NSI-MAIN", "BSE-MAIN"])
        check("only rows on the named venues are kept",
              [r.bbg for r in got], ["GOLDSTAR IS", "500325 IB"])
        got, _ = stage_crosscode(cc, "", _Quiet(), [])
        check("and with no --venues, all of them", len(got), 3)
        got, _ = stage_crosscode(cc, "", _Quiet(), ["ASX-MAIN"])
        check("a venue with no rows in the crosscode stops the run", got, None)
        got, _ = stage_crosscode(cc, "7203 JT", _Quiet(), ["NSI-MAIN"])
        check("--only outside --venues finds nothing", got, None)

    print("\n--from and --to")
    check("a day is a date", parse_day("2026-09-02", "--from"),
          dt.date(2026, 9, 2))
    check("not given is None", parse_day("", "--from"), None)
    try:
        parse_day("02/09/2026", "--from")
        check("a malformed day raised", False, True)
    except ValueError as e:
        check("a malformed day is refused, naming the flag",
              "--from" in str(e), True)

    #  Five qatt days with a weekend in the middle, as partitions really are.
    held = [dt.date(2026, 9, d) for d in (3, 4, 7, 8, 9)]
    real_partitions = qattsource.partitions
    qattsource.partitions = lambda _conn: list(held)
    try:
        check("--from and --to keep the days between, both ends included",
              stage_partitions(None, "2026-09-08", _Quiet(), "2026-09-04"),
              [dt.date(2026, 9, 4), dt.date(2026, 9, 7), dt.date(2026, 9, 8)])
        check("--from a weekend starts at the next day qatt holds",
              stage_partitions(None, "", _Quiet(), "2026-09-05"),
              [dt.date(2026, 9, 7), dt.date(2026, 9, 8), dt.date(2026, 9, 9)])
        check("--from alone runs to the newest day",
              stage_partitions(None, "", _Quiet(), "2026-09-09"),
              [dt.date(2026, 9, 9)])
        check("a range qatt holds nothing in stops the run",
              stage_partitions(None, "2026-09-06", _Quiet(), "2026-09-05"),
              None)
        check("and with neither, every day qatt holds",
              stage_partitions(None, "", _Quiet()), held)
    finally:
        qattsource.partitions = real_partitions

    check("--from with --backfill is refused before any server is asked",
          main(["--from", "2026-09-01", "--backfill", "5"]), 2)
    check("and so is a --from after its --to",
          main(["--from", "2026-09-10", "--to", "2026-09-01"]), 2)
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
        def __init__(self, bbg, sym, crosscode_bbg=None):
            self.bbg, self.sym, self.mic, self.rows = bbg, sym, "X", ()
            #  Same code in both places for everything but China, which is
            #  exactly what the real Name carries.
            self.crosscode_bbg = bbg if crosscode_bbg is None else crosscode_bbg

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

        ticksfile.write(ticksfile.path(d, "7203 JT", "7203 JT", P[2]),
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

    print("\none chunk in memory at a time")

    class Ordered:
        """Answers every sym asked, and notes which files were already on
        disk each time it is asked again."""

        def __init__(self, out):
            self.out, self.on_disk = out, []

        def __call__(self, q, date, syms):
            self.on_disk.append(sorted(
                f.parent.name for f in Path(self.out).rglob("*.csv")))
            return [{"sym": s, qattsource.TIME_FIELD:
                     dt.datetime(2026, 9, 3, 1, 0, 0),
                     "price": 1.5, "size": 100, "cond": "", "ex": ""}
                    for s in syms]

    with tempfile.TemporaryDirectory() as d:
        pl = plan([toyota, bhp], [D(2026, 9, 3)], d, 1, {})
        conn = Ordered(d)

        class Lines:
            def __init__(self):
                self.lines = []

            def info(self, msg=""):
                self.lines.append(msg)

        said = Lines()
        stats = run(conn, pl, {}, d, 1, False, {}, said)
        check("every chunk says where it is in the day and in the run",
              [l.split("  ")[1:3] for l in said.lines if "chunk" in l],
              [["chunk 1/2", "(run 1/2)"], ["chunk 2/2", "(run 2/2)"]])
        check("a chunk of one makes one read per name", stats["reads"], 2)
        check("both files are written", stats["files"], 2)

    with tempfile.TemporaryDirectory() as d:
        #  EXTRACT RUNS AHEAD OF LOAD, but only so far.  Twenty chunks, and
        #  a queue depth of one: no more than 2 x 1 + 3 may be asked for and
        #  not yet on disk, however fast kdb answers.
        twenty = [N(f"{k} JT", f"{k}.JP") for k in range(2000, 2020)]
        conn = Ordered(d)
        run(conn, plan(twenty, [D(2026, 9, 3)], d, 1, {}), {}, d, 1,
            False, {}, depth=1)
        ahead = [k - len(seen) for k, seen in enumerate(conn.on_disk)]
        check("extract never runs more than 2 x depth + 3 chunks ahead of "
              "what is written - memory is bounded, not the whole day",
              max(ahead) <= 5, True)
        check("and all twenty are written", len(list(Path(d).rglob("*.csv"))),
              20)

    print("\na read too big for qatt is halved, not fatal")

    class Fussy:
        """Resets the connection for any read of more than `most` syms,
        as qatt did on 2026-09-22."""

        def __init__(self, most, error=ConnectionResetError):
            self.most, self.error, self.asked = most, error, []

        def __call__(self, q, date, syms):
            self.asked.append(len(syms))
            if len(syms) > self.most:
                raise self.error("An existing connection was forcibly "
                                 "closed by the remote host")
            return [{"sym": s, qattsource.TIME_FIELD:
                     dt.datetime(2026, 9, 3, 1, 0, 0),
                     "price": 1.5, "size": 100, "cond": "", "ex": ""}
                    for s in syms]

    class Warned:
        def __init__(self):
            self.warnings = []

        def info(self, msg=""):
            pass

        def warn(self, msg=""):
            self.warnings.append(msg)

    many = [N(f"{k} JT", f"{k}.JP") for k in range(1000, 1008)]
    with tempfile.TemporaryDirectory() as d:
        fussy = Fussy(2)
        reopened = []
        pl = plan(many, [D(2026, 9, 2), D(2026, 9, 3)], d, 2, {})
        w = Warned()
        stats = run(fussy, pl, {}, d, 8, False, {}, w,
                    reconnect=lambda live: reopened.append(live) or fussy)
        check("every name still gets its file, both days",
              stats["files"], 16)
        check("8 was too many, so 4, then 2 - and 2 is kept for the rest "
              "of the run rather than failing again on the next day",
              fussy.asked, [8, 4, 2, 2, 2, 2, 2, 2, 2, 2])
        check("a fresh connection each time, because a reset one is dead",
              reopened, [False, False])
        check("and each halving is a warning in the log", len(w.warnings), 2)

    with tempfile.TemporaryDirectory() as d:
        cache = {}
        pl = plan([toyota], [D(2026, 9, 3)], d, 1, cache)
        try:
            run(Fussy(0), pl, {}, d, 8, False, cache, Warned(),
                reconnect=lambda live: Fussy(0))
            check("one sym that still fails raised", False, True)
        except RuntimeError as e:
            check("one sym that still fails stops the run, naming it",
                  "7203.JP" in str(e), True)
        check("and is NOT cached as a miss - a failure is not an answer",
              cache, {})

    with tempfile.TemporaryDirectory() as d:
        pl = plan(many, [D(2026, 9, 3)], d, 1, {})
        try:
            run(Fussy(0, ValueError), pl, {}, d, 8, False, {}, Warned(),
                reconnect=lambda live: None)
            check("a q error that is not about size raised", False, True)
        except ValueError:
            check("a q error that is NOT about size is not retried - "
                  "halving a 'type does not fix it", True, True)

    check("'wsfull and 'limit read as too big too",
          (too_big(Exception("wsfull")), too_big(Exception("limit")),
           too_big(Exception("type"))), (True, True, False))

    print("\nkdb's clock is Hong Kong's; the file carries the market's")

    class R0:
        def __init__(self, bbg, market):
            self.bbg, self.market = bbg, market

    HK = "China Standard Time"
    shift_of = shift_for(MK, HK)
    tokyo, mumbai = N("7203 JT", "7203.JP"), N("RELIANCE IS", "RELIANCE.IN")
    sydney, home = N("BHP AU", "BHP.AU"), N("5 HK", "5.HK")
    tokyo.rows = (R0("7203 JT", "TYO-MAIN"),)
    mumbai.rows = (R0("RELIANCE IS", "NSI-MAIN"),)
    sydney.rows = (R0("BHP AU", "ASX-MAIN"),)
    home.rows = (R0("5 HK", "HKG-MAIN"),)
    check("Tokyo is an hour ahead of the plant",
          shift_of(D(2026, 9, 3), tokyo), 3600)
    check("Mumbai two and a half behind",
          shift_of(D(2026, 9, 3), mumbai), -9000)
    check("Hong Kong itself does not move at all",
          shift_of(D(2026, 9, 3), home), 0)
    check("SYDNEY FOLLOWS ITS DAYLIGHT SAVING, so the same market is +2 in "
          "July and +3 in January - which is why the date decides and a "
          "fixed offset per market would be wrong",
          (shift_of(D(2026, 7, 15), sydney), shift_of(D(2026, 1, 15), sydney)),
          (7200, 10800))

    with tempfile.TemporaryDirectory() as d:
        day = D(2026, 9, 3)

        class AtNine:
            """kdb answers 09:00 Hong Kong for every name."""

            def __call__(self, q, date, syms):
                return [{"sym": s, qattsource.TIME_FIELD:
                         dt.timedelta(hours=9), "price": 1.5, "size": 100,
                         "cond": "T", "ex": "T"} for s in syms]

        run(AtNine(), plan([tokyo, mumbai, home], [day], d, 1, {}), MK, d,
            10, False, {}, shift_of=shift_of)

        def first(name):
            return ticksfile.path(d, name.crosscode_bbg, name.bbg, day) \
                .read_text(encoding="utf-8").splitlines()[1].split(",")[0]

        check("one 09:00 print from kdb is 10:00 in Tokyo's file",
              first(tokyo), "10:00:00")
        check("06:30 in Mumbai's", first(mumbai), "06:30:00")
        check("and 09:00 in Hong Kong's own", first(home), "09:00:00")
        check("the header still names the zone the times are now in",
              ticksfile.path(d, tokyo.crosscode_bbg, tokyo.bbg, day)
              .read_text(encoding="utf-8").splitlines()[0].split(",")[-1],
              "Tokyo Standard Time")

    print("\n--compress_venues, end to end")

    class R:
        def __init__(self, bbg, market):
            self.bbg, self.market = bbg, market

    class Quiet2:
        counts = {logs.WARN: 0}

        def __getattr__(self, _name):
            return lambda *a, **k: None

    ptt, aot = N("PTT TB", "PTT.TB"), N("AOT TB", "AOT.TB")
    ptt.rows = (R("PTT TB", "SET-MAIN"), R("PTT TB2", "OTHER-MAIN"))
    aot.rows = (R("AOT TB", "SET-MAIN"),)
    check("a name's venue is the market of the row that names its folder",
          venue_of(ptt), "SET-MAIN")
    with tempfile.TemporaryDirectory() as d:
        days = [D(2026, 9, 3), D(2026, 9, 4)]
        run(Ordered(d), plan([ptt, aot], days[:1], d, 5, {}), {}, d, 10,
            False, {})
        stage_compress([ptt, aot], d, False, Quiet2())
        check("the run's files end up in SET-MAIN.zip and nowhere else",
              sorted(e.name for e in Path(d).iterdir()), ["SET-MAIN.zip"])
        zipped = ticksfile.zipped_dates(d)
        pl = plan([ptt, aot], days, d, 5, {}, zipped)
        check("the next run asks only for the day the zip does not have",
              sorted(pl["by_date"]), [days[1]])
        check("without looking in the zip it would fetch both days again - "
              "which is what the zip lookup is for",
              sorted(plan([ptt, aot], days, d, 5, {})["by_date"]), days)
        run(Ordered(d), pl, {}, d, 10, False, {})
        stage_compress([ptt, aot], d, False, Quiet2())
        check("and that day is added to the same zip",
              sorted(ticksfile.zipped_dates(d)[("PTT TB", "PTT TB")]), days)

    print("\na day generated before the folders existed is not redone")
    with tempfile.TemporaryDirectory() as d:
        old_file = Path(d) / ticksfile.filename("7203 JT", P[2])
        old_file.write_text("x", encoding="utf-8")

        class Quiet:
            counts = {logs.WARN: 0}
            def kv(self, *a, **k): pass
            def info(self, *a, **k): pass
            def warn(self, *a, **k): pass

        pl = plan([toyota], P, d, 1, {})
        check("without the move, the day looks untried and would be "
              "fetched and rewritten", P[2] in pl["per_name"]["7203 JT"],
              True)

        stage_migrate([toyota], d, False, Quiet())
        check("after it, the file is in the name's folder",
              (Path(d) / "7203 JT" / old_file.name).is_file(), True)
        pl = plan([toyota], P, d, 1, {})
        check("and the day is not asked for again, which is the point",
              P[2] in pl["per_name"]["7203 JT"], False)

    print("\ntoday, which comes from the other server")

    class Which:
        """Records which connection was asked, and with what."""

        def __init__(self, rows=None):
            self.rows, self.calls = rows or [], []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return self.rows

    today = D(2026, 9, 8)
    with tempfile.TemporaryDirectory() as d:
        pl = plan([bhp], [D(2026, 9, 3)], d, 1, {})
        check("a plan off finished partitions does not contain today",
              today in pl["by_date"], False)

        pl = add_today(pl, [bhp], today)
        check("--today puts it there", today in pl["by_date"], True)
        check("without disturbing the finished day",
              sorted(pl["by_date"]), [D(2026, 9, 3), today])

        hdb, rdb = Which(), Which()
        run(hdb, pl, {}, d, 200, False, {}, None, rdb, today)
        check("the finished day went to the HDB, and only that day",
              [a[0] for _, a in hdb.calls], [D(2026, 9, 3)])
        check("today went to the RDB instead",
              len(rdb.calls), 1)
        check("and was asked for with syms alone - no date to send",
              [len(a) for _, a in rdb.calls], [1])

    with tempfile.TemporaryDirectory() as d:
        #  THE RDB GETS THE QATT KEY, not the crosscode code.  `7203 JT` is
        #  what the crosscode calls it and `7203.JP` is what qatt answers
        #  to; the conversion is universe.resolve_sym's, and the live path
        #  reads name.sym exactly as the dated one does.
        pl = add_today(plan([toyota], [], d, 1, {}), [toyota], today)
        hdb, rdb = Which(), Which()
        run(hdb, pl, {}, d, 200, False, {}, None, rdb, today)
        check("the RDB is asked for 7203.JP, not 7203 JT",
              [list(a[0]) for _, a in rdb.calls], [["7203.JP"]])

    with tempfile.TemporaryDirectory() as d:
        #  a file already on disk for today must NOT settle it
        pl = add_today(plan([bhp], [], d, 1, {}), [bhp], today)
        p = ticksfile.path(d, "BHP AU", "BHP AU", today)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        pl2 = add_today(plan([bhp], [], d, 1, {}), [bhp], today)
        check("a file for today does not count as tried - the session is "
              "still running",
              today in pl2["by_date"], True)

    with tempfile.TemporaryDirectory() as d:
        cache = {}
        pl = add_today(plan([bhp], [], d, 1, cache), [bhp], today)
        run(Silent(), pl, {}, d, 200, False, cache, None, Silent(), today)
        check("an empty answer for today is NOT cached as a miss - a name "
              "that has not traded by 11am may trade at 2pm",
              cache, {})

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
