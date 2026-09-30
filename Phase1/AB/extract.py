#!/usr/bin/env python3
"""The day's AB extract for Nova: one zip that replaces what B-PIPE gave.

    EXPORT_DIR/phase1-20260925.zip
        manifest.csv    key,value               what, when, how many
        master.csv      BloombergCode,sym,...   the crosscode's equity_master rows
        ticks.csv       sym,time,price,size,cond,ex   condensed prints, kdb's clock
        closes.csv      sym,close,source,reason one row per sym asked
        quote_only.csv  sym,time,bid,ask,cond   a sym with no print, its quote
        equity.csv      BloombergCode,sym,PX_LAST,...   equity_master fields
        ladders.csv     BloombergCode,sym,price,ticksize   raw tick ladders

Nova's R jobs (Phase1/Nova) turn it into the Historical tick files,
limitUpDown.csv and TradingData.csv.  Nova has no kdb, which is why every
lookup that needs one is done here and carried in the zip.

--for luld|td|ticks makes a zip for some of those jobs only, named for them
(phase1-20260925-luld-td.zip) and holding only their members.  Without
ticks no print and no quote is read: the closes come from
qattsource.last_q, a row per sym and cond, and are the ticks' closes.

THE DAY AND THE SERVER.  With no argument the day is TODAY and qatt and
quote are read from the RDB (QATT_RDB_SERVER), which holds today only and
has no date column.  --date reads the HDB (QATT_SERVER): the newest
partition on or before that date, as qatt_export did.  equity_master and
the tick ladders always come from EQUITY_MASTER_SERVER, at the newest
equity_master date on or before the day.

THE UNIVERSE IS THE CROSSCODE, filtered to our markets: a row is kept when
its Bloomberg exchange code is in config/close_conditions.csv, whatever its
Type.  The kept rows are resolved to qatt syms exactly
as qatt_export.py did (equity_master in three passes, config/markets.csv as
the fallback).

TICKS ARE CONDENSED IN q.  A ticks.csv line is one sym, second, price, cond
and ex, with the size of every print behind it summed - see
qattsource.ticks_q, which falls back to one line per print (and says why in
the log) only when qatt lacks one of those columns.  The manifest's
`prints` counts those lines.

THE CLOSE is closes.py's rule: the last line carrying one of its market's
close codes (config/close_conditions.csv); else, if it traded, its last
traded price (reason last-trade); else equity_master's PX_LAST (no-trades);
else no close at all.  It is worked out while the ticks stream,
one chunk of syms at a time, and only the answer is kept - a day of prints
is never held at once.  Every PX_LAST close and every missing close is one
`!!` line in the log; last-trade closes are counted per market.  The run
ends with a table per Bloomberg exchange code.

MARKET BY MARKET, RESUMABLE.  Every file goes to the day's staging folder,
EXPORT_DIR/phase1-YYYYMMDD/, as soon as it is ready: master.csv,
equity.csv and ladders.csv once, then per market (the exchange code of a
sym's primary CrossCode row) ticks-, closes- and quote_only-<MKT>.csv.
Each is written as .part and renamed, the ticks file last, so a market
whose three files exist is done.  A rerun of the same day reads back what
is staged and skips the done markets; --fresh deletes the folder first, and
so does a run whose source, equity_master date or CrossCode differs from
what stage.csv says the folder was built from.
The zip is then assembled from the folder, which is left in place.

FAIL LOUDLY.  A kdb failure, or equity_master answering nothing for the
reference fetch, stops the run and leaves no zip.  So does a day with no
print at all - a zip of it would give every name a permanent no-trading day
in Nova - and a no-argument run on a weekend, when the RDB holds no trading
day.  The zip is written as
.part and renamed, so a zip under its real name is always a finished one.

    python extract.py                     today, from the RDB
    python extract.py --date 2026-09-25   that day, from the HDB
    python extract.py --date 2026-09-25 --rdb   that day's zip, read from
                                          the RDB (just after midnight)
    python extract.py --fresh             the day again, from scratch
    python extract.py --market "NZ|HK"    only those markets, read again
    python extract.py --for "luld|td"     a zip for those jobs: no ticks read
    python extract.py --no-mail           no mail at the end of this run
    python extract.py --log extract.log   tee the log to a file
    python extract.py --self-test         checks, no kdb
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import io
import os
import re
import shutil
import sys
import time
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import closes
import crosscode
import logs
import mailer
import marketcfg
import qattsource
import refdata
import runreport
import settings
import universe

HERE = Path(__file__).resolve().parent

MANIFEST, MASTER, TICKS = "manifest.csv", "master.csv", "ticks.csv"
CLOSES, QUOTE_ONLY = "closes.csv", "quote_only.csv"
EQUITY, LADDERS = "equity.csv", "ladders.csv"
#  Staged only: what the folder was built from, and every fetched PX_LAST.
STAGE, PX = "stage.csv", "px.csv"
MEMBERS = (MANIFEST, MASTER, TICKS, CLOSES, QUOTE_ONLY, EQUITY, LADDERS)

TICK_COLUMNS = ["sym", "time", "price", "size", "cond", "ex"]
MASTER_COLUMNS = ["BloombergCode"] + list(qattsource.MASTER_FIELDS)
CLOSE_COLUMNS = ["sym", "close", "source", "reason"]
QUOTE_COLUMNS = ["sym", "time", "bid", "ask", "cond"]
EQUITY_COLUMNS = ["BloombergCode", "sym"] + list(refdata.EQUITY_FIELDS)
LADDER_COLUMNS = ["BloombergCode", "sym", "price", "ticksize"]

#  The reasons closes.resolve gives short of a closing print, in the
#  summary's order: the last trade (source qatt), PX_LAST (equity_master).
REASONS = ("last-trade", "no-trades", "none-before-cutoff")

#  What "too big" looks like from here - see historical_ticks.run.
TOO_BIG = ("wsfull", "limit", "abort")


class ExtractError(Exception):
    """The run stops here and writes no zip."""


#  WHAT A ZIP IS FOR (--for), in this fixed order, and the members each
#  Nova job reads.  historical.r reads closes too, for its "sym AB asked
#  about" check.  The manifest is in every zip.
USES = ("luld", "td", "ticks")
USE_MEMBERS = {"luld": (MASTER, CLOSES, EQUITY, LADDERS),
               "td": (MASTER, CLOSES, EQUITY),
               "ticks": (MASTER, TICKS, CLOSES, QUOTE_ONLY)}


def parse_uses(text) -> tuple:
    """--for's value: uses joined by |, any case, into USES order.  Empty
    is all three.  An unknown one is a ValueError."""
    got = [u.strip().lower() for u in (text or "").split("|") if u.strip()]
    bad = [u for u in got if u not in USES]
    if bad:
        raise ValueError(f"--for {'|'.join(bad)}: not one of "
                         f"{'|'.join(USES)}")
    return tuple(u for u in USES if u in got) or USES


def zip_members(uses) -> list:
    """The zip's members for these uses: the manifest, then the union, in
    MEMBERS order."""
    need = {m for u in uses for m in USE_MEMBERS[u]}
    return [MANIFEST] + [m for m in MEMBERS if m in need]


def bundle_name(date, uses=USES) -> str:
    """phase1-YYYYMMDD.zip for all three uses, else the uses after the
    date: phase1-YYYYMMDD-luld-td.zip."""
    tail = "" if tuple(uses) == USES else "-" + "-".join(uses)
    return f"phase1-{date:%Y%m%d}{tail}.zip"


def qatt_server(date, rdb=False):
    """(setting, source) for qatt and quote: the RDB for today or with
    --rdb, the HDB for a --date."""
    return (("QATT_SERVER", "hdb") if date and not rdb
            else ("QATT_RDB_SERVER", "rdb"))


def quote_server(date, rdb=False) -> str:
    """The setting for the quote table's own process, for the same mode as
    qatt_server.  Blank in local_settings.py means qatt's server."""
    return "QUOTE_SERVER" if date and not rdb else "QUOTE_RDB_SERVER"


#  The RDB's own date: what day it holds.  A plain expression.
ZD_Q = ".z.D"


def rdb_date(conn, day, where, log):
    """Log the RDB's .z.D next to the day asked for; a different one is a
    !! line, never a stop - with --date --rdb the user chose to read the
    RDB.  Returns .z.D, or None when it cannot be read."""
    try:
        zd = qattsource._as_date(conn(ZD_Q))
    except Exception as e:                                  # noqa: BLE001
        zd, why = None, f"{type(e).__name__}: {str(e)[:80]}"
    else:
        why = "not a date"
    if zd is None:
        log.warn(f"could not read the RDB's .z.D on {where} ({why}); "
                 f"going on")
        return None
    log.kv("RDB date", str(zd), f"asked for {day}")
    if zd != day:
        log.warn(f"the RDB is on {zd}, not {day}: the prints are whatever "
                 f"day the RDB holds, filed under {day}")
    return zd


#  One cheap question, asked once per connection before any market: is the
#  table this job reads there at all?  A plain expression, not a lambda.
TABLES_Q = "tables[]"


def require_table(conn, table, where, hint, log) -> None:
    """Stop, before anything is staged, when `table` is not on `conn`.  A
    server that will not list its tables is only a warning - the reads
    themselves will say more."""
    try:
        have = {qattsource.text(t) for t in
                qattsource._iter(qattsource._atom(conn(TABLES_Q)))}
    except Exception as e:                                  # noqa: BLE001
        log.warn(f"could not list the tables on {where} "
                 f"({type(e).__name__}: {str(e)[:80]}); going on")
        return
    if table not in have:
        raise ExtractError(f"the {table} table is not on {where}; set "
                           f"{hint} to the process that has it")
    log.kv(f"{table} table", "found", where)


def safe_code(code) -> str:
    """A market code as a file name: only [A-Za-z0-9_-], anything else
    becomes _, and no code at all is NONE."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", code or "") or "NONE"


#  LastTradeBefore is written in HKT, the clock kdb stamps qatt in.
HKT = "China Standard Time"


def cut_seconds(date, hkt, kdb_tz) -> int:
    """A LastTradeBefore, HH:MM or HH:MM:SS in HKT, as seconds of day in
    kdb's clock (KDB_TIMEZONE) on that date: itself when kdb is on HKT,
    else shifted by marketcfg's one offset per day, taken at noon.  A
    malformed time is a ValueError."""
    m = re.fullmatch(r"(\d\d?):(\d\d)(?::(\d\d))?", (hkt or "").strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59 or int(m[3] or 0) > 59:
        raise ValueError(f"{hkt!r} is not HH:MM or HH:MM:SS")
    secs = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)
    return (secs + marketcfg.shift_seconds(date, HKT, kdb_tz)) % 86_400


def px_or_none(value):
    """PX_LAST as closes.resolve wants it: the text, or None when it is
    blank, not a number, or <= 0 - a zero last price is not a price."""
    try:
        return value if value and Decimal(value) > 0 else None
    except InvalidOperation:
        return None


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


def universe_rows(rows, conditions) -> tuple:
    """(kept, {reason: Counter}): the CrossCode rows this extract covers.

    A row is ours when its Bloomberg exchange code (the last word of its
    BloombergCode) is a BBGCode of config/close_conditions.csv - whatever
    its Type: warrants and the rest stay in.  The CrossCode is the whole
    firm's; without this the day ran over some 400 exchange codes.
    Dropped rows are counted per exchange code, for the log."""
    kept, dropped = [], {"exchange code not ours": collections.Counter()}
    for r in rows:
        if r.bbg_ext in conditions:
            kept.append(r)
        else:
            dropped["exchange code not ours"][r.bbg_ext] += 1
    return kept, dropped


def wanted_syms(rows, master, markets) -> list:
    """Every qatt sym the crosscode resolves to, once each."""
    return sorted({s for s, _src in
                   (universe.resolve_sym(r, master, markets) for r in rows)
                   if s})


def too_big(e) -> bool:
    return (isinstance(e, (ConnectionError, TimeoutError))
            or any(k in str(e) for k in TOO_BIG))


def fetch_chunks(conn, date, syms, cols, size, reconnect, log, last=False,
                 cut=None):
    """{sym: rows} per chunk of syms, in order.  `date` None reads the RDB.
    A read that is too big is halved and asked again on a fresh connection,
    and the smaller size is kept - as in historical_ticks.run.  `size` is a
    number, or a {"n": number} shared across calls, which keeps the halved
    size for every later market too.  One sym that still fails stops the
    export: a zip missing a name would look exactly like a quiet day.

    `last` reads qattsource.last_q instead of the ticks - the closes
    without the prints - shaped by shape_last into the same {sym: rows};
    `cut` (seconds, kdb's clock) its rows at or before a cutoff."""
    state = size if isinstance(size, dict) else {"n": size}
    size, pos, reads = max(1, int(state["n"])), 0, 0
    while pos < len(syms):
        group = syms[pos:pos + size]
        t0 = time.monotonic()
        try:
            if last:
                raw = qattsource.fetch_last_raw(conn, date, group, cut)
            elif date is None:
                raw = qattsource.fetch_live_raw(conn, group, cols)
            else:
                raw = qattsource.fetch_raw(conn, date, group, cols)
        except Exception as e:                              # noqa: BLE001
            if not too_big(e):
                raise
            if len(group) == 1:
                raise RuntimeError(f"qatt failed on ONE sym, {group[0]} on "
                                   f"{date or 'the RDB'}: {e}") from e
            size = state["n"] = max(1, len(group) // 2)
            log.warn(f"{len(group)} syms was too much for qatt "
                     f"({type(e).__name__}: {str(e)[:80]}); reconnecting "
                     f"and asking {size} at a time from here on")
            conn = reconnect()
            continue
        by_sym = (qattsource.shape_last(raw) if last
                  else qattsource.shape(raw, cols=cols))
        del raw
        pos += len(group)
        reads += 1
        log.info(f"read {reads}  {pos:,}/{len(syms):,} syms  "
                 f"{sum(len(v) for v in by_sym.values()):,} lines  "
                 f"{time.monotonic() - t0:.1f}s")
        yield by_sym
        by_sym = None           # one chunk alive at a time


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


def group_syms(rows, master, markets) -> dict:
    """{qatt sym: the crosscode rows that resolve to it}, in crosscode
    order - wanted_syms, keeping which rows made each sym."""
    out = {}
    for r in rows:
        s, _src = universe.resolve_sym(r, master, markets)
        if s:
            out.setdefault(s, []).append(r)
    return out


def exts_of(rows) -> list:
    """The Bloomberg exchange codes of these rows, first seen first."""
    out = []
    for r in rows:
        if r.bbg_ext and r.bbg_ext not in out:
            out.append(r.bbg_ext)
    return out


def market_of(rows, master) -> str:
    """The one exchange code a sym is counted under in the summary: its
    primary (equity_master's EQY_PRIM_EXCH_SHRT) when the crosscode carries
    it, else the first code seen.  7203 JT and 7203 JE count once, as JT."""
    exts = exts_of(rows)
    for r in rows:
        prim = (master.get(r.bbg) or {}).get("EQY_PRIM_EXCH_SHRT", "")
        if prim in exts:
            return prim
    return exts[0] if exts else ""


def stage_dir(export_dir, day) -> Path:
    """The day's staging folder: EXPORT_DIR/phase1-YYYYMMDD/."""
    return Path(export_dir) / f"phase1-{day:%Y%m%d}"


def set_aside(stage, log) -> None:
    """Move an old staging folder out of the way, then try to delete it.

    NEVER DELETE-THEN-CREATE THE SAME NAME.  On a network share (SMB) a
    removed folder stays "delete pending" for a moment, and creating the
    same name straight after fails with FileExistsError - the user's second
    live run died exactly so.  A rename is immediate, so the folder is
    renamed to <name>.stale-HHMMSS first (a numbered suffix if that is
    taken or the rename fails), and only then removed, best effort.  What
    cannot be removed is harmless: no run ever reads a .stale- folder."""
    stamp = f"{stage.name}.stale-{dt.datetime.now():%H%M%S}"
    aside, errors = None, []
    for n in range(20):
        name = stage.with_name(stamp + (f"-{n}" if n else ""))
        if name.exists():
            continue
        try:
            os.replace(stage, name)
        except OSError as e:
            errors.append(f"{name.name}: {e}")
            continue
        aside = name
        break
    if aside is None:
        raise ExtractError(f"could not move the old staging folder {stage} "
                           f"aside: {'; '.join(errors[-3:])}")
    log.info(f"old staging folder moved to {aside.name}")
    shutil.rmtree(aside, ignore_errors=True)
    if aside.exists():
        log.warn(f"{aside.name} could not be removed; it is harmless, "
                 f"delete it by hand")


def parse_markets(text) -> list:
    """--market's value: codes joined by |, trimmed, upper-cased, each once,
    as Nova's historical.r --market takes them."""
    out = []
    for c in (text or "").split("|"):
        c = c.strip().upper()
        if c and c not in out:
            out.append(c)
    return out


def resolve_markets(names, rows, conditions) -> tuple:
    """(codes, unknown) for --market's names, upper-cased already.

    A name that is a BBGCode of close_conditions.csv is that code; else a
    FidessaMarket (Row.market, case-insensitive) stands for the Bloomberg
    exchange codes of the universe rows on it - NZE-MAIN -> NZ.  A name that
    is neither is `unknown`.  `codes` is first-seen order, each once."""
    codes, unknown = [], []
    for name in names:
        if name in conditions:
            found = [name]
        else:
            found = [r.bbg_ext for r in rows
                     if (r.market or "").strip().upper() == name]
        if not found:
            unknown.append(name)
        for c in found:
            if c not in codes:
                codes.append(c)
    return codes, unknown


def market_done(files) -> bool:
    return all(f.exists() for f in files)


def any_print(stage, mkts) -> bool:
    """Whether any sym of these markets traded - a close from qatt - done
    now or by an earlier run alike.  Read from the closes, which every use
    stages: a sym with a print always closes from qatt."""
    for m in mkts:
        for r in read_csv(market_files(stage, m)[1]):
            if r["source"] == "qatt":
                return True
    return False


def done_for(files, uses) -> bool:
    """A market is done for these uses when the staged files they need
    exist: ticks needs all three, luld and td the closes."""
    return market_done(files if "ticks" in uses else files[1:2])


def weekend(date, today):
    """The day's name when a no-argument run falls on a Saturday or a
    Sunday, else None."""
    if date is None and today.weekday() >= 5:
        return f"{today:%A}"
    return None


def market_files(stage, mkt) -> tuple:
    """(ticks, closes, quote_only) of one market.  ticks is renamed into
    place LAST, so a market is done only when all three exist."""
    return tuple(stage / f"{n}-{mkt}.csv"
                 for n in ("ticks", "closes", "quote_only"))


def _part(path) -> Path:
    return path.with_name(path.name + ".part")


def write_csv(path, header, rows) -> None:
    """Write path as .part and rename it, so a file under its real name is
    always complete.  A .part left by a crash is simply overwritten."""
    part = _part(path)
    with part.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    os.replace(part, path)


def read_csv(path) -> list:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_ticks(fh, chunks, seen) -> dict:
    """Stream one market's ticks into the open text file `fh`, and return
    the counts.

    `chunks` is fetch_chunks' answer.  One sym is only ever in one chunk,
    which keeps each sym's prints CONTIGUOUS.  A row is a condensed line -
    one second, price, cond and ex, its size summed (qattsource.ticks_q) -
    and `prints` counts those lines.  `seen(sym, rows)` is called
    once per sym with prints, while its rows are still in hand - that is
    where the close is worked out - and the rows are dropped with the
    chunk."""
    stats = {"prints": 0, "syms with prints": 0}
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
            seen(sym, rows)
    return stats


SUMMARY_COLUMNS = ("syms", "ticks", "qatt") + REASONS + ("no-close",
                                                         "quote-only")


def log_summary(table, log) -> None:
    widths = [max(6, len(c)) + 2 for c in SUMMARY_COLUMNS]
    log.info(f"{'market':<8}" + "".join(f"{c:>{w}}" for c, w in
                                        zip(SUMMARY_COLUMNS, widths)))
    total = dict.fromkeys(SUMMARY_COLUMNS, 0)
    for market in sorted(table):
        for c in SUMMARY_COLUMNS:
            total[c] += table[market][c]
    for market, row in sorted(table.items()) + [("all", total)]:
        log.info(f"{market:<8}" + "".join(f"{row[c]:>{w}}" for c, w in
                                          zip(SUMMARY_COLUMNS, widths)))


def _csv(rows) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def assemble(stage, out, mkts, head, uses=USES) -> tuple:
    """Write the zip from the staging folder, and return (manifest, table).

    Only the members `uses` need (zip_members).  master, equity and ladders
    go in as they are.  ticks, closes and quote_only are the markets' files
    concatenated under one header, streamed, so a whole day is never held.
    The manifest's counts, and the summary table per market, are counted
    from the pieces on the way - so a market done by an earlier run is
    counted exactly like one done now.  A sym with prints is a sym whose
    close came from qatt, so `ticks` and `syms with prints` come from the
    closes, whatever the uses; `prints` and `quote only` need the ticks,
    and are blank in a zip without them.  `head` is the manifest's first
    keys; the counts and `exported at` follow.  Written as .part and
    renamed."""
    want = zip_members(uses)
    table = {m: dict.fromkeys(SUMMARY_COLUMNS, 0) for m in mkts}
    part = _part(out)
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for name in (MASTER, EQUITY, LADDERS):
                if name in want:
                    z.write(stage / name, name)

            prints = ""
            if TICKS in want:
                prints = 0
                with z.open(TICKS, "w", force_zip64=True) as raw:
                    fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                    fh.write(_csv([TICK_COLUMNS]))
                    for m in mkts:
                        with market_files(stage, m)[0].open(
                                encoding="utf-8", newline="") as src:
                            next(src, None)
                            for line in src:
                                fh.write(line)
                                prints += 1
                    fh.flush()
                    fh.detach()

            def concat(name, header, index, count):
                with z.open(name, "w") as raw:
                    fh = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                    w = csv.writer(fh)
                    w.writerow(header)
                    for m in mkts:
                        for r in read_csv(market_files(stage, m)[index]):
                            count(table[m], r)
                            w.writerow([r[c] for c in header])
                    fh.flush()
                    fh.detach()

            def count_close(row, r):
                row["syms"] += 1
                if r["source"] == "qatt":
                    row["ticks"] += 1           # it traded
                if r["source"] == "qatt" and not r["reason"]:
                    row["qatt"] += 1            # a closing print
                elif r["reason"] in REASONS:
                    row[r["reason"]] += 1
                else:
                    row["no-close"] += 1

            def count_quote(row, r):
                row["quote-only"] += 1

            concat(CLOSES, CLOSE_COLUMNS, 1, count_close)
            if QUOTE_ONLY in want:
                concat(QUOTE_ONLY, QUOTE_COLUMNS, 2, count_quote)

            def total(c):
                return sum(row[c] for row in table.values())

            manifest = dict(head)
            manifest.update({
                "prints": prints,
                "syms with prints": total("ticks"),
                "quote only": total("quote-only") if QUOTE_ONLY in want
                else "",
                #  Four disjoint counts that add up to "syms asked":
                #  a closing print, the last trade, PX_LAST, nothing.
                "closes from qatt": total("qatt"),
                "closes from last trade": total("last-trade"),
                "closes from equity_master": total("no-trades")
                + total("none-before-cutoff"),
                "no close": total("no-close"),
                "exported at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"})
            z.writestr(MANIFEST, _csv([["key", "value"]] + [
                [k, str(v)] for k, v in manifest.items()]))
        os.replace(part, out)
    except BaseException:
        if part.exists():
            part.unlink()
        raise
    return manifest, table


def read_quotes(conns, qday, syms, log) -> dict:
    """fetch_quotes on the quote connection (qatt's when none is set).  A
    read that drops is asked once more on a fresh connection."""
    conn = conns.get("quote") or conns["qatt"]
    try:
        return refdata.fetch_quotes(conn, qday, syms)
    except Exception as e:                                  # noqa: BLE001
        again = conns.get("quote_reconnect")
        if not (too_big(e) and again):
            raise
        log.warn(f"quote read of {len(syms)} syms dropped "
                 f"({type(e).__name__}); reconnecting and asking again")
        conns["quote"] = again()
        return refdata.fetch_quotes(conns["quote"], qday, syms)


def build(cfg, date, conns, log, today=None, markets=None, conditions=None,
          fresh=False, rdb=False, only=None, uses=None, cutoffs=None,
          report=None):
    """Stage the day market by market, then write
    EXPORT_DIR/phase1-YYYYMMDD.zip, and return its path.

    `date` None is today from the RDB; a date is the HDB, or with `rdb`
    that date's zip from the RDB's undated queries.  `conns` holds
    the open connections - "em" for equity_master and the ladders, "qatt"
    for the prints, "quote" for the quotes (qatt's when absent) - and
    "reconnect" / "quote_reconnect", which open fresh ones, and "names",
    how the qatt and quote connections are named in an error.  `markets` and `conditions`
    default to config/.

    RESUMABLE.  Every file goes to EXPORT_DIR/phase1-YYYYMMDD/ as soon as it
    is ready.  master, equity and ladders already there are read back, not
    fetched; a market whose three files are there is skipped.  `fresh`
    deletes the folder first.

    `only` (--market) is a list of exchange codes or FidessaMarket names
    (resolve_markets): only those markets are
    read, each REDONE even if staged; the rest are not touched.  Everything
    before the markets stays on the full universe.  The zip is written only
    once every market is staged; until then build returns None.  Raises ExtractError, or whatever kdb raised,
    and then no zip is written; what was staged stays for the rerun.

    `uses` (--for) is some of USES, all three by default.  Without ticks
    no print is read: each market's closes come from qattsource.last_q, a
    row per sym and cond, the same closes the ticks give; no quote either.
    A market is done for the uses when their files are staged (done_for),
    so a luld/td run reuses a ticks run's closes, and a ticks run after a
    luld/td run reads, the full way, the markets that lack ticks.

    `report`, a runreport.RunReport, is filled as the run goes, for the
    mail main() sends at the end.

    `cutoffs` is closes.load_cutoffs': BBGCode -> LastTradeBefore, from
    config/ with the conditions, none when the conditions are given.  The
    market's last trade is then its last print at or before that time,
    HKT, in kdb's clock for the day."""
    today = today or dt.date.today()
    uses = tuple(uses) if uses else USES
    if not uses or any(u not in USES for u in uses):
        raise ExtractError(f"--for {'|'.join(uses)}: each must be one of "
                           f"{'|'.join(USES)}")
    uses = tuple(u for u in USES if u in uses)
    ticks = "ticks" in uses
    rp = report if report is not None else runreport.RunReport(uses, today)
    rp.uses = uses
    if markets is None:
        markets = marketcfg.load(HERE / "config" / "markets.csv")
    if conditions is None:
        conditions = closes.load_conditions(
            HERE / "config" / "close_conditions.csv")
        if cutoffs is None:
            cutoffs = closes.load_cutoffs(
                HERE / "config" / "close_conditions.csv")
    cutoffs = cutoffs or {}
    q_name, source = qatt_server(date, rdb)
    em = conns["em"]
    #  How each connection is named in an error: the setting, and in a real
    #  run its host:port.  Quote is qatt's unless a quote server is set.
    names = {"qatt": q_name, "quote": q_name, **conns.get("names", {})}

    log.step(1, "crosscode")
    rows, dropped = crosscode.load(cfg["CROSSCODE_PATH"])
    log.kv("crosscode", logs.thousands(len(rows)) + " rows",
           str(cfg["CROSSCODE_PATH"]))
    for e in dropped:
        if e.rows:
            log.warn(f"{logs.thousands(len(e.rows))} rows dropped: {e.reason}")
    everything = len(rows)
    rp.universe = {"rows": everything + sum(len(e.rows) for e in dropped),
                   "dropped": [(e.reason, len(e.rows), "")
                               for e in dropped if e.rows]}
    rows, dropped = universe_rows(rows, conditions)
    rp.universe["kept"] = len(rows)
    rp.universe["dropped"] += [
        (reason, sum(c.values()), ", ".join(
            f"{k or '(blank)'} {v:,}" for k, v in c.most_common(10)))
        for reason, c in dropped.items() if c]
    log.kv("universe", logs.thousands(len(rows)) + " rows",
           f"of {logs.thousands(everything)}, the exchange codes of "
           f"close_conditions.csv")
    for reason, counts in dropped.items():
        if counts:
            log.kv("dropped", logs.thousands(sum(counts.values())) + " rows",
                   f"{reason}: " + ", ".join(
                       f"{k or '(blank)'} {logs.thousands(v)}"
                       for k, v in counts.most_common(10))
                   + (" ..." if len(counts) > 10 else ""))
    if not rows:
        raise ExtractError("no CrossCode row is ours: none has an exchange "
                           "code of close_conditions.csv")
    if only:
        #  Before kdb is asked anything: codes, or FidessaMarket names
        #  resolved on the universe's rows.
        codes, unknown = resolve_markets(only, rows, conditions)
        if unknown:
            raise ExtractError(
                f"--market {'|'.join(unknown)}: neither an exchange code of "
                f"close_conditions.csv nor a FidessaMarket of the universe's "
                f"CrossCode rows")
        said = "|".join(only)
        if codes != only:
            said += " -> " + "|".join(codes)
        log.kv("--market", said,
               f"{len(codes)} of {len(conditions)} markets this run")
        only = codes
    #  Each cutoff must read as a time: checked before kdb is asked.
    for code, hkt in sorted(cutoffs.items()):
        try:
            said = qattsource.hms(cut_seconds(today, hkt, HKT))
        except ValueError as e:
            raise ExtractError(f"LastTradeBefore of {code}: {e}") from e
        log.kv("last trade before", f"{code} {said} HKT")
    log.kv("--for", "|".join(uses),
           "-> " + bundle_name(dt.date(1, 1, 1), uses).replace(
               "00010101", "YYYYMMDD"))

    log.step(2, f"qatt, from the {source.upper()}")
    require_table(conns["qatt"], "qatt", names["qatt"],
                  "QATT_RDB_SERVER / QATT_SERVER", log)
    if ticks:
        require_table(conns.get("quote") or conns["qatt"], "quote",
                      names["quote"], "QUOTE_RDB_SERVER / QUOTE_SERVER", log)
    zd = None
    if source == "rdb":
        #  --date D --rdb: D names the day, and stands for today in the
        #  equity_master lookup; the RDB is read as it is.
        day = date or today
        today = day
        log.kv("day", str(day), "given, read from the RDB" if date
               else "today")
        zd = rdb_date(conns["qatt"], day, names["qatt"], log)
    else:
        parts = qattsource.partitions(conns["qatt"])
        day = pick_day(parts, today, date)
        if day is None:
            raise ExtractError(f"qatt has no partition on or before {date}")
        log.kv("day", str(day), f"newest partition on or before {date}")
    qday = None if source == "rdb" else day
    #  The cutoffs in kdb's clock, for this day: HKT itself, normally.
    try:
        cut_kdb = {code: cut_seconds(day, hkt, cfg["KDB_TIMEZONE"])
                   for code, hkt in cutoffs.items()}
    except ValueError as e:
        raise ExtractError(f"LastTradeBefore: {e}") from e
    rp.cutoffs = [(code, qattsource.hms(cut_seconds(day, cutoffs[code],
                                                    HKT)),
                   qattsource.hms(cut_kdb[code])) for code in sorted(cut_kdb)]
    for code in sorted(cut_kdb):
        if cut_kdb[code] != cut_seconds(day, cutoffs[code], HKT):
            log.kv("last trade before", f"{code} = "
                   f"{qattsource.hms(cut_kdb[code])} kdb",
                   f"kdb's clock is {cfg['KDB_TIMEZONE']}")
    try:
        cols = qattsource.select_columns(qattsource.columns(conns["qatt"]))
    except ValueError as e:
        raise ExtractError(str(e)) from e
    log.kv("columns asked for", ", ".join(cols))
    note = qattsource.condense_note(cols)
    if note:
        log.warn(f"ticks NOT condensed: {note}")
    else:
        log.kv("ticks condensed", "one line per sym, second, price, cond, "
               "ex; size summed")

    log.step(3, "equity_master")
    #  days_back bounds the server-side fallback (.z.D-n) at the trade day
    #  too; the default of 1 would take a date AFTER an older --date day.
    master_date = qattsource.resolve_master_date(
        em, day, days_back=(today - day).days)
    log.kv("equity_master date", master_date)
    rp.trade_date = day
    rp.run = {"source": source, "equity_master date": str(master_date),
              "time field": qattsource.TIME_FIELD,
              "kdb timezone": cfg["KDB_TIMEZONE"]}

    #  WHAT THE FOLDER WAS BUILT FROM.  It is keyed by the trade day only, so
    #  an RDB run's markets must not pass for an HDB run's of the same day,
    #  nor another equity_master date's or another CrossCode's.
    built = {"source": source, "equity_master date": str(master_date),
             "crosscode": hashlib.sha1("\n".join(sorted(
                 {r.bbg for r in rows})).encode("utf-8")).hexdigest()[:12]}
    stage = stage_dir(cfg["EXPORT_DIR"], day)
    if stage.exists() and not fresh:
        try:
            was = {r["key"]: r["value"] for r in read_csv(stage / STAGE)}
        except (OSError, KeyError):
            was = {}
        if was != built:
            said = ", ".join(f"{k} {was.get(k, '?')}"
                             for k in ("source", "equity_master date"))
            if was.get("crosscode") != built["crosscode"]:
                said += ", another CrossCode"
            log.warn(f"staged folder was built from {said}; starting the "
                     f"day fresh")
            fresh = True
    if fresh and stage.exists():
        set_aside(stage, log)
    stage.mkdir(parents=True, exist_ok=True)
    if not (stage / STAGE).exists():
        write_csv(stage / STAGE, ["key", "value"], built.items())
    log.kv("staging", str(stage))
    rp.stage = stage
    chunk = int(cfg["MASTER_CHUNK"])
    if (stage / MASTER).exists():
        master = {r["BloombergCode"]: {f: r[f] for f in
                                       qattsource.MASTER_FIELDS}
                  for r in read_csv(stage / MASTER)}
        log.kv("matched", f"{logs.thousands(len(master))} codes",
               "read back from master.csv")
    else:
        cands = candidates(rows, markets)
        master, hits = {}, {"sym_bpipe": 0, "sym_mbpipe": 0, "sym": 0}
        keys = list(cands)
        for i in range(0, len(keys), chunk):
            got = qattsource.fetch_master(
                em, master_date, {k: cands[k] for k in keys[i:i + chunk]})
            if got:
                master.update(got["rows"])
                for k, v in got["hits"].items():
                    hits[k] += v
        log.kv("matched", f"{logs.thousands(len(master))} of "
                          f"{logs.thousands(len(cands))}",
               ", ".join(f"{k} {v}" for k, v in hits.items()))
        write_csv(stage / MASTER, MASTER_COLUMNS, (
            [b] + [m.get(f, "") for f in qattsource.MASTER_FIELDS]
            for b, m in sorted(master.items())))
    groups = group_syms(rows, master, markets)
    syms = sorted(groups)
    codes = {s: closes.codes_for_sym(exts_of(g), conditions)
             for s, g in groups.items()}
    by_market, odd = {}, {}
    for s in syms:
        code = market_of(groups[s], master)
        if not re.fullmatch(r"[A-Z0-9]{2,4}", code):
            odd.setdefault(code, groups[s][0].bbg)
        by_market.setdefault(safe_code(code), []).append(s)
    log.kv("syms to export", logs.thousands(len(syms)),
           f"in {len(by_market)} markets")
    rp.markets_total = len(by_market)
    #  A sym's cutoff is its market's - the code it is filed under.
    cut_of = {s: cut_kdb[m] for m, ms in by_market.items() if m in cut_kdb
              for s in ms}
    if odd:
        log.warn(f"{len(odd)} odd exchange codes, filed under a safe name: "
                 + ", ".join(f"{c!r} ({b})" for c, b in
                             sorted(odd.items())[:15])
                 + (" ..." if len(odd) > 15 else ""))

    log.step(4, "reference data")
    #  One candidate list per BloombergCode, the first crosscode row's.
    ref = {}
    for r in rows:
        if r.bbg not in ref:
            ref[r.bbg] = refdata.ref_candidates(
                r, markets, universe.resolve_sym(r, master, markets)[0])
    wanted = sorted({c for cs in ref.values() for c in cs})

    if (stage / EQUITY).exists() and (stage / PX).exists():
        eq_pick = {r["BloombergCode"]: r["sym"]
                   for r in read_csv(stage / EQUITY)}
        px = {r["sym"]: r["PX_LAST"] for r in read_csv(stage / PX)}
        log.kv("equity rows", f"{len(eq_pick):,} codes",
               "read back from equity.csv and px.csv")
    else:
        equity = {}
        for i in range(0, len(wanted), chunk):
            equity.update(refdata.fetch_equity(em, master_date,
                                               wanted[i:i + chunk]))
        if not equity:
            raise ExtractError(
                f"equity_master answered no row for any of "
                f"{logs.thousands(len(wanted))} syms on {master_date}")
        eq_pick = {b: p for b, p in
                   ((b, refdata.pick_ref(None, cs, equity))
                    for b, cs in ref.items()) if p}
        #  EVERY fetched sym's PX_LAST, not only the picked ones: a sym's
        #  own row prices its fallback even when a code picked another.
        #  Staged before equity.csv, and read back only with it.
        px = {p: row["PX_LAST"] for p, row in equity.items()}
        write_csv(stage / PX, ["sym", "PX_LAST"], sorted(px.items()))
        write_csv(stage / EQUITY, EQUITY_COLUMNS, (
            [b, p] + [equity[p][f] for f in refdata.EQUITY_FIELDS]
            for b, p in sorted(eq_pick.items())))
        log.kv("equity rows", f"{len(eq_pick):,} of {len(ref):,} codes")
        del equity

    if "luld" not in uses:
        log.kv("ladders", "not needed", "only luld reads them")
    elif (stage / LADDERS).exists():
        log.kv("ladders", "already staged", "ladders.csv kept")
    else:
        ladders = {}
        for i in range(0, len(wanted), chunk):
            ladders.update(refdata.fetch_ladders(em, wanted[i:i + chunk]))
        lad_pick = {b: p for b, p in
                    ((b, refdata.pick_ref(None, cs, ladders))
                     for b, cs in ref.items()) if p}
        if lad_pick:
            log.kv("ladders", f"{len(lad_pick):,} of {len(ref):,} codes")
        else:
            log.warn(f"no tick ladder for any of {len(ref):,} codes "
                     f"({len(wanted):,} syms asked); ladders.csv is empty")
        write_csv(stage / LADDERS, LADDER_COLUMNS, (
            [b, p, price, tick] for b, p in sorted(lad_pick.items())
            for price, tick in ladders[p]))
        del ladders

    def px_of(sym):
        """PX_LAST of the sym itself, else of the row its codes found."""
        for s in [sym] + [eq_pick.get(r.bbg) for r in groups.get(sym, [])]:
            if s and s in px:
                return px_or_none(px[s])
        return None

    settled = {}

    def settle(sym, prints, ltp=None):
        """(Close, why): `why` is the fallback reason even for a sym that
        ends with no close, so its log line can say it.  `ltp` are the fast
        path's rows at or before the cutoff, when the market has one."""
        cut = cut_of.get(sym)
        c = closes.resolve(sym, prints, codes.get(sym, []), px_of(sym),
                           cut, ltp)
        why = c.reason
        if c.reason == "no-close":
            why = closes.resolve(sym, prints, codes.get(sym, []), "?",
                                 cut, ltp).reason
        settled[sym] = (c, why)

    def reconnect():
        conns["qatt"] = conns["reconnect"]()
        return conns["qatt"]

    n = int(cfg["SYM_CHUNK"])
    size = {"n": n}                 # the halved read size, for every market
    todo = [m for m in sorted(by_market) if not only or m in only]
    for c in only or []:
        if c not in by_market:
            log.info(f"--market {c}: no sym of the universe is in it")
    #  Without ticks, the closes come from last_q - when qatt has the
    #  columns the condensed read needs; else from the full read, unwritten.
    fast = qattsource.condensed(cols)
    for step, mkt in enumerate(todo, 1):
        msyms = by_market[mkt]
        log.step(f"5.{step}",
                 f"market {mkt}, {logs.thousands(len(msyms))} syms")
        files = market_files(stage, mkt)
        need = files if ticks else files[1:2]
        old = [f.with_name(f.name + ".old") for f in need]
        if only:
            #  REDO IT, as far as this run's uses need.  Set its files
            #  aside, ticks first, so a crash from here leaves the market
            #  "not done"; renamed, not deleted, because a network share
            #  keeps a deleted name busy.
            for f, o in zip(need, old):
                if f.exists():
                    os.replace(f, o)
        elif done_for(files, uses):
            staged = read_csv(files[1])
            if {r["sym"] for r in staged} == set(msyms):
                log.info(f"{mkt} done already, skipped")
                rp.market(mkt, staged, note="already staged")
                continue
            log.warn(f"{mkt} staged for other syms than this run's; "
                     f"redoing it")
        settled = {}
        parts = [_part(f) for f in files]
        quote_rows, stats = [], {"prints": 0}
        t0 = time.monotonic()
        try:
            try:
                if ticks:
                    with parts[0].open("w", encoding="utf-8",
                                       newline="") as fh:
                        stats = write_ticks(fh, fetch_chunks(
                            conns["qatt"], qday, msyms, cols, size,
                            reconnect, log), settle)
                elif fast and mkt in cut_kdb:
                    #  The closes, then the last trades before the cut:
                    #  two short reads, a sym's rows kept between them.
                    day_rows, ltp = {}, {}
                    for by_sym in fetch_chunks(
                            conns["qatt"], qday, msyms, cols, size,
                            reconnect, log, last=True):
                        day_rows.update((k, v) for k, v in by_sym.items()
                                        if v)
                    for by_sym in fetch_chunks(
                            conns["qatt"], qday, sorted(day_rows), cols,
                            size, reconnect, log, last=True,
                            cut=cut_kdb[mkt]):
                        ltp.update(by_sym)
                    for sym, rows in day_rows.items():
                        settle(sym, rows, ltp.get(sym, []))
                else:
                    for by_sym in fetch_chunks(
                            conns["qatt"], qday, msyms, cols, size,
                            reconnect, log, last=fast):
                        for sym, rows in by_sym.items():
                            if rows:
                                settle(sym, rows)
            except Exception as e:                          # noqa: BLE001
                raise ExtractError(f"market {mkt}, qatt read on "
                                   f"{names['qatt']}: "
                                   f"{type(e).__name__}: {e}") from e
            ticked = set(settled)

            quiet = [s for s in msyms if s not in ticked]
            quotes = {}
            for k in range(0, len(quiet) if ticks else 0, n):
                try:
                    quotes.update(read_quotes(conns, qday, quiet[k:k + n],
                                              log))
                except Exception as e:                      # noqa: BLE001
                    raise ExtractError(f"market {mkt}, quote read on "
                                       f"{names['quote']}: "
                                       f"{type(e).__name__}: {e}") from e
            log.kv("no print", logs.thousands(len(quiet)),
                   f"{logs.thousands(len(quotes))} with a quote" if ticks
                   else "no quote read: no ticks this run")
            if not any(codes[s] for s in msyms):
                log.warn(f"{mkt} has no close codes in close_conditions.csv: "
                         f"a name that traded closes at its last trade")
            for s in quiet:
                settle(s, [])
                if s not in quotes:
                    continue
                if not codes[s]:
                    log.warn(f"quote  {s}  {','.join(exts_of(groups[s]))}  "
                             f"no close codes for its market, quote-only row "
                             f"skipped")
                    continue
                secs, bid, ask = quotes[s]
                quote_rows.append([s, "" if secs is None
                                   else qattsource.hms(secs),
                                   bid, ask, codes[s][0]])

            for s in msyms:
                c, why = settled[s]
                exts = ",".join(exts_of(groups[s]))
                if c.source == "equity_master":
                    log.warn(f"close  {s}  {exts}  {c.reason}  -> "
                             f"equity_master {c.close}")
                elif c.source != "qatt":
                    log.warn(f"close  {s}  {exts}  {why}  -> no close, "
                             f"PX_LAST null or <= 0")
            got = [settled[s][0] for s in msyms]
            write_csv(files[1], CLOSE_COLUMNS,
                      ([c.sym, c.close, c.source, c.reason] for c in got))
            if ticks:
                write_csv(files[2], QUOTE_COLUMNS, quote_rows)
                os.replace(parts[0], files[0])      # last: the market is done
            for o in old:
                try:
                    o.unlink()
                except OSError:
                    pass
        except BaseException:
            for p in parts:
                if p.exists():
                    p.unlink()
            raise
        log.kv(f"{mkt} done", f"{logs.thousands(len(msyms))} syms",
               (f"{stats['prints']:,} prints, " if ticks
                else "closes only, ")
               + f"closing print "
               f"{sum(c.source == 'qatt' and not c.reason for c in got):,}, "
               f"last trade "
               f"{sum(c.reason == 'last-trade' for c in got):,}, "
               f"equity_master "
               f"{sum(c.source == 'equity_master' for c in got):,}, "
               f"no close {sum(c.reason == 'no-close' for c in got):,}, "
               f"quote-only {len(quote_rows):,}")
        rp.market(mkt, [{"sym": c.sym, "close": c.close, "source": c.source,
                         "reason": c.reason, "why": settled[c.sym][1]}
                        for c in got],
                  prints=stats["prints"] if ticks else "",
                  quote_only=len(quote_rows) if ticks else "",
                  seconds=round(time.monotonic() - t0, 1))

    #  NO ZIP UNTIL EVERY MARKET IS STAGED: a --market run may leave some.
    missing = [m for m in sorted(by_market)
               if not done_for(market_files(stage, m), uses)]
    rp.missing = missing
    if missing:
        log.info(f"{len(missing)} markets still to do "
                 f"({', '.join(missing)}); no zip yet - run without "
                 f"--market to finish them")
        return None

    #  A WHOLE DAY WITH NO PRINT IS THE WRONG DAY, not a quiet one: the RDB
    #  on a weekend, or after kdb's midnight rollover.  Nova would write a
    #  permanent no-trading day for every name.  The staging is kept.
    if not any_print(stage, sorted(by_market)):
        if source == "rdb" and date:
            hint = (f". The RDB is on {zd or 'an unknown day'}, so it may "
                    f"no longer hold {day}: run --date {day} without --rdb "
                    f"to read the HDB")
        elif source == "rdb":
            hint = (f". After midnight the RDB holds the new day: use "
                    f"--date {day}")
        else:
            hint = ""
        raise ExtractError(f"qatt returned no prints at all for {day} on "
                           f"{names['qatt']}; no zip written{hint}")

    log.step(6, "the zip")
    out = Path(cfg["EXPORT_DIR"]) / bundle_name(day, uses)
    manifest, table = assemble(stage, out, sorted(by_market), {
        "date": day, "source": source, "for": "|".join(uses),
        "equity_master date": master_date,
        "time column": qattsource.TIME_FIELD,
        "kdb timezone": cfg["KDB_TIMEZONE"], "syms asked": len(syms)},
        uses)
    log_summary(table, log)
    log.info()
    log.kv("syms with prints",
           f"{logs.thousands(manifest['syms with prints'])} of "
           f"{logs.thousands(len(syms))}")
    log.kv("prints", logs.thousands(manifest["prints"]) if ticks
           else "not read: no ticks in this zip")
    log.kv("closes", f"closing print {manifest['closes from qatt']:,}, "
                     f"last trade {manifest['closes from last trade']:,}, "
                     f"equity_master "
                     f"{manifest['closes from equity_master']:,}, "
                     f"none {manifest['no close']:,}")
    if ticks:
        log.kv("quote only", logs.thousands(manifest["quote only"]))
    log.ok(f"written  {out.stat().st_size / 1e6:,.1f} MB  {out}")
    rp.zip, rp.zip_size = str(out), out.stat().st_size
    rp.members = zip_members(uses)
    rp.manifest = {k: str(v) for k, v in manifest.items()}
    return out


def main(argv=None, today=None) -> int:
    p = argparse.ArgumentParser(
        description="The day's qatt prints, closes and reference data, in "
                    "one zip for Nova.")
    p.add_argument("--date", default="",
                   help="YYYY-MM-DD: that day from the HDB, instead of "
                        "today from the RDB")
    p.add_argument("--rdb", action="store_true",
                   help="with --date: read that day from the RDB instead "
                        "of the HDB (e.g. just after midnight, while the "
                        "RDB still holds it); alone, the same as no "
                        "argument")
    p.add_argument("--market", default="",
                   help="only these markets this run: Bloomberg exchange "
                        "codes or FidessaMarket names, joined by | (e.g. "
                        "\"NZ|HKG-MAIN\"), any case; each is read again, "
                        "and the zip is written once every market is "
                        "staged")
    p.add_argument("--for", dest="uses", default="",
                   help="what the zip is for: luld, td, ticks, joined by | "
                        "(e.g. \"luld|td\"), any case; all three by "
                        "default.  Without ticks no print is read")
    p.add_argument("--log", default="", help="tee the log to this file")
    p.add_argument("--fresh", action="store_true",
                   help="delete the day's staging folder and start over")
    p.add_argument("--no-mail", action="store_true",
                   help="send no mail at the end of this run")
    p.add_argument("--self-test", action="store_true",
                   help="checks, with no kdb")
    a = p.parse_args(argv)
    if a.self_test:
        return self_test()

    try:
        date = dt.date.fromisoformat(a.date) if a.date else None
    except ValueError:
        print(f"FAIL  --date {a.date!r} is not YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        uses = parse_uses(a.uses)
    except ValueError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2
    q_name, source = qatt_server(date, a.rdb)
    u_name = quote_server(date, a.rdb)
    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "EXPORT_DIR")
        em_host, em_port = settings.server(cfg, "EQUITY_MASTER_SERVER")
        q_host, q_port = settings.server(cfg, q_name)
        #  Blank: the quote table is on qatt's own process.
        own_quote = bool(str(cfg.get(u_name, "")).strip())
        if own_quote:
            u_host, u_port = settings.server(cfg, u_name)
    except settings.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    names = {"qatt": f"{q_name} ({q_host}:{q_port})"}
    names["quote"] = (f"{u_name} ({u_host}:{u_port})" if own_quote
                      else names["qatt"])
    log = logs.Log(path=a.log or None)
    today = today or dt.date.today()
    rp = runreport.RunReport(uses, today)
    rp.params = {"--date": a.date or "(none: today)", "--rdb": a.rdb,
                 "--for": "|".join(uses), "--market": a.market or "all",
                 "--fresh": a.fresh}
    log.on_emit = rp.capture
    out = None
    rc = 1
    try:
        name = weekend(date, today)
        if name:
            log.fail(f"today, {today}, is a {name}: the RDB holds no trading "
                     f"day. Use --date YYYY-MM-DD for the day you want")
            return 1
        log.kv("qatt", source, names["qatt"])
        log.kv("quote", source, names["quote"])
        try:
            conns = {"em": qattsource.connect(em_host, em_port),
                     "qatt": qattsource.connect(q_host, q_port),
                     "reconnect": lambda: connect_again(q_host, q_port, log),
                     "names": names}
            if own_quote:
                conns["quote"] = qattsource.connect(u_host, u_port)
                conns["quote_reconnect"] = lambda: connect_again(
                    u_host, u_port, log)
            out = build(cfg, date, conns, log, today=today, fresh=a.fresh,
                        rdb=a.rdb, only=parse_markets(a.market) or None,
                        uses=uses, report=rp)
        except ExtractError as e:
            log.fail(str(e))
            log.fail("no zip written")
            return 1
        except SystemExit as e:             # qattsource's explained failures
            log.fail(str(e))
            log.fail("no zip written")
            return 1
        except Exception as e:                              # noqa: BLE001
            log.fail(f"{type(e).__name__}: {e}")
            log.fail("no zip written")
            return 1
        rc = 0
        return 0
    finally:
        finish(rp, cfg, log, out, rc, no_mail=a.no_mail)
        log.close()


def finish(rp, cfg, log, out, rc, no_mail=False) -> None:
    """The end of every run once the log is open: write the HTML report and
    send the mail - OK with a zip, PARTIAL without one, FAILED on an XX.
    Nothing here changes the run's exit code: a report or a mail that
    fails is a !! line."""
    rp.ended = dt.datetime.now()
    rp.status = ("FAILED" if rc or rp.first_fail() else
                 "OK" if out else "PARTIAL")
    page = None
    try:
        folder = (Path(out).parent if out
                  else rp.stage if rp.stage and Path(rp.stage).is_dir()
                  else Path(cfg["EXPORT_DIR"]))
        page = rp.write_html(folder)
        log.kv("report", str(page))
    except Exception as e:                                  # noqa: BLE001
        log.warn(f"report not written: {type(e).__name__}: {e}")
    to = cfg.get("EMAIL_TO") or []
    if isinstance(to, str):
        to = [t.strip() for t in re.split(r"[,;]", to) if t.strip()]
    host = str(cfg.get("SMTP_HOST") or "").strip()
    if no_mail:
        log.info("no mail: --no-mail")
    elif not host or not to:
        log.info("no mail: SMTP_HOST / EMAIL_TO not set")
    else:
        try:
            mailer.send(rp.subject(), rp.body(), host,
                        cfg.get("EMAIL_FROM") or "", to,
                        [page] if page else [])
            log.info(f"mail sent to {', '.join(to)}: {rp.subject()}")
        except Exception as e:                              # noqa: BLE001
            log.warn(f"mail not sent: {e}")


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def attempt(fn):
        """fn()'s exception, or None."""
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            return e
        return None

    print("extract --self-test\n\nwhich day, which server")
    D = dt.date
    parts = [D(2026, 9, 18), D(2026, 9, 21), D(2026, 9, 22)]
    check("the newest partition before today",
          pick_day(parts, D(2026, 9, 23)), D(2026, 9, 22))
    check("--date takes the newest on or before it",
          pick_day(parts, D(2026, 9, 23), D(2026, 9, 20)), D(2026, 9, 18))
    check("nothing that early is None",
          pick_day(parts, D(2026, 9, 23), D(2026, 9, 1)), None)
    check("no --date reads the RDB",
          qatt_server(None),
          ("QATT_RDB_SERVER", "rdb"))
    check("--date reads the HDB",
          qatt_server(D(2026, 9, 25)), ("QATT_SERVER", "hdb"))

    check("a market code is a safe file name",
          [safe_code(c) for c in ("JT", ".", "", "A/B", "u-1_x")],
          ["JT", "_", "NONE", "A_B", "u-1_x"])

    print("\nthe universe")
    R = crosscode.Row
    rows = [R("", "", "7203 JT", "7203", "JT", "", "", "TYO-MAIN", ""),
            R("", "", "7203 JE", "7203", "JE", "", "", "JNX-MAIN", ""),
            R("", "", "ZZZ XX", "ZZZ", "XX", "", "", "NOWHERE", "")]
    markets = {"TYO-MAIN": marketcfg.Market("TYO-MAIN", "JP",
                                            "Tokyo Standard Time")}
    check("the third candidate shape is ticker dot composite",
          candidates(rows, markets)["7203 JT"][2], "7203.JP")
    check("three venue rows ask for one sym, and an unresolvable one none",
          wanted_syms(rows, {"7203 JE": {"sym": "7203.JP"}}, markets),
          ["7203.JP"])

    def typed(bbg, sec_type):
        ticker, ext = crosscode.split_bbg(bbg)
        return R("", "", bbg, ticker, ext, sec_type, "", "", "")

    ours = {"JT": ["e"], "HK": ["CA"]}
    kept, dropped = universe_rows(
        [typed("7203 JT", "Equity"), typed("1321 JT", "ETF"),
         typed("7203W JT", "Warrant"), typed("AAA XX", "Equity"),
         typed("BBB XX", "Equity"), typed("5 HK", ""),
         typed("6 HK", "equity")], ours)
    check("every Type of an exchange code of ours is kept - Equity, ETF, "
          "a Warrant, a blank Type",
          [r.bbg for r in kept],
          ["7203 JT", "1321 JT", "7203W JT", "5 HK", "6 HK"])
    check("an exchange code not in close_conditions.csv is dropped, "
          "counted per code, and that is the only reason",
          {k: dict(v) for k, v in dropped.items()},
          {"exchange code not ours": {"XX": 2}})

    print("\nreading qatt")
    asked = []

    def fake(query, *args):
        asked.append((query, len(args[-1])))
        if len(args[-1]) > 2:
            raise ConnectionResetError("dropped")
        return [{"sym": s, qattsource.TIME_FIELD: dt.time(9, 0, 1),
                 "price": 1.5, "size": 100, "cond": "", "ex": "T"}
                for s in args[-1]]

    quiet = logs.Log(stamps=False, quiet=True)
    got = list(fetch_chunks(fake, D(2026, 9, 22), ["A", "B", "C", "D", "E"],
                            ["sym"], 4, lambda: fake, quiet))
    check("a dropped read is halved, and the smaller size kept",
          [n for _q, n in asked], [4, 2, 2, 1])
    check("every sym comes back once, shaped",
          [s for c in got for s in c], ["A", "B", "C", "D", "E"])
    check("a row is (seconds, price, size, cond, ex)",
          got[0]["A"], [(32401, "1.5", "100", "#N/A N.A.", "T")])
    check("the HDB is asked with the date",
          asked[0][0], qattsource.ticks_q(None, ["sym"]))
    asked.clear()
    list(fetch_chunks(fake, None, ["A"], ["sym"], 4, lambda: fake, quiet))
    check("no date asks the RDB, with no date in the query",
          asked[0][0], qattsource.live_ticks_q(None, ["sym"]))
    asked.clear()
    size = {"n": 4}
    list(fetch_chunks(fake, None, ["A", "B", "C"], ["sym"], size,
                      lambda: fake, quiet))
    list(fetch_chunks(fake, None, ["D", "E"], ["sym"], size,
                      lambda: fake, quiet))
    check("a shared size keeps the halving for the next call",
          ([n for _q, n in asked], size), ([3, 1, 1, 1, 1, 1], {"n": 1}))

    print("\nan end-to-end build, on fake kdb")
    em_date = D(2026, 9, 24)
    MASTER_TABLE = [
        # sym_bpipe, sym_mbpipe, sym, prim, comp, mic
        ("7203.JT", "7203 JT EQUITY", "7203.JP", "JT", "JP", "XTKS"),
        ("7203.JE", "7203 JE EQUITY", "7203.JP", "JT", "JP", "XTKS"),
        ("AIA.NZ", "AIA NZ EQUITY", "AIA.NZ", "NZ", "NZ", "XNZE"),
        ("8888.HK", "8888 HK EQUITY", "8888.HK", "HK", "HK", "XHKG"),
        ("8889.HK", "8889 HK EQUITY", "8889.HK", "HK", "HK", "XHKG"),
        ("ZZZ.XX", "ZZZ XX EQUITY", "ZZZ.XX", "XX", "XX", "XXXX"),
        ("QQQ.XX", "QQQ XX EQUITY", "QQQ.XX", "XX", "XX", "XXXX")]
    MASTER_ROWS = [dict(zip(("sym_bpipe", "sym_mbpipe")
                            + qattsource.MASTER_FIELDS, r))
                   for r in MASTER_TABLE]

    def equity_row(sym, px):
        row = {f: "" for f in refdata.EQUITY_FIELDS}
        row.update(sym=sym, PX_LAST=px, LONG_COMP_NAME=f"{sym} LTD")
        return row

    EQUITY_ROWS = [equity_row("7203.JP", 2871), equity_row("AIA.NZ", 6.13),
                   equity_row("8888.HK", 3.4), equity_row("8889.HK", None),
                   equity_row("ZZZ.XX", 1.4), equity_row("QQQ.XX", 0.0)]

    class FakeEm:
        def __init__(self, equity=EQUITY_ROWS, client_fails=False,
                     date=em_date):
            self.equity, self.asked = equity, []
            self.client_fails, self.days_back = client_fails, None
            self.date = date

        def __call__(self, q, *args):
            self.asked.append(q)
            if q == qattsource.MAXDATE_CLIENT_Q:
                if self.client_fails:
                    raise RuntimeError("'type")
                return self.date
            if q == qattsource.MAXDATE_SERVER_Q:
                self.days_back = args[0]
                return self.date
            s = set(args[-1]) if args else set()
            for query, col in ((qattsource.MASTER_BPIPE_Q, "sym_bpipe"),
                               (qattsource.MASTER_MBPIPE_Q, "sym_mbpipe"),
                               (qattsource.MASTER_SYM_Q, "sym")):
                if q == query:
                    return [r for r in MASTER_ROWS if r[col] in s]
            if q == refdata.EQUITY_Q:
                return [r for r in self.equity if r["sym"] in s]
            if q == refdata.IDS_Q:
                return [{"sym": "7203.JP", "id": "1"}] if "7203.JP" in s \
                    else []
            if q == refdata.TBL_Q:
                return [{"id": 1, "price": 3000, "ticksize": 0.5},
                        {"id": 1, "price": 1000, "ticksize": 0.1}]
            raise AssertionError(f"em was asked {q}")

    T = qattsource.TIME_FIELD
    TICK_ROWS = {
        "7203.JP": [("08:00:00", 2850, 412300, "O"),
                    ("10:30:00", 2858, 98000, "e"),
                    ("14:30:00", 2876, 1203400, "e"),
                    ("14:31:00", 2877, 100, "")],
        "AIA.NZ": [("06:00:00", 6.12, 15000, ""),
                   ("12:44:58", 6.14, 800, "XT")],
        "ZZZ.XX": [("09:00:00", 1.5, 10, "")]}

    def sec(t):
        return None if t is None else t.hour * 3600 + t.minute * 60 + t.second

    def at(s):
        """A second as kdb sends it now: "i"$ - an int, 0Ni for null."""
        return -2147483648 if s is None else s

    def order(key):
        """kdb's sort of a `by` key: a null second first."""
        sym, s = key[0], key[1]
        return (sym, s is not None, s or 0) + tuple(key[2:])

    def fake_condense(rows):
        """What kdb answers the condensed ticks query with."""
        summed = {}
        for r in rows:
            k = (r["sym"], sec(r[T]), r["price"], r["cond"], r["ex"])
            summed[k] = summed.get(k, 0) + r["size"]
        return [{"sym": k[0], T: at(k[1]), "price": k[2], "cond": k[3],
                 "ex": k[4], "size": v}
                for k, v in sorted(summed.items(), key=lambda kv:
                                   order(kv[0]))]

    def fake_last(rows, cut=None):
        """What kdb answers qattsource.last_q with: per sym and cond, the
        highest price in that cond's latest second (a null second only
        when the cond has no timed print).  With a cut, only the seconds at
        or before it - and a null second, which q sorts below every
        second."""
        best = {}
        for r in rows:
            if cut is not None and sec(r[T]) is not None \
                    and sec(r[T]) > cut:
                continue
            k = (r["sym"], r["cond"], sec(r[T]))
            best[k] = max(best.get(k, r["price"]), r["price"])
        out = []
        for sym, cond in sorted({(k[0], k[1]) for k in best}):
            secs = [k[2] for k in best if k[:2] == (sym, cond)]
            timed = [x for x in secs if x is not None]
            keep = max(timed) if timed else None
            out.append({"sym": sym, "cond": cond, T: at(keep),
                        "price": best[(sym, cond, keep)]})
        return out

    class FakeQatt:
        def __init__(self, fail=False, fail_on=(), parts=None,
                     max_syms=None, tables=("qatt",), empty=False,
                     zd=D(2026, 9, 28), silent=(), ticks=None):
            self.asked, self.fail, self.fail_on = [], fail, set(fail_on)
            self.ticks = TICK_ROWS if ticks is None else ticks
            self.reads = []             # ("fast" | "ticks", syms) per read
            self.zd = zd                # the RDB's .z.D; None cannot say
            self.empty = empty          # no print at all, as a wrong day
            self.silent = set(silent)   # these syms have no print
            self.tables = list(tables)
            self.syms, self.sizes, self.max_syms = [], [], max_syms
            self.parts = parts or [D(2026, 9, 24), D(2026, 9, 25)]

        def __call__(self, q, *args):
            self.asked.append(q)
            if q == TABLES_Q:
                return self.tables
            if q == ZD_Q:
                if self.zd is None:
                    raise RuntimeError("'.z.D")
                return self.zd
            if q == qattsource.PARTITIONS_Q:
                return self.parts
            if q == qattsource.COLUMNS_Q:
                return ["sym", "time", T, "price", "size", "cond", "ex"]
            if "from qatt" in q:
                cutq = "<=" in q            # last_q with a cut: [..;s;c]
                syms = list(args[-2] if cutq else args[-1])
                self.syms += syms
                self.sizes.append(len(syms))
                if self.max_syms and len(syms) > self.max_syms:
                    raise ConnectionResetError("dropped")
                if self.fail or self.fail_on & set(syms):
                    raise RuntimeError("'type")
                rows = [{"sym": s, T: None if t is None
                         else dt.time.fromisoformat(t), "price": p,
                         "size": n, "cond": c, "ex": "X"}
                        for s in syms for t, p, n, c in
                        ([] if self.empty or s in self.silent
                         else self.ticks.get(s, []))]
                fast = qattsource.LAST_MARK in q
                self.reads.append(("cut" if cutq else "fast" if fast
                                   else "ticks", syms))
                if fast:
                    return fake_last(rows, args[-1] if cutq else None)
                if "size:sum size by" in q:
                    return fake_condense(rows)
                return rows
            raise AssertionError(f"qatt was asked {q}")

    class FakeQuote:
        def __init__(self, tables=("quote",), fail=False):
            self.asked, self.tables, self.fail = [], list(tables), fail

        def __call__(self, q, *args):
            if q == TABLES_Q:
                return self.tables
            self.asked.append((q, args))
            if self.fail:
                raise RuntimeError("'quote")
            have = {"8888.HK": dt.time(16, 8, 2), "QQQ.XX": dt.time(9, 0)}
            return [{"sym": s, "time": have[s], "bid": 3.41, "ask": 3.43}
                    for s in args[-1] if s in have]

    class Caught(logs.Log):
        def __init__(self):
            super().__init__(stamps=False, quiet=True)
            self.lines = []

        def emit(self, level, text=""):
            self.lines.append(self.line(level, text))

    #  JE and XX are ours here, with no close code - a blank
    #  CloseCondCodes cell - so the universe filter keeps them.
    conditions = {"JT": ["e", "ES"], "NZ": ["CA"], "HK": ["CA"],
                  "JE": [], "XX": []}

    CROSSCODE = ("BloombergCode,FidessaMarket,Type\n"
                 "7203 JT,TYO-MAIN,Equity\n7203 JE,JNX-MAIN,Equity\n"
                 "AIA NZ,NZE-MAIN,Equity\n8888 HK,HKG-MAIN,Equity\n"
                 "8889 HK,HKG-MAIN,ETF\nZZZ XX,XXX-MAIN,Equity\n"
                 "QQQ XX,XXX-MAIN,Equity\nBSKT HK,HKG-MAIN,Basket\n"
                 "12345 HK,HKG-MAIN,Warrant\nAAA US,NYS-MAIN,Equity\n")

    def run(tmp, date, em=None, qatt=None, quote=None, log=None,
            fresh=False, cc_text=CROSSCODE, extra=None, conds=None,
            reconnect=None, rdb=False, only=None, uses=None,
            cutoffs=None, mk=None):
        cc = Path(tmp) / "CrossCode.csv"
        cc.write_text(cc_text, encoding="utf-8")
        cfg = dict(settings.DEFAULTS, CROSSCODE_PATH=str(cc),
                   EXPORT_DIR=str(Path(tmp) / "out"), **(extra or {}))
        conns = {"em": em or FakeEm(), "qatt": qatt or FakeQatt(),
                 "quote": quote or FakeQuote(),
                 "reconnect": reconnect or (lambda: FakeQatt())}
        return build(cfg, date, conns, log or Caught(), today=D(2026, 9, 28),
                     markets=markets if mk is None else mk,
                     conditions=conditions if conds is None else conds,
                     fresh=fresh, rdb=rdb, only=only, uses=uses,
                     cutoffs=cutoffs)

    def members(path):
        with zipfile.ZipFile(path) as z:
            return {n: z.read(n).decode("utf-8").splitlines()
                    for n in z.namelist()}

    with tempfile.TemporaryDirectory() as tmp:
        qatt, quote, log = FakeQatt(), FakeQuote(), Caught()
        err, out = None, None
        try:
            out = run(tmp, D(2026, 9, 25), qatt=qatt, quote=quote, log=log)
        except Exception as e:                              # noqa: BLE001
            err = e
        check("an HDB build runs", repr(err), "None")
        z = members(out) if out else {m: [""] for m in MEMBERS}
        check("the name is the day", out and out.name,
              "phase1-20260925.zip")
        check("the zip, and the staging folder left beside it",
              out and sorted(p.name for p in out.parent.iterdir()),
              ["phase1-20260925", "phase1-20260925.zip"])
        check("no .part is left behind",
              out and sorted(p.name for p in out.parent.rglob("*.part")), [])
        check("the staging folder holds each market's three files",
              out and sorted(p.name for p in
                             (out.parent / "phase1-20260925").iterdir()),
              sorted([EQUITY, LADDERS, MASTER, PX, STAGE] +
                     [f"{n}-{m}.csv" for n in ("closes", "quote_only",
                                               "ticks")
                      for m in ("HK", "JT", "NZ", "XX")]))
        check("seven members", sorted(z), sorted(MEMBERS))
        check("every member's header",
              [z.get(m, [""])[0] for m in MEMBERS],
              ["key,value", ",".join(MASTER_COLUMNS),
               ",".join(TICK_COLUMNS), ",".join(CLOSE_COLUMNS),
               ",".join(QUOTE_COLUMNS), ",".join(EQUITY_COLUMNS),
               ",".join(LADDER_COLUMNS)])
        check("ticks: each sym's prints together, kdb's clock",
              z.get(TICKS, [])[1:4],
              ["7203.JP,08:00:00,2850,412300,O,X",
               "7203.JP,10:30:00,2858,98000,e,X",
               "7203.JP,14:30:00,2876,1203400,e,X"])
        check("closes: one row per sym asked, every kind of answer, "
              "market by market",
              z.get(CLOSES),
              ["sym,close,source,reason",
               "8888.HK,3.4,equity_master,no-trades",
               "8889.HK,,,no-close",
               "7203.JP,2876,qatt,",
               "AIA.NZ,6.14,qatt,last-trade",
               "QQQ.XX,,,no-close",
               "ZZZ.XX,1.5,qatt,last-trade"])
        check("a no-trades sym with a quote is quote-only, cond its first "
              "close code; one with no close codes is skipped",
              z.get(QUOTE_ONLY),
              ["sym,time,bid,ask,cond", "8888.HK,16:08:02,3.41,3.43,CA"])
        check("the quote is asked for the syms with no print, on the HDB",
              (sorted({q for q, _a in quote.asked}),
               sorted({a[0] for _q, a in quote.asked}),
               sorted(s for _q, a in quote.asked for s in a[1])),
              ([refdata.QUOTE_HDB_Q], [D(2026, 9, 25)],
               ["8888.HK", "8889.HK", "QQQ.XX"]))
        check("master: one row per code equity_master answered",
              z.get(MASTER, [None, None])[1],
              "7203 JE,7203.JP,JT,JP,XTKS")
        check("equity: keyed by BloombergCode, the sym it was found under",
              [r.split(",")[:3] for r in z.get(EQUITY, [])[1:3]],
              [["7203 JE", "7203.JP", "2871"], ["7203 JT", "7203.JP", "2871"]])
        check("equity: a null PX_LAST is blank",
              [r.split(",")[2] for r in z.get(EQUITY, [])
               if r.startswith("8889 HK,")], [""])
        check("ladders: raw, per code, price ascending",
              z.get(LADDERS, [])[1:],
              ["7203 JE,7203.JP,1000,0.1", "7203 JE,7203.JP,3000,0.5",
               "7203 JT,7203.JP,1000,0.1", "7203 JT,7203.JP,3000,0.5"])
        manifest = dict(r.split(",", 1) for r in z.get(MANIFEST, [])[1:])
        check("manifest keys, in order",
              list(manifest),
              ["date", "source", "for", "equity_master date", "time column",
               "kdb timezone", "syms asked", "prints", "syms with prints",
               "quote only", "closes from qatt", "closes from last trade",
               "closes from equity_master", "no close", "exported at"])
        check("manifest values",
              [manifest.get(k) for k in
               ("date", "source", "for", "equity_master date", "time column",
                "syms asked", "prints", "syms with prints", "quote only",
                "closes from qatt", "closes from last trade",
                "closes from equity_master", "no close")],
              ["2026-09-25", "hdb", "luld|td|ticks", "2026-09-24", T, "6",
               "7", "3", "1",
               "1", "2", "1", "2"])
        check("the universe: the kept rows, and each drop with its reason",
              [ln for ln in log.lines if ln.startswith("..  universe")
               or ln.startswith("..  dropped")],
              ["..  universe                8 rows   of 9, the exchange codes "
               "of close_conditions.csv",
               "..  dropped                 1 rows   exchange code not ours: "
               "US 1"])
        check("one !! line per equity_master close and per no-close; none "
              "for a last trade",
              [ln for ln in log.lines if ln.startswith("!!  close")],
              ["!!  close  8888.HK  HK  no-trades  -> equity_master 3.4",
               "!!  close  8889.HK  HK  no-trades  -> no close, PX_LAST "
               "null or <= 0",
               "!!  close  QQQ.XX  XX  no-trades  -> no close, PX_LAST "
               "null or <= 0"])
        check("last-trade closes are a count on the market's done line",
              [ln.split("   ")[-1] for ln in log.lines
               if ln.startswith("..  NZ done")],
              ["2 prints, closing print 0, last trade 1, equity_master 0, "
               "no close 0, quote-only 0"])
        check("a market with no close codes is one !! line",
              [ln for ln in log.lines if "no close codes in" in ln],
              ["!!  XX has no close codes in close_conditions.csv: a name "
               "that traded closes at its last trade"])
        check("a quote with no close code to label it is a !! line",
              [ln for ln in log.lines if ln.startswith("!!  quote")],
              ["!!  quote  QQQ.XX  XX  no close codes for its market, "
               "quote-only row skipped"])
        top = next((i for i, ln in enumerate(log.lines)
                    if ln.startswith("..  market ")), len(log.lines))
        table = log.lines[top:top + 3]
        check("a summary row per exchange code: syms, with ticks, qatt, "
              "fallbacks by reason, no-close, quote-only",
              [ln.split()[1:] for ln in table],
              [["market", "syms", "ticks", "qatt", "last-trade",
                "no-trades", "none-before-cutoff", "no-close", "quote-only"],
               ["HK", "2", "0", "0", "0", "1", "0", "1", "1"],
               ["JT", "1", "1", "1", "0", "0", "0", "0", "0"]])

    print("\nthe RDB, when there is no --date")
    with tempfile.TemporaryDirectory() as tmp:
        qatt, quote = FakeQatt(), FakeQuote()
        out = None
        err = None
        try:
            out = run(tmp, None, qatt=qatt, quote=quote)
        except Exception as e:                              # noqa: BLE001
            err = e
        check("an RDB build runs", repr(err), "None")
        check("the day is today", out and out.name, "phase1-20260928.zip")
        check("no partition list is asked of the RDB",
              qattsource.PARTITIONS_Q in qatt.asked, False)
        check("the ticks are read with the RDB's undated query",
              [q for q in qatt.asked if "from qatt" in q][:1],
              [qattsource.live_ticks_q(None, ["sym", T, "price", "size",
                                              "cond", "ex"])])
        check("and the quote with the RDB's",
              quote.asked and quote.asked[0][0], refdata.QUOTE_RDB_Q)
        manifest = dict(r.split(",", 1) for r in
                        (members(out)[MANIFEST][1:] if out else []))
        check("the manifest says rdb", manifest.get("source"), "rdb")

    print("\nfailing loudly")
    with tempfile.TemporaryDirectory() as tmp:
        err = attempt(lambda: run(tmp, D(2026, 9, 25), em=FakeEm(equity=[])))
        check("equity_master answering nothing stops the run",
              type(err).__name__, "ExtractError")
        outdir = Path(tmp) / "out"
        check("and leaves no zip",
              sorted(p.name for p in outdir.glob("*.zip*")), [])
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  qatt=FakeQatt(fail=True)))
        check("a qatt failure mid-stream stops the run, naming the market, "
              "the step and the setting",
              (type(err).__name__, str(err)),
              ("ExtractError", "market HK, qatt read on QATT_SERVER: "
                               "RuntimeError: 'type"))
        check("and leaves no zip and no .part",
              sorted(p.name for p in outdir.glob("*.zip*"))
              + sorted(p.name for p in outdir.rglob("*.part")), [])
        err = attempt(lambda: run(tmp, D(2026, 9, 1)))
        check("a --date before every partition stops the run",
              type(err).__name__, "ExtractError")
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  quote=FakeQuote(fail=True)))
        check("so does a quote failure, named as one",
              str(err), "market HK, quote read on QATT_SERVER: "
                        "RuntimeError: 'quote")

    print("\nno print at all for the whole day")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260928"
        err = attempt(lambda: run(tmp, None, qatt=FakeQatt(empty=True)))
        check("an RDB day with no print at all stops the run: a weekend or "
              "the new day after midnight",
              (type(err).__name__, str(err)),
              ("ExtractError", "qatt returned no prints at all for "
               "2026-09-28 on QATT_RDB_SERVER; no zip written. After "
               "midnight the RDB holds the new day: use --date 2026-09-28"))
        check("and writes no zip",
              sorted(p.name for p in stage.parent.glob("*.zip*")), [])
        check("the staging folder is left as it is",
              sorted(p.name for p in stage.glob("ticks-*.csv")),
              ["ticks-HK.csv", "ticks-JT.csv", "ticks-NZ.csv",
               "ticks-XX.csv"])
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  qatt=FakeQatt(empty=True)))
        check("an HDB day with no print stops too",
              str(err), "qatt returned no prints at all for 2026-09-25 on "
                        "QATT_SERVER; no zip written")

    print("\nthe quote and qatt tables, checked before any market")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260925"
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  quote=FakeQuote(tables=("trade",))))
        check("no quote table on the quote connection stops the run",
              str(err), "the quote table is not on QATT_SERVER; set "
                        "QUOTE_RDB_SERVER / QUOTE_SERVER to the process "
                        "that has it")
        check("before anything is staged", stage.exists(), False)
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  qatt=FakeQatt(tables=("quote",))))
        check("and no qatt table on the qatt connection",
              str(err), "the qatt table is not on QATT_SERVER; set "
                        "QATT_RDB_SERVER / QATT_SERVER to the process "
                        "that has it")

    print("\nmain: the quote server's own connection")
    with tempfile.TemporaryDirectory() as tmp:
        import contextlib
        cc = Path(tmp) / "CrossCode.csv"
        cc.write_text(CROSSCODE, encoding="utf-8")
        fakes = {("em-host", 1): FakeEm(), ("qatt-host", 2): FakeQatt(),
                 ("quote-host", 3): FakeQuote()}
        saved = qattsource.connect, settings.load

        sent = []

        class FakeSMTP:
            """smtplib.SMTP as mailer uses it; `fail` makes sending raise."""
            fail = False

            def __init__(self, host):
                self.host = host

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send_message(self, msg):
                if FakeSMTP.fail:
                    raise OSError("connection refused")
                sent.append(msg)

        def main_with(quote_rdb, argv=(), today=D(2026, 9, 28), hdb=None,
                      mail=None, tag=""):
            cfg = dict(settings.DEFAULTS, CROSSCODE_PATH=str(cc),
                       EXPORT_DIR=str(Path(tmp) / "out"),
                       EQUITY_MASTER_SERVER="em-host:1",
                       QATT_RDB_SERVER="qatt-host:2",
                       QUOTE_RDB_SERVER=quote_rdb,
                       QATT_SERVER="qatt-host:2", QUOTE_SERVER=quote_rdb)
            cfg.update(hdb or {})
            cfg.update(mail or {})
            settings.load = lambda: cfg
            qattsource.connect = lambda h, p: fakes[(h, int(p))]
            smtp = mailer.smtplib.SMTP
            mailer.smtplib.SMTP = FakeSMTP
            logfile = Path(tmp) / (f"run{len(quote_rdb)}-{today}-"
                                   f"{'_'.join(argv)}{tag}.log")
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    rc = main(["--log", str(logfile)] + list(argv),
                              today=today)
            finally:
                qattsource.connect, settings.load = saved
                mailer.smtplib.SMTP = smtp
            return rc, logfile.read_text("utf-8")

        rc, text = main_with("")
        check("QUOTE_RDB_SERVER blank: the qatt RDB is asked, has no quote "
              "table, and the run stops before any market",
              (rc, "XX  the quote table is not on QATT_RDB_SERVER "
                   "(qatt-host:2); set QUOTE_RDB_SERVER / QUOTE_SERVER to "
                   "the process that has it" in text, "--- 5.1" in text),
              (1, True, False))
        rc, text = main_with("quote-host:3")
        check("QUOTE_RDB_SERVER set: the run goes through", rc, 0)
        check("the quotes go to that connection",
              {q for q, _a in fakes[("quote-host", 3)].asked},
              {refdata.QUOTE_RDB_Q})
        check("and never to qatt's",
              [q for q in fakes[("qatt-host", 2)].asked if "quote" in q], [])

        print("\nmain: no argument on a weekend")
        for f in fakes.values():
            f.asked.clear()
        rc, text = main_with("quote-host:3", today=D(2026, 9, 26))
        check("a no-argument run on a Saturday stops, telling the user to "
              "use --date, before asking kdb anything",
              (rc, "XX  today, 2026-09-26, is a Saturday: the RDB holds no "
                   "trading day. Use --date YYYY-MM-DD for the day you "
                   "want" in text,
               [f.asked for f in fakes.values()]),
              (1, True, [[], [], []]))
        rc, text = main_with("quote-host:3", today=D(2026, 9, 27))
        check("and on a Sunday", (rc, "is a Sunday" in text), (1, True))
        rc, text = main_with("quote-host:3", ["--date", "2026-09-25"],
                             today=D(2026, 9, 26))
        check("--date on a Saturday goes through", rc, 0)

        print("\nmain: the mail at the end")
        MAIL = {"SMTP_HOST": "smtp-host", "EMAIL_FROM": "phase1@example.com",
                "EMAIL_TO": ["desk@example.com"]}
        out = Path(tmp) / "out"

        def attached(m):
            return [a.get_filename() for a in m.iter_attachments()]

        sent.clear()
        rc, text = main_with("", mail=MAIL, tag="-m1")
        check("a run that stops with XX still mails: FAILED, with its first "
              "XX line, the report attached",
              (rc, [m["Subject"][:85] for m in sent],
               [attached(m) for m in sent]),
              (1, ["[Phase1] extract 2026-09-28: FAILED - the quote table is "
                   "not on QATT_RDB_SERVER (qatt"],
               [["phase1-20260928-report.html"]]))
        sent.clear()
        rc, text = main_with("quote-host:3", mail=MAIL, tag="-m2")
        check("a good run mails OK, with the zip's name",
              (rc, [m["Subject"] for m in sent]),
              (0, ["[Phase1] extract 2026-09-28: OK - phase1-20260928.zip"]))
        body = sent[0].get_body(("plain",)).get_content() if sent else ""
        check("the body: date, source, closes by kind, markets, the log's "
              "!! and XX", [ln.split()[0] for ln in body.splitlines()[2:11]],
              ["trade", "source", "--for", "--market", "zip", "duration",
               "closes", "markets", "log"])
        page = out / "phase1-20260928-report.html"
        check("the report is written next to the zip",
              page.exists() and "<h2" in page.read_text("utf-8"), True)
        sent.clear()
        rc, text = main_with("quote-host:3", tag="-m3")
        check("no SMTP_HOST / EMAIL_TO: no mail, said so",
              (rc, sent, "..  no mail: SMTP_HOST / EMAIL_TO not set" in text),
              (0, [], True))
        rc, text = main_with("quote-host:3", ["--no-mail"], mail=MAIL)
        check("--no-mail: no mail, said so",
              (rc, sent, "..  no mail: --no-mail" in text), (0, [], True))
        FakeSMTP.fail = True
        rc, text = main_with("quote-host:3", mail=MAIL, tag="-m5")
        FakeSMTP.fail = False
        check("a mail that cannot be sent is a !! line; the exit code is "
              "the run's", (rc, "!!  mail not sent: connection refused"
                            in text), (0, True))
        rc, text = main_with("quote-host:3", ["--market", "NZ", "--fresh"],
                             mail=MAIL)
        check("a --market run that leaves markets to do mails PARTIAL",
              [m["Subject"] for m in sent][-1:],
              ["[Phase1] extract 2026-09-28: PARTIAL - 2 markets still to "
               "do"])
        check("and its report is written in the staging folder",
              (out / "phase1-20260928" / "phase1-20260928-report.html")
              .exists(), True)
        main_with("quote-host:3", tag="-m7")        # finish the day again

        print("\nmain: --date with --rdb")
        fakes[("hdb-host", 4)] = FakeQatt()
        fakes[("hdbq-host", 5)] = FakeQuote()
        for f in fakes.values():
            f.asked.clear()
        hdb = {"QATT_SERVER": "hdb-host:4", "QUOTE_SERVER": "hdbq-host:5"}
        rc, text = main_with("quote-host:3", ["--date", "2026-09-25",
                                              "--rdb"], hdb=hdb)
        out = Path(tmp) / "out"
        check("--date D --rdb goes through", rc, 0)
        check("the zip and the staging folder are named D",
              ((out / "phase1-20260925.zip").exists(),
               read_csv(out / "phase1-20260925" / STAGE)[0]["value"]),
              (True, "rdb"))
        check("the manifest says rdb",
              dict(r.split(",", 1) for r in members(
                  out / "phase1-20260925.zip")[MANIFEST][1:])["source"],
              "rdb")
        check("qatt and quote are asked on the RDB connections, undated, "
              "with no partition list",
              (sorted({q for q in fakes[("qatt-host", 2)].asked
                       if "from qatt" in q}),
               qattsource.PARTITIONS_Q in fakes[("qatt-host", 2)].asked,
               {q for q, _a in fakes[("quote-host", 3)].asked}),
              ([qattsource.live_ticks_q(None, ["sym", T, "price", "size",
                                               "cond", "ex"])],
               False, {refdata.QUOTE_RDB_Q}))
        check("and the HDB ones are never asked anything",
              (fakes[("hdb-host", 4)].asked, fakes[("hdbq-host", 5)].asked),
              ([], []))
        check("the RDB's .z.D is logged next to D",
              "..  RDB date                2026-09-28   asked for 2026-09-25"
              in text, True)
        check("and a mismatch is a !! line, not a stop",
              "!!  the RDB is on 2026-09-28, not 2026-09-25: the prints are "
              "whatever day the RDB holds, filed under 2026-09-25" in text,
              True)
        rc, text = main_with("quote-host:3", ["--date", "2026-09-26",
                                              "--rdb"], hdb=hdb)
        check("a weekend D with --rdb is allowed",
              (rc, (out / "phase1-20260926.zip").exists()), (0, True))
        rc, text = main_with("quote-host:3", ["--rdb"], hdb=hdb)
        check("--rdb alone is a no-argument run: today, from the RDB - it "
              "resumes the no-argument run's staging of 2026-09-28",
              (rc, "done already, skipped" in text,
               "staged folder was built" in text), (0, True, False))
        rc, text = main_with("quote-host:3", ["--rdb"], hdb=hdb,
                             today=D(2026, 9, 27))
        check("so on a Sunday it stops as a no-argument run does",
              (rc, "is a Sunday" in text), (1, True))

    print("\nbuild: --date with --rdb")
    with tempfile.TemporaryDirectory() as tmp:
        qatt, quote, log, em = (FakeQatt(zd=D(2026, 9, 25)), FakeQuote(),
                                Caught(), FakeEm(client_fails=True))
        out = run(tmp, D(2026, 9, 25), qatt=qatt, quote=quote, log=log,
                  em=em, rdb=True)
        check("named D, read undated from the RDB, no partition list",
              (out.name, qattsource.PARTITIONS_Q in qatt.asked,
               {q for q, _a in quote.asked}),
              ("phase1-20260925.zip", False, {refdata.QUOTE_RDB_Q}))
        check("equity_master is resolved with D as today: .z.D-0",
              em.days_back, 0)
        check("the same .z.D: no !! about it",
              [ln for ln in log.lines if "the RDB is on" in ln], [])
        log = Caught()
        run(tmp, D(2026, 9, 25), qatt=FakeQatt(zd=None), log=log, rdb=True)
        check("a .z.D that cannot be read is a !!, and the run goes on",
              [ln.split(" on ")[0] for ln in log.lines if ".z.D" in ln],
              ["!!  could not read the RDB's .z.D"])
        err = attempt(lambda: run(tmp, D(2026, 9, 24), rdb=True,
                                  qatt=FakeQatt(empty=True)))
        check("no print at all with --rdb: the hint says to drop --rdb",
              str(err).split("; no zip written")[1],
              ". The RDB is on 2026-09-28, so it may no longer hold "
              "2026-09-24: run --date 2026-09-24 without --rdb to read "
              "the HDB")

    print("\nthe equity_master date, when the client-date query fails")
    with tempfile.TemporaryDirectory() as tmp:
        em = FakeEm(client_fails=True)
        run(tmp, D(2026, 9, 25), em=em)
        check("the server query is bounded at the trade day, not yesterday: "
              "today 2026-09-28 minus 3", em.days_back, 3)
        em = FakeEm(client_fails=True)
        run(tmp, None, em=em)
        check("an RDB run asks for today's, .z.D-0", em.days_back, 0)

    print("\nmarket by market, resumable")

    def same(z):
        """The members, the manifest's `exported at` aside."""
        return {n: [ln for ln in v if not ln.startswith("exported at,")]
                for n, v in z.items()}

    with tempfile.TemporaryDirectory() as tmp:
        whole = same(members(run(tmp, D(2026, 9, 25))))
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260925"
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  qatt=FakeQatt(fail_on={"7203.JP"})))
        check("a qatt failure on the second market (JT) stops the run",
              type(err).__name__, "ExtractError")
        check("the first market's three files are staged, the second's "
              "nothing, and no zip",
              (sorted(p.name for p in stage.iterdir()),
               sorted(p.name for p in stage.parent.glob("*.zip*"))),
              (sorted([EQUITY, LADDERS, MASTER, PX, STAGE,
                       "closes-HK.csv",
                       "quote_only-HK.csv", "ticks-HK.csv"]), []))

        em, qatt, quote, log = FakeEm(), FakeQatt(), FakeQuote(), Caught()
        out = run(tmp, D(2026, 9, 25), em=em, qatt=qatt, quote=quote,
                  log=log)
        check("the rerun does not ask qatt or quote for the done market",
              [s for s in qatt.syms + [x for _q, a in quote.asked
                                       for x in a[-1]]
               if s.endswith(".HK")], [])
        check("and says so", "..  HK done already, skipped" in log.lines,
              True)
        check("nor equity_master for what was staged",
              [q for q in em.asked if q in (
                  qattsource.MASTER_BPIPE_Q, refdata.EQUITY_Q,
                  refdata.IDS_Q)], [])
        check("the zip equals an uninterrupted run's",
              same(members(out)), whole)
        top = next((i for i, ln in enumerate(log.lines)
                    if ln.startswith("..  market ")), len(log.lines))
        check("the summary still covers the skipped market",
              [ln.split()[1:3] for ln in log.lines[top + 1:top + 2]],
              [["HK", "2"]])

        qatt = FakeQatt()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, fresh=True)
        check("--fresh re-reads every market",
              sorted(set(qatt.syms)),
              ["7203.JP", "8888.HK", "8889.HK", "AIA.NZ", "QQQ.XX",
               "ZZZ.XX"])
        check("and gives the same zip", same(members(out)), whole)

    print("\n--market: some markets this run")
    check("the codes are split on |, trimmed and upper-cased",
          parse_markets("nz| HK|nz"), ["NZ", "HK"])
    with tempfile.TemporaryDirectory() as tmp:
        em, qatt = FakeEm(), FakeQatt()
        err = attempt(lambda: run(tmp, D(2026, 9, 25), em=em, qatt=qatt,
                                  only=parse_markets("NZ|zzz-main")))
        check("a name that is neither a code nor a FidessaMarket stops the "
              "run, before kdb is asked anything, saying both were tried",
              (str(err), em.asked, qatt.asked),
              ("--market ZZZ-MAIN: neither an exchange code of "
               "close_conditions.csv nor a FidessaMarket of the universe's "
               "CrossCode rows", [], []))

    for names, want_ticks, want_syms, want_log in (
            ("NZE-MAIN", ["ticks-NZ.csv"], ["AIA.NZ"],
             "..  --market                NZE-MAIN -> NZ   1 of 5 markets "
             "this run"),
            ("NZ", ["ticks-NZ.csv"], ["AIA.NZ"],
             "..  --market                NZ   1 of 5 markets this run"),
            ("tyo-main|hk", ["ticks-HK.csv", "ticks-JT.csv"],
             ["7203.JP", "8888.HK", "8889.HK"],
             "..  --market                TYO-MAIN|HK -> JT|HK   2 of 5 "
             "markets this run")):
        with tempfile.TemporaryDirectory() as tmp:
            qatt, log = FakeQatt(), Caught()
            run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
                only=parse_markets(names))
            stage = Path(tmp) / "out" / "phase1-20260925"
            check(f"--market {names}: its markets, and the resolution logged",
                  (sorted(p.name for p in stage.glob("ticks-*.csv")),
                   sorted(set(qatt.syms)),
                   [ln for ln in log.lines if ln.startswith("..  --market")]),
                  (want_ticks, want_syms, [want_log]))

    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp) / "out"
        stage = outdir / "phase1-20260925"
        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
                  only=["HK", "NZ"])
        check("a --market run on a fresh day stages only those markets",
              (sorted(p.name for p in stage.glob("ticks-*.csv")),
               sorted(set(qatt.syms))),
              (["ticks-HK.csv", "ticks-NZ.csv"],
               ["8888.HK", "8889.HK", "AIA.NZ"]))
        check("and writes no zip, saying what is left",
              (out, sorted(p.name for p in outdir.glob("*.zip*")),
               [ln for ln in log.lines if "still to do" in ln]),
              (None, [], ["..  2 markets still to do (JT, XX); no zip yet - "
                          "run without --market to finish them"]))
        check("the selection is logged in step 1",
              [ln for ln in log.lines if ln.startswith("..  --market")],
              ["..  --market                HK|NZ   2 of 5 markets this run"])
        fingerprint = (stage / STAGE).read_text("utf-8")

        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log)
        check("the next run without --market reads only the others",
              sorted(set(qatt.syms)), ["7203.JP", "QQQ.XX", "ZZZ.XX"])
        check("and writes the zip a single full run writes",
              same(members(out)), whole)

        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log, only=["JT"])
        check("--market on a fully staged day re-reads only that market",
              sorted(set(qatt.syms)), ["7203.JP"])
        check("and rebuilds the zip", (out and out.name,
                                       same(members(out))),
              ("phase1-20260925.zip", whole))
        check("no .old or .part is left from the redo",
              sorted(p.name for p in stage.iterdir()
                     if p.suffix in (".old", ".part")), [])
        check("the fingerprint is the full universe's, whatever --market "
              "says: stage.csv is unchanged and nothing restarted",
              ((stage / STAGE).read_text("utf-8") == fingerprint,
               any("staged folder was built" in ln for ln in log.lines)),
              (True, False))

        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log, only=["XX"],
                  fresh=True)
        check("--fresh with --market resets every market and reads only "
              "the selected: no zip until a full run",
              (out, sorted(set(qatt.syms)),
               sorted(p.name for p in stage.glob("ticks-*.csv"))),
              (None, ["QQQ.XX", "ZZZ.XX"], ["ticks-XX.csv"]))

    print("\nresuming only what is still valid")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260925"
        run(tmp, D(2026, 9, 25))
        (stage / "ticks-JT.csv").unlink()
        (stage / "ticks-JT.csv.part").write_text("half a file", "utf-8")
        qatt = FakeQatt()
        out = run(tmp, D(2026, 9, 25), qatt=qatt)
        check("the crash window: closes and quote_only there, ticks not - "
              "the market is redone", sorted(set(qatt.syms)), ["7203.JP"])
        check("and the zip is whole", same(members(out)), whole)

        lines = (stage / "closes-HK.csv").read_text("utf-8").splitlines()
        (stage / "closes-HK.csv").write_text(
            "\n".join(lines[:-1]) + "\n", "utf-8")
        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log)
        check("a done market whose syms differ from this run's is redone",
              (sorted(set(qatt.syms)),
               any(ln.startswith("!!  HK staged for other syms")
                   for ln in log.lines)),
              (["8888.HK", "8889.HK"], True))
        check("and the zip is whole", same(members(out)), whole)

        qatt, log = FakeQatt(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
                  em=FakeEm(date=D(2026, 9, 25)))
        check("another equity_master date starts the day fresh",
              ([ln for ln in log.lines if ln.startswith("!!  staged")][:1],
               len(set(qatt.syms))),
              (["!!  staged folder was built from source hdb, equity_master "
                "date 2026-09-24; starting the day fresh"], 6))

        qatt, log = FakeQatt(), Caught()
        run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
            em=FakeEm(date=D(2026, 9, 25)),
            cc_text=CROSSCODE.replace("QQQ XX,XXX-MAIN,Equity\n", ""))
        check("so does another CrossCode",
              (any(ln.startswith("!!  staged folder was built from")
                   for ln in log.lines), len(set(qatt.syms))), (True, 5))

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260928"
        attempt(lambda: run(tmp, None, qatt=FakeQatt(fail_on={"7203.JP"})))
        qatt, log = FakeQatt(parts=[D(2026, 9, 25), D(2026, 9, 28)]), Caught()
        run(tmp, D(2026, 9, 28), qatt=qatt, log=log)
        check("an HDB run of a day an RDB run staged starts it fresh",
              (any(ln.startswith("!!  staged folder was built from source "
                                 "rdb") for ln in log.lines),
               "8888.HK" in qatt.syms), (True, True))

    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, D(2026, 9, 25))
        qatt, log = FakeQatt(), Caught()
        run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
            cc_text=CROSSCODE + "BBB US,NYS-MAIN,Equity\n"
                                "CCC LN,LSE-MAIN,Warrant\n")
        check("the CrossCode fingerprint covers only the rows kept: rows "
              "outside the universe change nothing",
              (any(ln.startswith("!!  staged folder") for ln in log.lines),
               qatt.syms), (False, []))

    print("\nstarting fresh on a network share: rename aside, never "
          "delete-then-create")
    with tempfile.TemporaryDirectory() as tmp:
        outdir = Path(tmp) / "out"
        run(tmp, D(2026, 9, 25))
        saved_rmtree = shutil.rmtree
        #  An SMB share: the old folder cannot be removed right away.
        shutil.rmtree = lambda path, ignore_errors=False: None
        try:
            qatt, log = FakeQatt(), Caught()
            err = attempt(lambda: run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
                                      em=FakeEm(date=D(2026, 9, 25))))
        finally:
            shutil.rmtree = saved_rmtree
        stale = sorted(p for p in outdir.iterdir() if ".stale-" in p.name)
        check("the auto-fresh goes through even when the old folder stays",
              (repr(err), len(set(qatt.syms))), ("None", 6))
        check("the old folder is renamed aside, not deleted",
              [read_csv(p / STAGE)[1]["value"] for p in stale],
              ["2026-09-24"])
        check("and the new one is created afresh",
              read_csv(outdir / "phase1-20260925" / STAGE)[1]["value"],
              "2026-09-25")
        check("the log says where it went, and that it is still there",
              ([ln[:47] for ln in log.lines
                if "old staging folder moved to" in ln],
               any(ln.startswith("!!") and "could not be removed" in ln
                   for ln in log.lines)),
              (["..  old staging folder moved to phase1-20260925"], True))
        qatt = FakeQatt()
        run(tmp, D(2026, 9, 25), qatt=qatt, em=FakeEm(date=D(2026, 9, 25)))
        check("a leftover .stale- folder is not taken for the staging "
              "folder: the next run resumes, and leaves it alone",
              (qatt.syms, sorted(p for p in outdir.iterdir()
                                 if ".stale-" in p.name) == stale),
              ([], True))

        log = Caught()
        run(tmp, D(2026, 9, 25), em=FakeEm(date=D(2026, 9, 25)), fresh=True,
            log=log)
        check("--fresh renames aside too, and removes the old folder when "
              "it can",
              (any("old staging folder moved to" in ln for ln in log.lines),
               sorted(p for p in outdir.iterdir()
                      if ".stale-" in p.name) == stale),
              (True, True))

    print("\nthe halved read size, across markets")
    with tempfile.TemporaryDirectory() as tmp:
        qatt, log = FakeQatt(), Caught()
        run(tmp, D(2026, 9, 25), qatt=qatt, log=log,
            extra={"SYM_CHUNK": 500})
        check("SYM_CHUNK is taken as set, with no cap and no warning",
              [ln for ln in log.lines if "SYM_CHUNK" in ln], [])
    with tempfile.TemporaryDirectory() as tmp:
        qatt = FakeQatt(max_syms=1)
        run(tmp, D(2026, 9, 25), qatt=qatt, extra={"SYM_CHUNK": 2},
            reconnect=lambda: qatt)
        check("HK is halved once, and every later market starts at 1",
              qatt.sizes, [2, 1, 1, 1, 1, 1, 1])

    print("\nPX_LAST, the sym's own row first")
    with tempfile.TemporaryDirectory() as tmp:
        em = FakeEm(equity=EQUITY_ROWS + [equity_row("7203.JT", 1)])
        cc_text = ("BloombergCode,FidessaMarket,Type\n"
                   "7203 JT,TYO-MAIN,Equity\nAIA NZ,NZE-MAIN,Equity\n")
        silent = {"7203.JP"}            # no print: its close is PX_LAST
        z = members(run(tmp, D(2026, 9, 25), em=em, cc_text=cc_text,
                        conds={"JT": [], "NZ": ["CA"]},
                        qatt=FakeQatt(silent=silent)))
        check("7203 JT picks 7203.JT for equity.csv",
              [r.split(",")[:3] for r in z[EQUITY][1:2]],
              [["7203 JT", "7203.JT", "1"]])
        check("but 7203.JP's fallback is its own row's PX_LAST",
              [r for r in z[CLOSES] if r.startswith("7203.JP,")],
              ["7203.JP,2871,equity_master,no-trades"])
        (Path(tmp) / "out" / "phase1-20260925" / "ticks-JT.csv").unlink()
        z = members(run(tmp, D(2026, 9, 25), em=FakeEm(equity=[]),
                        cc_text=cc_text, conds={"JT": [], "NZ": ["CA"]},
                        qatt=FakeQatt(silent=silent)))
        check("and a resumed run reads it back from px.csv",
              [r for r in z[CLOSES] if r.startswith("7203.JP,")],
              ["7203.JP,2871,equity_master,no-trades"])

    print("\n--for: what the zip is for")
    check("the uses parse in any case and order, into luld, td, ticks",
          (parse_uses("TD|luld"), parse_uses(""), parse_uses(" ticks ")),
          (("luld", "td"), USES, ("ticks",)))
    check("an unknown use is refused", type(attempt(
        lambda: parse_uses("luld|lul"))).__name__, "ValueError")
    check("the zip is named for its uses, in the fixed order",
          [bundle_name(D(2026, 9, 29), u) for u in
           (("luld",), ("luld", "td"), ("td", "ticks"), USES)],
          ["phase1-20260929-luld.zip", "phase1-20260929-luld-td.zip",
           "phase1-20260929-td-ticks.zip", "phase1-20260929.zip"])
    check("the members per use, the manifest always",
          [zip_members(u) for u in (("luld",), ("td",), ("ticks",))],
          [[MANIFEST, MASTER, CLOSES, EQUITY, LADDERS],
           [MANIFEST, MASTER, CLOSES, EQUITY],
           [MANIFEST, MASTER, TICKS, CLOSES, QUOTE_ONLY]])

    with tempfile.TemporaryDirectory() as tmp:
        qatt, quote, em, log = FakeQatt(), FakeQuote(), FakeEm(), Caught()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, quote=quote, em=em,
                  log=log, uses=("luld", "td"))
        z = members(out) if out else {}
        check("--for luld|td: named for it, with its members",
              (out and out.name, sorted(z)),
              ("phase1-20260925-luld-td.zip",
               sorted([MANIFEST, MASTER, CLOSES, EQUITY, LADDERS])))
        check("the manifest says what it is for",
              dict(r.split(",", 1) for r in z.get(MANIFEST, [])[1:])
              .get("for"), "luld|td")
        check("step 1 says it", [ln for ln in log.lines
                                 if ln.startswith("..  --for")],
              ["..  --for                   luld|td   -> "
               "phase1-YYYYMMDD-luld-td.zip"])
        check("no tick read and no quote: only the fast close query",
              ({k for k, _ in qatt.reads}, quote.asked),
              ({"fast"}, []))
        check("its closes, master, equity and ladders are the full zip's",
              {m: z.get(m) for m in (CLOSES, MASTER, EQUITY, LADDERS)},
              {m: whole[m] for m in (CLOSES, MASTER, EQUITY, LADDERS)})
        stage = Path(tmp) / "out" / "phase1-20260925"
        check("only closes are staged per market",
              sorted(p.name for p in stage.glob("*-*.csv")),
              ["closes-HK.csv", "closes-JT.csv", "closes-NZ.csv",
               "closes-XX.csv"])

    #  Close codes, a last trade, no trades, null times and two prices in
    #  one last second: the fast query must give the full path's closes.
    TRICKY = {
        "7203.JP": [("09:00:00", 2850, 100, "O"),
                    ("14:30:00", 2876, 100, "e"),
                    ("14:30:00", 2880, 100, "e"),
                    ("14:30:00", 2870, 100, ""),
                    ("14:30:05", 2890, 100, "X,e"),     # two codes, one ours
                    (None, 9999, 100, "e")],
        "AIA.NZ": [("06:00:00", 6.12, 100, ""),
                   ("12:44:58", 6.14, 100, "XT"),
                   ("12:44:58", 6.13, 100, ""),
                   ("12:45:00", 6.10, 100, "A"),        # sorts before XT
                   (None, 7.0, 100, "")],
        "ZZZ.XX": [(None, 1.5, 100, ""), (None, 1.7, 100, "")]}
    got = {}
    for u in (("ticks",), ("luld",)):
        with tempfile.TemporaryDirectory() as tmp:
            got[u] = members(run(tmp, D(2026, 9, 25), uses=u,
                                 qatt=FakeQatt(ticks=TRICKY)))[CLOSES]
    check("the fast closes are the full path's, on a tricky day",
          got[("luld",)], got[("ticks",)])
    check("and they are what the rule says",
          got[("luld",)],
          ["sym,close,source,reason", "8888.HK,3.4,equity_master,no-trades",
           "8889.HK,,,no-close", "7203.JP,2890,qatt,",
           "AIA.NZ,6.1,qatt,last-trade", "QQQ.XX,,,no-close",
           "ZZZ.XX,1.7,qatt,last-trade"])

    print("\nthe last trade before a market's cutoff")
    check("the time is HKT, kdb's own clock: 14:30 is 14:30:00",
          cut_seconds(D(2026, 9, 25), "14:30", "China Standard Time"),
          14 * 3600 + 1800)
    check("and a kdb in another clock gets it converted",
          cut_seconds(D(2026, 9, 25), "14:30:00", "Tokyo Standard Time"),
          15 * 3600 + 1800)
    check("a malformed time is refused",
          type(attempt(lambda: cut_seconds(D(2026, 9, 25), "4pm",
                                           "China Standard Time"))).__name__,
          "ValueError")
    mk = None
    cuts = {"HK": "16:00:00", "XX": "10:00"}
    CUTDAY = dict(TRICKY, **{
        "8888.HK": [("16:05:00", 3.5, 100, "")],        # all after
        "8889.HK": [("15:00:00", 3.55, 100, ""),
                    ("16:10:00", 3.6, 100, "CA")],      # a close, after
        "ZZZ.XX": [("09:00:00", 1.5, 100, ""), ("09:59:59", 1.6, 100, ""),
                   ("10:00:00", 1.55, 100, ""),         # at the cut
                   ("10:30:00", 1.9, 100, ""), (None, 1.95, 100, "")]})
    got, logs_ = {}, {}
    for u in (("ticks",), ("luld",)):
        with tempfile.TemporaryDirectory() as tmp:
            qatt, logs_[u] = FakeQatt(ticks=CUTDAY), Caught()
            got[u] = members(run(tmp, D(2026, 9, 25), uses=u, qatt=qatt,
                                 cutoffs=cuts, mk=mk,
                                 log=logs_[u]))[CLOSES]
            if u == ("luld",):
                cut_reads = [(k, syms) for k, syms in qatt.reads
                             if k == "cut"]
    check("with cutoffs, the fast closes are the full path's",
          got[("luld",)], got[("ticks",)])
    check("the last trade at or before the cut; a close after it still "
          "wins; all after is PX_LAST, none-before-cutoff",
          got[("luld",)],
          ["sym,close,source,reason",
           "8888.HK,3.4,equity_master,none-before-cutoff",
           "8889.HK,3.6,qatt,", "7203.JP,2890,qatt,",
           "AIA.NZ,6.1,qatt,last-trade", "QQQ.XX,,,no-close",
           "ZZZ.XX,1.55,qatt,last-trade"])
    check("the fast path asks the cut query for the cutoff markets' syms "
          "that traded, only",
          sorted(s for _k, syms in cut_reads for s in syms),
          ["8888.HK", "8889.HK", "ZZZ.XX"])
    check("step 1 says each cutoff, in HKT",
          [ln for ln in logs_[("ticks",)].lines
           if ln.startswith("..  last trade before")],
          ["..  last trade before       HK 16:00:00 HKT",
           "..  last trade before       XX 10:00:00 HKT"])
    check("a none-before-cutoff close is a !! line, and a summary column",
          ([ln for ln in logs_[("ticks",)].lines
            if "none-before-cutoff  ->" in ln],
           "none-before-cutoff" in next(
               ln for ln in logs_[("ticks",)].lines
               if ln.startswith("..  market "))),
          (["!!  close  8888.HK  HK  none-before-cutoff  -> equity_master "
            "3.4"], True))
    with tempfile.TemporaryDirectory() as tmp:
        em = FakeEm()
        err = attempt(lambda: run(tmp, D(2026, 9, 25), em=em,
                                  cutoffs={"HK": "16h00"}))
        check("a malformed LastTradeBefore stops the run, naming the code, "
              "before kdb is asked",
              (str(err), em.asked),
              ("LastTradeBefore of HK: '16h00' is not HH:MM or HH:MM:SS",
               []))

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "out" / "phase1-20260925"
        em = FakeEm()
        run(tmp, D(2026, 9, 25), em=em, uses=("td",))
        check("--for td fetches no ladder",
              (refdata.IDS_Q in em.asked, (stage / LADDERS).exists()),
              (False, False))
        qatt, em = FakeQatt(), FakeEm()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, em=em, uses=("luld",))
        check("a luld run after it reuses the staged closes, and fetches "
              "the ladders it lacks",
              (qatt.reads, refdata.IDS_Q in em.asked,
               out and out.name), ([], True, "phase1-20260925-luld.zip"))
        qatt = FakeQatt()
        out = run(tmp, D(2026, 9, 25), qatt=qatt, uses=("ticks",))
        check("a ticks run after it reads every market's ticks, the full "
              "way", sorted(s for k, syms in qatt.reads if k == "ticks"
                            for s in syms),
              sorted(["7203.JP", "8888.HK", "8889.HK", "AIA.NZ", "QQQ.XX",
                      "ZZZ.XX"]))
        check("and writes the closes the full zip has",
              members(out)[CLOSES], whole[CLOSES])
        qatt = FakeQatt()
        out = run(tmp, D(2026, 9, 25), qatt=qatt)
        check("then the full zip needs no read at all, and equals a "
              "single full run's", (qatt.reads, same(members(out))),
              ([], whole))
        nz = (stage / "ticks-NZ.csv").read_bytes()
        qatt = FakeQatt()
        run(tmp, D(2026, 9, 25), qatt=qatt, uses=("luld",), only=["NZ"])
        check("--market with --for luld redoes only NZ's closes, the fast "
              "way, and leaves its ticks alone",
              (qatt.reads, (stage / "ticks-NZ.csv").read_bytes() == nz),
              ([("fast", ["AIA.NZ"])], True))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
