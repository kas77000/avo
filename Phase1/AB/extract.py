#!/usr/bin/env python3
"""The day's AB extract for Nova: one zip that replaces what B-PIPE gave.

    EXPORT_DIR/phase1-20260925.zip
        manifest.csv    key,value               what, when, how many
        master.csv      BloombergCode,sym,...   the crosscode's equity_master rows
        ticks.csv       sym,time,price,size,cond,ex   every print, kdb's clock
        closes.csv      sym,close,source,reason one row per sym asked
        quote_only.csv  sym,time,bid,ask,cond   a sym with no print, its quote
        equity.csv      BloombergCode,sym,PX_LAST,...   equity_master fields
        ladders.csv     BloombergCode,sym,price,ticksize   raw tick ladders

Nova's R jobs (Phase1/Nova) turn it into the Historical tick files,
limitUpDown.csv and TradingData.csv.  Nova has no kdb, which is why every
lookup that needs one is done here and carried in the zip.

THE DAY AND THE SERVER.  With no argument the day is TODAY and qatt and
quote are read from the RDB (QATT_RDB_SERVER), which holds today only and
has no date column.  --date reads the HDB (QATT_SERVER): the newest
partition on or before that date, as qatt_export did.  equity_master and
the tick ladders always come from EQUITY_MASTER_SERVER, at the newest
equity_master date on or before the day.

THE UNIVERSE IS THE CROSSCODE, resolved to qatt syms exactly as
qatt_export.py did (equity_master in three passes, config/markets.csv as the
fallback).

THE CLOSE is closes.py's rule: the last print carrying one of its market's
close codes (config/close_conditions.csv), else equity_master's PX_LAST with
a reason, else no close at all.  It is worked out while ticks.csv streams,
one chunk of syms at a time, and only the answer is kept - a day of prints
is never held at once.  Every fallback is one `!!` line in the log, and the
run ends with a table per Bloomberg exchange code.

FAIL LOUDLY.  A kdb failure, or equity_master answering nothing for the
reference fetch, stops the run and leaves no zip.  The zip is written as
.part and renamed, so a zip under its real name is always a finished one.

    python extract.py                     today, from the RDB
    python extract.py --date 2026-09-25   that day, from the HDB
    python extract.py --log extract.log   tee the log to a file
    python extract.py --self-test         checks, no kdb
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
from decimal import Decimal, InvalidOperation
from pathlib import Path

import closes
import crosscode
import logs
import marketcfg
import qattsource
import refdata
import settings
import universe

HERE = Path(__file__).resolve().parent

MANIFEST, MASTER, TICKS = "manifest.csv", "master.csv", "ticks.csv"
CLOSES, QUOTE_ONLY = "closes.csv", "quote_only.csv"
EQUITY, LADDERS = "equity.csv", "ladders.csv"
MEMBERS = (MANIFEST, MASTER, TICKS, CLOSES, QUOTE_ONLY, EQUITY, LADDERS)

TICK_COLUMNS = ["sym", "time", "price", "size", "cond", "ex"]
MASTER_COLUMNS = ["BloombergCode"] + list(qattsource.MASTER_FIELDS)
CLOSE_COLUMNS = ["sym", "close", "source", "reason"]
QUOTE_COLUMNS = ["sym", "time", "bid", "ask", "cond"]
EQUITY_COLUMNS = ["BloombergCode", "sym"] + list(refdata.EQUITY_FIELDS)
LADDER_COLUMNS = ["BloombergCode", "sym", "price", "ticksize"]

#  The fallback reasons closes.resolve gives, in the summary's order.
REASONS = ("no-trades", "no-closing-trade", "no-close-codes")

#  What "too big" looks like from here - see historical_ticks.run.
TOO_BIG = ("wsfull", "limit", "abort")


class ExtractError(Exception):
    """The run stops here and writes no zip."""


def bundle_name(date) -> str:
    return f"phase1-{date:%Y%m%d}.zip"


def qatt_server(date):
    """(setting, source) for qatt and quote: the RDB for today, the HDB for
    a --date."""
    return ("QATT_SERVER", "hdb") if date else ("QATT_RDB_SERVER", "rdb")


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


def wanted_syms(rows, master, markets) -> list:
    """Every qatt sym the crosscode resolves to, once each."""
    return sorted({s for s, _src in
                   (universe.resolve_sym(r, master, markets) for r in rows)
                   if s})


def too_big(e) -> bool:
    return (isinstance(e, (ConnectionError, TimeoutError))
            or any(k in str(e) for k in TOO_BIG))


def fetch_chunks(conn, date, syms, cols, size, reconnect, log):
    """{sym: rows} per chunk of syms, in order.  `date` None reads the RDB.
    A read that is too big is halved and asked again on a fresh connection,
    and the smaller size is kept - as in historical_ticks.run.  One sym that
    still fails stops the export: a zip missing a name would look exactly
    like a quiet day."""
    size, pos, reads = max(1, int(size)), 0, 0
    while pos < len(syms):
        group = syms[pos:pos + size]
        t0 = time.monotonic()
        try:
            raw = (qattsource.fetch_live_raw(conn, group, cols)
                   if date is None else
                   qattsource.fetch_raw(conn, date, group, cols))
        except Exception as e:                              # noqa: BLE001
            if not too_big(e):
                raise
            if len(group) == 1:
                raise RuntimeError(f"qatt failed on ONE sym, {group[0]} on "
                                   f"{date or 'the RDB'}: {e}") from e
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


def write_ticks(z, chunks, seen) -> dict:
    """Stream ticks.csv into the open zip, and return the counts.

    `chunks` is fetch_chunks' answer.  One sym is only ever in one chunk,
    which keeps each sym's prints CONTIGUOUS in ticks.csv.  `seen(sym,
    rows)` is called once per sym with prints, while its rows are still in
    hand - that is where the close is worked out - and the rows are dropped
    with the chunk."""
    stats = {"prints": 0, "syms with prints": 0}
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
                seen(sym, rows)
        fh.flush()
        fh.detach()
    return stats


SUMMARY_COLUMNS = ("syms", "ticks", "qatt") + REASONS + ("no-close",
                                                         "quote-only")


def summary(groups, master, settled, ticked, quoted) -> dict:
    """{exchange code: {column: count}}.  A fallback is counted under its
    reason; a sym with no close at all only under no-close."""
    table = {}
    for sym, rows in groups.items():
        row = table.setdefault(market_of(rows, master),
                               dict.fromkeys(SUMMARY_COLUMNS, 0))
        c, _why = settled[sym]
        row["syms"] += 1
        row["ticks"] += sym in ticked
        row["quote-only"] += sym in quoted
        if c.source == "qatt":
            row["qatt"] += 1
        elif c.source == "equity_master":
            row[c.reason] += 1
        else:
            row["no-close"] += 1
    return table


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


def build(cfg, date, conns, log, today=None, markets=None, conditions=None):
    """Write EXPORT_DIR/phase1-YYYYMMDD.zip and return its path.

    `date` None is today from the RDB; a date is the HDB.  `conns` holds
    the open connections - "em" for equity_master and the ladders, "qatt"
    for the prints, "quote" for the quotes (qatt's when absent) - and
    "reconnect", which opens a fresh qatt one.  `markets` and `conditions`
    default to config/.  Raises ExtractError, or whatever kdb raised, and
    then no zip is left behind."""
    today = today or dt.date.today()
    if markets is None:
        markets = marketcfg.load(HERE / "config" / "markets.csv")
    if conditions is None:
        conditions = closes.load_conditions(
            HERE / "config" / "close_conditions.csv")
    _name, source = qatt_server(date)
    em = conns["em"]

    log.step(1, "crosscode")
    rows, dropped = crosscode.load(cfg["CROSSCODE_PATH"])
    log.kv("crosscode", logs.thousands(len(rows)) + " rows",
           str(cfg["CROSSCODE_PATH"]))
    for e in dropped:
        if e.rows:
            log.warn(f"{logs.thousands(len(e.rows))} rows dropped: {e.reason}")

    log.step(2, f"qatt, from the {source.upper()}")
    if source == "rdb":
        day = today
        log.kv("day", str(day), "today")
    else:
        parts = qattsource.partitions(conns["qatt"])
        day = pick_day(parts, today, date)
        if day is None:
            raise ExtractError(f"qatt has no partition on or before {date}")
        log.kv("day", str(day), f"newest partition on or before {date}")
    try:
        cols = qattsource.select_columns(qattsource.columns(conns["qatt"]))
    except ValueError as e:
        raise ExtractError(str(e)) from e
    log.kv("columns asked for", ", ".join(cols))

    log.step(3, "equity_master")
    master_date = qattsource.resolve_master_date(em, day)
    log.kv("equity_master date", master_date)
    cands = candidates(rows, markets)
    master, hits = {}, {"sym_bpipe": 0, "sym_mbpipe": 0, "sym": 0}
    keys, chunk = list(cands), int(cfg["MASTER_CHUNK"])
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
    groups = group_syms(rows, master, markets)
    syms = sorted(groups)
    log.kv("syms to export", logs.thousands(len(syms)))
    codes = {s: closes.codes_for_sym(exts_of(g), conditions)
             for s, g in groups.items()}

    log.step(4, "reference data")
    #  One candidate list per BloombergCode, the first crosscode row's.
    ref = {}
    for r in rows:
        if r.bbg not in ref:
            ref[r.bbg] = refdata.ref_candidates(
                r, markets, universe.resolve_sym(r, master, markets)[0])
    wanted = sorted({c for cs in ref.values() for c in cs})
    equity, ladders = {}, {}
    for i in range(0, len(wanted), chunk):
        equity.update(refdata.fetch_equity(em, master_date,
                                           wanted[i:i + chunk]))
        ladders.update(refdata.fetch_ladders(em, wanted[i:i + chunk]))
    if not equity:
        raise ExtractError(
            f"equity_master answered no row for any of "
            f"{logs.thousands(len(wanted))} syms on {master_date}")
    eq_pick = {b: refdata.pick_ref(None, cs, equity) for b, cs in ref.items()}
    lad_pick = {b: refdata.pick_ref(None, cs, ladders)
                for b, cs in ref.items()}
    log.kv("equity rows", f"{sum(1 for p in eq_pick.values() if p):,} of "
                          f"{len(ref):,} codes")
    n_lad = sum(1 for p in lad_pick.values() if p)
    if n_lad:
        log.kv("ladders", f"{n_lad:,} of {len(ref):,} codes")
    else:
        log.warn(f"no tick ladder for any of {len(ref):,} codes "
                 f"({len(wanted):,} syms asked); ladders.csv is empty")

    def px_of(sym):
        """PX_LAST of the sym itself, else of the row its codes found."""
        for s in [sym] + [eq_pick.get(r.bbg) for r in groups.get(sym, [])]:
            if s and s in equity:
                return px_or_none(equity[s]["PX_LAST"])
        return None

    settled = {}

    def settle(sym, prints):
        """(Close, why): `why` is the fallback reason even for a sym that
        ends with no close, so its log line can say it."""
        c = closes.resolve(sym, prints, codes.get(sym, []), px_of(sym))
        why = c.reason
        if c.reason == "no-close":
            why = closes.resolve(sym, prints, codes.get(sym, []), "?").reason
        settled[sym] = (c, why)

    def reconnect():
        conns["qatt"] = conns["reconnect"]()
        return conns["qatt"]

    log.step(5, "ticks and closes")
    out = Path(cfg["EXPORT_DIR"]) / bundle_name(day)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            stats = write_ticks(z, fetch_chunks(
                conns["qatt"], None if source == "rdb" else day, syms, cols,
                cfg["SYM_CHUNK"], reconnect, log), settle)
            ticked = set(settled)

            log.step(6, "quotes, for syms with no print")
            quiet = [s for s in syms if s not in ticked]
            qconn, n = conns.get("quote") or conns["qatt"], int(cfg["SYM_CHUNK"])
            quotes = {}
            for i in range(0, len(quiet), n):
                quotes.update(refdata.fetch_quotes(
                    qconn, None if source == "rdb" else day, quiet[i:i + n]))
            log.kv("no print", logs.thousands(len(quiet)),
                   f"{logs.thousands(len(quotes))} with a quote")
            quote_rows = []
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

            log.step(7, "closes")
            for s in syms:
                c, why = settled[s]
                exts = ",".join(exts_of(groups[s]))
                if c.source == "equity_master":
                    log.warn(f"close  {s}  {exts}  {c.reason}  -> "
                             f"equity_master {c.close}")
                elif c.source != "qatt":
                    log.warn(f"close  {s}  {exts}  {why}  -> no close, "
                             f"PX_LAST null or <= 0")
            got = [settled[s][0] for s in syms]
            count = {"qatt": sum(c.source == "qatt" for c in got),
                     "equity_master": sum(c.source == "equity_master"
                                          for c in got),
                     "none": sum(c.reason == "no-close" for c in got)}

            z.writestr(MASTER, _csv([MASTER_COLUMNS] + [
                [b] + [m.get(f, "") for f in qattsource.MASTER_FIELDS]
                for b, m in sorted(master.items())]))
            z.writestr(CLOSES, _csv([CLOSE_COLUMNS] + [
                [c.sym, c.close, c.source, c.reason] for c in got]))
            z.writestr(QUOTE_ONLY, _csv([QUOTE_COLUMNS] + quote_rows))
            z.writestr(EQUITY, _csv([EQUITY_COLUMNS] + [
                [b, p] + [equity[p][f] for f in refdata.EQUITY_FIELDS]
                for b, p in sorted(eq_pick.items()) if p]))
            z.writestr(LADDERS, _csv([LADDER_COLUMNS] + [
                [b, p, price, tick]
                for b, p in sorted(lad_pick.items()) if p
                for price, tick in ladders[p]]))
            manifest = {
                "date": day, "source": source,
                "equity_master date": master_date,
                "time column": qattsource.TIME_FIELD,
                "kdb timezone": cfg["KDB_TIMEZONE"],
                "syms asked": len(syms), **stats,
                "quote only": len(quote_rows),
                "closes from qatt": count["qatt"],
                "closes from equity_master": count["equity_master"],
                "no close": count["none"],
                "exported at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"}
            z.writestr(MANIFEST, _csv([["key", "value"]] + [
                [k, str(v)] for k, v in manifest.items()]))
        os.replace(part, out)
    except BaseException:
        if part.exists():
            part.unlink()
        raise

    log.step(8, "result")
    log_summary(summary(groups, master, settled, ticked,
                        {r[0] for r in quote_rows}), log)
    log.info()
    log.kv("syms with prints", f"{logs.thousands(stats['syms with prints'])} "
                               f"of {logs.thousands(len(syms))}")
    log.kv("prints", logs.thousands(stats["prints"]))
    if not stats["prints"]:
        log.warn(f"qatt gave no print at all for {day}")
    log.kv("closes", f"qatt {count['qatt']:,}, equity_master "
                     f"{count['equity_master']:,}, none {count['none']:,}")
    log.kv("quote only", logs.thousands(len(quote_rows)))
    log.ok(f"written  {out.stat().st_size / 1e6:,.1f} MB  {out}")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The day's qatt prints, closes and reference data, in "
                    "one zip for Nova.")
    p.add_argument("--date", default="",
                   help="YYYY-MM-DD: that day from the HDB, instead of "
                        "today from the RDB")
    p.add_argument("--log", default="", help="tee the log to this file")
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
    q_name, source = qatt_server(date)
    try:
        cfg = settings.load()
        settings.require(cfg, "CROSSCODE_PATH", "EXPORT_DIR")
        em_host, em_port = settings.server(cfg, "EQUITY_MASTER_SERVER")
        q_host, q_port = settings.server(cfg, q_name)
    except settings.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    log = logs.Log(path=a.log or None)
    log.kv("qatt and quote", source, f"{q_name}")
    try:
        conns = {"em": qattsource.connect(em_host, em_port),
                 "qatt": qattsource.connect(q_host, q_port),
                 "reconnect": lambda: connect_again(q_host, q_port, log)}
        build(cfg, date, conns, log)
    except SystemExit as e:             # qattsource's explained failures
        log.fail(str(e))
        log.fail("no zip written")
        return 1
    except Exception as e:                                  # noqa: BLE001
        log.fail(f"{type(e).__name__}: {e}")
        log.fail("no zip written")
        return 1
    finally:
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
        def __init__(self, equity=EQUITY_ROWS):
            self.equity, self.asked = equity, []

        def __call__(self, q, *args):
            self.asked.append(q)
            if q == qattsource.MAXDATE_CLIENT_Q:
                return em_date
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

    class FakeQatt:
        def __init__(self, fail=False):
            self.asked, self.fail = [], fail

        def __call__(self, q, *args):
            self.asked.append(q)
            if q == qattsource.PARTITIONS_Q:
                return [D(2026, 9, 24), D(2026, 9, 25)]
            if q == qattsource.COLUMNS_Q:
                return ["sym", "time", T, "price", "size", "cond", "ex"]
            if "from qatt" in q:
                if self.fail:
                    raise RuntimeError("'type")
                return [{"sym": s, T: dt.time.fromisoformat(t), "price": p,
                         "size": n, "cond": c, "ex": "X"}
                        for s in args[-1] for t, p, n, c in
                        TICK_ROWS.get(s, [])]
            raise AssertionError(f"qatt was asked {q}")

    class FakeQuote:
        def __init__(self):
            self.asked = []

        def __call__(self, q, *args):
            self.asked.append((q, args))
            have = {"8888.HK": dt.time(16, 8, 2), "QQQ.XX": dt.time(9, 0)}
            return [{"sym": s, "time": have[s], "bid": 3.41, "ask": 3.43}
                    for s in args[-1] if s in have]

    class Caught(logs.Log):
        def __init__(self):
            super().__init__(stamps=False, quiet=True)
            self.lines = []

        def emit(self, level, text=""):
            self.lines.append(self.line(level, text))

    conditions = {"JT": ["e", "ES"], "NZ": ["CA"], "HK": ["CA"]}

    def run(tmp, date, em=None, qatt=None, quote=None, log=None):
        cc = Path(tmp) / "CrossCode.csv"
        cc.write_text(
            "BloombergCode,FidessaMarket,Type\n"
            "7203 JT,TYO-MAIN,\n7203 JE,JNX-MAIN,\nAIA NZ,NZE-MAIN,\n"
            "8888 HK,HKG-MAIN,\n8889 HK,HKG-MAIN,\nZZZ XX,XXX-MAIN,\n"
            "QQQ XX,XXX-MAIN,\nBSKT HK,HKG-MAIN,Basket\n", encoding="utf-8")
        cfg = dict(settings.DEFAULTS, CROSSCODE_PATH=str(cc),
                   EXPORT_DIR=str(Path(tmp) / "out"))
        conns = {"em": em or FakeEm(), "qatt": qatt or FakeQatt(),
                 "quote": quote or FakeQuote(),
                 "reconnect": lambda: FakeQatt()}
        return build(cfg, date, conns, log or Caught(), today=D(2026, 9, 28),
                     markets=markets, conditions=conditions)

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
        check("no .part is left behind",
              out and sorted(p.name for p in out.parent.iterdir()),
              ["phase1-20260925.zip"])
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
        check("closes: one row per sym asked, every kind of answer",
              z.get(CLOSES),
              ["sym,close,source,reason",
               "7203.JP,2876,qatt,",
               "8888.HK,3.4,equity_master,no-trades",
               "8889.HK,,,no-close",
               "AIA.NZ,6.13,equity_master,no-closing-trade",
               "QQQ.XX,,,no-close",
               "ZZZ.XX,1.4,equity_master,no-close-codes"])
        check("a no-trades sym with a quote is quote-only, cond its first "
              "close code; one with no close codes is skipped",
              z.get(QUOTE_ONLY),
              ["sym,time,bid,ask,cond", "8888.HK,16:08:02,3.41,3.43,CA"])
        check("the quote is asked for the syms with no print, on the HDB",
              quote.asked and (quote.asked[0][0], quote.asked[0][1][0],
                               sorted(quote.asked[0][1][1])),
              (refdata.QUOTE_HDB_Q, D(2026, 9, 25),
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
              ["date", "source", "equity_master date", "time column",
               "kdb timezone", "syms asked", "prints", "syms with prints",
               "quote only", "closes from qatt", "closes from equity_master",
               "no close", "exported at"])
        check("manifest values",
              [manifest.get(k) for k in
               ("date", "source", "equity_master date", "time column",
                "syms asked", "prints", "syms with prints", "quote only",
                "closes from qatt", "closes from equity_master", "no close")],
              ["2026-09-25", "hdb", "2026-09-24", T, "6", "7", "3", "1",
               "1", "3", "2"])
        check("one !! line per fallback, with the market and the price",
              [ln for ln in log.lines if ln.startswith("!!  close")],
              ["!!  close  8888.HK  HK  no-trades  -> equity_master 3.4",
               "!!  close  8889.HK  HK  no-trades  -> no close, PX_LAST "
               "null or <= 0",
               "!!  close  AIA.NZ  NZ  no-closing-trade  -> equity_master "
               "6.13",
               "!!  close  QQQ.XX  XX  no-close-codes  -> no close, PX_LAST "
               "null or <= 0",
               "!!  close  ZZZ.XX  XX  no-close-codes  -> equity_master 1.4"])
        check("a quote with no close code to label it is a !! line",
              [ln for ln in log.lines if ln.startswith("!!  quote")],
              ["!!  quote  QQQ.XX  XX  no close codes for its market, "
               "quote-only row skipped"])
        table = [ln for ln in log.lines if ln.startswith("..  HK ")
                 or ln.startswith("..  JT ") or ln.startswith("..  market")]
        check("a summary row per exchange code: syms, with ticks, qatt, "
              "fallbacks by reason, no-close, quote-only",
              [ln.split()[1:] for ln in table],
              [["market", "syms", "ticks", "qatt", "no-trades",
                "no-closing-trade", "no-close-codes", "no-close",
                "quote-only"],
               ["HK", "2", "0", "0", "1", "0", "0", "1", "1"],
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
              sorted(p.name for p in outdir.iterdir())
              if outdir.exists() else [], [])
        err = attempt(lambda: run(tmp, D(2026, 9, 25),
                                  qatt=FakeQatt(fail=True)))
        check("a qatt failure mid-stream stops the run",
              type(err).__name__, "RuntimeError")
        check("and leaves no zip and no .part",
              sorted(p.name for p in outdir.iterdir())
              if outdir.exists() else [], [])
        err = attempt(lambda: run(tmp, D(2026, 9, 1)))
        check("a --date before every partition stops the run",
              type(err).__name__, "ExtractError")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
