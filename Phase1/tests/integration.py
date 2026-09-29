#!/usr/bin/env python3
"""AB -> Nova integration round for Phase1, on fake kdb and the real R jobs.

    python Phase1/tests/integration.py            scenarios 1-3
    python Phase1/tests/integration.py --big      and the ~3M-print day
    python Phase1/tests/integration.py --keep     leave the temp dir behind

AB's REAL extract.build() runs on fake connections that answer the queries
the way kdb would: the condensed read (`0!select size:sum size by sym,
tradeTime:tradeTime.second, price, cond, ex ...`) is answered by actually
grouping the day's raw prints and summing their size, and the uncondensed
read with the raw prints themselves. Nova's REAL R jobs (historical.r,
limit_up_down.r, trading_data.r, run_phase1.cmd) then run on the zips, each
from a temp copy of Phase1/Nova with its own settings.r.

    1  condensed vs raw         the same day, zip A condensed, zip B raw
    2  time representations     int, timedelta, pandas, numpy, a frame, null
    3  workflow                 AB resume and --market, run_phase1.cmd,
                                historical.r --market=NZE-MAIN and a rerun
    4  --big                    timings only, no pass/fail on them

The trade day is the last weekday before today, so limit_up_down.r's and
trading_data.r's date guards pass. R is P1_RSCRIPT, or R 3.2.2 at its
usual place on this PC. Everything is written under one temp dir.

Zip B is forced down the uncondensed path by patching
qattsource.condensed() to say no, for that one build: the query, the time
unit and the log all follow from it, exactly as they would for a qatt
lacking a column - but every column is still there to compare.

Ends with `all checks passed` or `SOME CHECKS FAILED`.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import datetime as dt
import io
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
AB = REPO / "Phase1" / "AB"
NOVA = REPO / "Phase1" / "Nova"
RSCRIPT = os.environ.get("P1_RSCRIPT",
                         "C:/Users/user/r322/bin/x64/Rscript.exe")

sys.path.insert(0, str(AB))
import crosscode                                            # noqa: E402
import extract                                              # noqa: E402
import logs                                                 # noqa: E402
import qattsource                                           # noqa: E402
import refdata                                              # noqa: E402
import settings                                             # noqa: E402

T = qattsource.TIME_FIELD
COLS = ["sym", "time", T, "price", "size", "cond", "ex"]
CONDENSED_MARK = "0!select size:sum size by"


def last_weekday(today):
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


TODAY = dt.date.today()
DAY = last_weekday(TODAY)
YMD = f"{DAY:%Y%m%d}"

# -- checks -------------------------------------------------------------

RESULTS = []


def check(name, good, detail=""):
    good = bool(good)
    RESULTS.append((name, good, detail))
    print(f"  {'ok  ' if good else 'FAIL'}  {name}"
          + ("" if good or not detail else f"\n          {detail}"))
    return good


def section(title):
    print(f"\n{title}")


# -- the synthetic day ----------------------------------------------------

CC_HEADER = ["#FidessaCode", "RicCode", "Type", "BloombergCode",
             "BloombergSecurityType", "FidessaMarket", "Currency",
             "BloombergStatus", "Mnemo", "VenueList", "BSEBloombergCode",
             "BSERic"]

JP_LADDER = [("1000", "0.1"), ("3000", "0.5"), ("10000", "1"),
             ("30000", "5"), ("100000", "10"), ("300000", "50"),
             ("1000000", "100"), ("999999999", "1000")]
KR_LADDER = [("2000", "1"), ("5000", "5"), ("20000", "10"), ("50000", "50"),
             ("200000", "100"), ("500000", "500"), ("999999999", "1000")]
KR_ETF_LADDER = [("2000", "1"), ("999999999", "5")]

#  Per market, in kdb's clock (China Standard Time): the session in
#  seconds of day, the close code, the exchange letters, the currency.
SESSIONS = {
    "JT": (8 * 3600, 14 * 3600 + 1800, "e", ["T"], "JPY"),
    "KP": (8 * 3600, 14 * 3600 + 1800, "GC", ["K"], "KRW"),
    "HK": (9 * 3600 + 1800, 16 * 3600 + 480, "CA", ["H"], "HKD"),
    "NZ": (5 * 3600, 11 * 3600 + 2700, "CA", ["N"], "NZD"),
    "IS": (11 * 3600 + 2700, 18 * 3600 + 1800, "AUC", ["N", "B"], "INR"),
}


class Name(SimpleNamespace):
    """One CrossCode row, what equity_master knows of it, and its day."""


def N(bbg, market, typ, sym, prim, comp, mic, px, *, ladder=None,
      long="", kind="trade", code=None, base=None, tick="1"):
    ticker, ext = bbg.split(" ")
    return Name(bbg=bbg, market=market, type=typ, sym=sym, prim=prim,
                comp=comp, mic=mic, px=px, ladder=ladder,
                long=long or f"FIXTURE {ticker} {typ.upper()}", kind=kind,
                code=code or bbg, ext=ext, ticker=ticker,
                base=Decimal(base or px or "1"), tick=Decimal(tick),
                ric=f"{ticker}.{ext}X", fcode=f"{ticker}.{market[:3]}")


def small_names():
    return [
        N("7203 JT", "TYO-MAIN", "Equity", "7203.JP", "JT", "JP", "XTKS",
          "2871", ladder=JP_LADDER, long="TOYOTA MOTOR CORP",
          code="7203 JP"),
        N("6758 JT", "TYO-MAIN", "Equity", "6758.JP", "JT", "JP", "XTKS",
          "13050", ladder=JP_LADDER, long="SONY GROUP CORP", code="6758 JP",
          tick="5"),
        N("1570 JT", "TYO-MAIN", "ETF", "1570.JP", "JT", "JP", "XTKS",
          "28500", ladder=JP_LADDER,
          long="NEXT FUNDS NIKKEI 225 LEVERAGED INDEX ETF", code="1570 JP",
          tick="10"),
        N("005930 KP", "KSC-MAIN", "Equity", "005930.KS", "KP", "KS", "XKRX",
          "71300", ladder=KR_LADDER, long="SAMSUNG ELECTRONICS CO LTD",
          tick="100"),
        N("299990 KP", "KSC-MAIN", "ETF", "299990.KS", "KP", "KS", "XKRX",
          "15250", ladder=KR_ETF_LADDER, long="FIXTURE KOSPI200 2X ETF",
          tick="5"),
        N("700 HK", "HKG-MAIN", "Equity", "700.HK", "HK", "HK", "XHKG",
          "385.2", long="TENCENT HOLDINGS LTD", tick="0.2"),
        N("12345 HK", "HKG-MAIN", "Warrant", "12345.HK", "HK", "HK", "XHKG",
          "0.105", long="FIXTURE TENCENT CALL WARRANT", tick="0.001"),
        N("8888 HK", "HKG-MAIN", "Equity", "8888.HK", "HK", "HK", "XHKG",
          "3.4", kind="quote", long="FIXTURE HK QUOTE LTD"),
        N("8889 HK", "HKG-MAIN", "Equity", "8889.HK", "HK", "HK", "XHKG",
          "", kind="none", long="FIXTURE HK SUSPENDED LTD"),
        N("AIA NZ", "NZE-MAIN", "Equity", "AIA.NZ", "NZ", "NZ", "XNZE",
          "6.13", long="AUCKLAND INTL AIRPORT LTD", tick="0.01"),
        N("FPH NZ", "NZE-MAIN", "Equity", "FPH.NZ", "NZ", "NZ", "XNZE",
          "33.1", long="FISHER & PAYKEL HEALTHCARE", tick="0.01"),
        N("INFY IS", "NSI-MAIN", "Equity", "INFY.IS", "IS", "IN", "XNSE",
          "1850.5", long="INFOSYS LTD", code="INFY IN", tick="0.05"),
    ]


#  An NZE-MAIN name equity_master does not know: Nova resolves it to
#  NOMAS.NZ from hist_markets.csv; AB needs the same from markets.csv.
NOMAS = N("NOMAS NZ", "NZE-MAIN", "Equity", "", "NZ", "NZ", "", "2.5",
          long="FIXTURE NZ NO MASTER", tick="0.01")
NOMAS_SYM = "NOMAS.NZ"


def ms_time(sec, ms):
    return dt.time(sec // 3600, sec // 60 % 60, sec % 60, ms * 1000)


def raw_prints(name):
    """The name's raw qatt prints, in time order: runs of prints sharing
    second, price, cond and ex, other prices in the same second, a
    morning close code for Japan, and three prints of the close in the
    session's last second at one price."""
    start, end, close, exs, _ = SESSIONS[name.ext]
    rng = random.Random(zlib.crc32(name.bbg.encode()))
    keys = []
    s = start + rng.randint(0, 30)
    mid = start + (end - start) // 3
    did_mid = False
    while s < end - 10 and len(keys) < 60:
        if name.ext == "JT" and not did_mid and s >= mid:
            keys += [(s, name.base, close, exs[0])] * 2
            did_mid = True
        price = name.base + name.tick * rng.randint(-6, 6)
        if price <= 0:
            price = name.base
        cond = rng.choice(["", "", "", "", "X", "R,S"])
        ex = rng.choice(exs)
        keys += [(s, price, cond, ex)] * rng.choice([1, 1, 2, 3, 4])
        s += rng.choice([0, 0, 1, 1, 2, 7, 60, 300, 900])
    closing = name.base + name.tick * rng.randint(-3, 3)
    keys += [(end, closing, close, exs[0])] * 3
    #  Within a second, the keys interleave as prints would.
    by_sec = collections.OrderedDict()
    for k in keys:
        by_sec.setdefault(k[0], []).append(k)
    rows = []
    for sec, ks in by_sec.items():
        if sec != end:
            rng.shuffle(ks)
        mss = sorted(rng.sample(range(1000), len(ks)))
        for (sec_, price, cond, ex), ms in zip(ks, mss):
            rows.append({"sym": name.sym or NOMAS_SYM, T: ms_time(sec, ms),
                         "price": price, "size": rng.randint(1, 40) * 100,
                         "cond": cond, "ex": ex})
    return rows


class Day:
    """Names, their raw prints and quotes, and the CrossCode file."""

    def __init__(self, names, extra=()):
        self.names = list(names) + list(extra)
        self.raw = {}
        self.quotes = {}
        for n in self.names:
            sym = n.sym or NOMAS_SYM
            if n.kind == "trade":
                self.raw[sym] = raw_prints(n)
            elif n.kind == "quote":
                self.quotes[sym] = (dt.time(16, 8, 2), Decimal("3.41"),
                                    Decimal("3.43"))

    def write_crosscode(self, path):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(CC_HEADER)
            for n in self.names:
                w.writerow([n.fcode, n.ric, n.type, n.bbg, "Common Stock",
                            n.market, SESSIONS[n.ext][4], "ACTV",
                            n.ticker, "", "", ""])
        return path


def condense(rows, rep=lambda s: dt.timedelta(seconds=s)):
    """What kdb answers ticks_q with: size summed by sym, second, price,
    cond, ex, sorted by them, the time a q second."""
    summed = {}
    for r in rows:
        t = r[T]
        key = (r["sym"], t.hour * 3600 + t.minute * 60 + t.second,
               r["price"], r["cond"], r["ex"])
        summed[key] = summed.get(key, 0) + r["size"]
    return [{"sym": k[0], T: rep(k[1]), "price": k[2], "cond": k[3],
             "ex": k[4], "size": v} for k, v in sorted(summed.items())]


class FakeTable:
    """A pykx table as extract sees it: one .pd()."""

    def __init__(self, df):
        self.df = df

    def pd(self):
        return self.df.copy()


class FakeKdb:
    """equity_master, qatt and quote for one Day.

    `time_rep` turns a condensed line's seconds into what the connection
    hands back; `frame` answers with a pandas frame instead of rows;
    `null_at` is (sym, index) of a condensed line whose time is null;
    `fail_syms` makes a qatt read of any of them raise."""

    def __init__(self, day, time_rep=None, frame=False, null_at=None,
                 fail_syms=()):
        self.day = day
        self.time_rep = time_rep or (lambda s: dt.timedelta(seconds=s))
        self.frame = frame
        self.null_at = null_at
        self.fail_syms = set(fail_syms)
        self.asked = []             # (kind, syms) per qatt tick read
        master = []
        for n in day.names:
            if not n.sym:
                continue
            master.append({"BloombergCode": n.bbg, "sym": n.sym,
                           "EQY_PRIM_EXCH_SHRT": n.prim,
                           "COMPOSITE_EXCH_CODE": n.comp,
                           "ID_MIC_PRIM_EXCH": n.mic,
                           "sym_bpipe": crosscode.bbg_dotted(n.bbg),
                           "sym_mbpipe": crosscode.bbg_full(n.bbg)})
        self.master = master
        self.equity, self.ladders = {}, {}
        for i, n in enumerate(day.names):
            if not n.sym:
                continue
            self.equity[n.sym] = {
                "PX_LAST": n.px or None, "EQY_BETA": "1.05",
                "volatility": "0.24", "REL_INDEX": "IDX",
                "CUR_MKT_CAP": str(1000 + 37 * i), "fx_last": "0.128",
                "ID_ISIN": f"XX00FIXT{i:04d}", "INDUSTRY_SECTOR": "Industrial",
                "LONG_COMP_NAME": n.long}
            if n.ladder:
                self.ladders[n.sym] = n.ladder
        self.ids = {s: str(i) for i, s in enumerate(sorted(self.ladders), 1)}

    def em(self, q, *args):
        if q in (qattsource.MAXDATE_CLIENT_Q, qattsource.MAXDATE_SERVER_Q):
            return DAY
        s = set(args[-1]) if args else set()
        for query, col in ((qattsource.MASTER_BPIPE_Q, "sym_bpipe"),
                           (qattsource.MASTER_MBPIPE_Q, "sym_mbpipe"),
                           (qattsource.MASTER_SYM_Q, "sym")):
            if q == query:
                return [r for r in self.master if r[col] in s]
        if q == refdata.EQUITY_Q:
            return [dict(v, sym=k) for k, v in self.equity.items() if k in s]
        if q == refdata.IDS_Q:
            return [{"sym": k, "id": v} for k, v in self.ids.items()
                    if k in s]
        if q == refdata.TBL_Q:
            return [{"id": self.ids[k], "price": Decimal(p),
                     "ticksize": Decimal(t)}
                    for k, lad in self.ladders.items() for p, t in lad]
        raise AssertionError(f"equity_master was asked {q}")

    def qatt(self, q, *args):
        if q == extract.TABLES_Q:
            return ["qatt", "quote"]
        if q == qattsource.PARTITIONS_Q:
            return [last_weekday(DAY), DAY]
        if q == qattsource.COLUMNS_Q:
            return list(COLS)
        if "from qatt" not in q:
            raise AssertionError(f"qatt was asked {q}")
        syms = list(args[-1])
        condensed = CONDENSED_MARK in q
        self.asked.append(("condensed" if condensed else "raw", syms, q))
        if self.fail_syms & set(syms):
            raise RuntimeError("'type (a fake kdb failure)")
        raw = [r for s in syms for r in self.day.raw.get(s, [])]
        if not condensed:
            return raw
        rows = condense(raw, self.time_rep)
        if self.null_at:
            sym, i = self.null_at
            mine = [r for r in rows if r["sym"] == sym]
            if mine:
                mine[i][T] = None
        if not self.frame:
            return rows
        import numpy as np
        import pandas as pd
        secs = [None if r[T] is None else int(r[T].total_seconds())
                for r in rows]
        df = pd.DataFrame({
            "sym": pd.Series([r["sym"] for r in rows], dtype=object),
            T: pd.Series(np.array([np.timedelta64("NaT") if s is None else
                                   np.timedelta64(s, "s") for s in secs],
                                  dtype="timedelta64[s]")),
            "price": pd.Series([r["price"] for r in rows], dtype=object),
            "cond": pd.Series([r["cond"] for r in rows], dtype=object),
            "ex": pd.Series([r["ex"] for r in rows], dtype=object),
            "size": pd.Series([r["size"] for r in rows], dtype="int64")})
        return FakeTable(df)

    def quote(self, q, *args):
        if q == extract.TABLES_Q:
            return ["quote"]
        s = set(args[-1])
        return [{"sym": k, "time": v[0], "bid": v[1], "ask": v[2]}
                for k, v in self.day.quotes.items() if k in s]

    def conns(self):
        return {"em": self.em, "qatt": self.qatt, "quote": self.quote,
                "reconnect": lambda: self.qatt}

    def tick_syms(self):
        return [s for _, syms, _ in self.asked for s in syms]


class FileLog(logs.Log):
    """AB's log to a file only, with its lines kept."""

    def __init__(self, path):
        super().__init__(path=path)
        self.lines = []

    def emit(self, level, text=""):
        rendered = self.line(level, text)
        self.lines.append(rendered)
        if self.fh:
            self.fh.write(rendered + "\n")
            self.fh.flush()


@contextlib.contextmanager
def uncondensed():
    """qatt 'lacks' what condensing needs: the read falls back to one line
    per print, and every consequence (query, time unit, log) follows."""
    was = qattsource.condensed
    qattsource.condensed = lambda cols, time_field=None: False
    try:
        yield
    finally:
        qattsource.condensed = was


def ab_build(export, cc, kdb, only=None, fresh=False, chunk=2):
    cfg = dict(settings.DEFAULTS, CROSSCODE_PATH=str(cc),
               EXPORT_DIR=str(export), SYM_CHUNK=chunk)
    Path(export).mkdir(parents=True, exist_ok=True)
    log = FileLog(Path(export) / "extract.log")
    try:
        return extract.build(cfg, DAY, kdb.conns(), log, today=TODAY,
                             fresh=fresh, only=only), log
    finally:
        log.close()


def members(zip_path, drop_exported=True):
    out = {}
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            b = z.read(n)
            if drop_exported and n == "manifest.csv":
                b = b"".join(line for line in b.splitlines(True)
                             if not line.startswith(b"exported at,"))
            out[n] = b
    return out


def member_rows(zip_path, name):
    with zipfile.ZipFile(zip_path) as z:
        text = z.read(name).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def manifest(zip_path):
    return {r["key"]: r["value"] for r in member_rows(zip_path, "manifest.csv")}


# -- Nova -----------------------------------------------------------------

def nova_env(root, cc, **extra):
    """A copy of Phase1/Nova under root, with its own settings.r."""
    root = Path(root)
    nova = root / "nova"
    nova.mkdir(parents=True, exist_ok=True)
    for f in NOVA.iterdir():
        if f.suffix in (".r", ".cmd") and f.name != "settings.r":
            shutil.copy2(f, nova / f.name)
    shutil.copytree(NOVA / "config", nova / "config", dirs_exist_ok=True)
    env = SimpleNamespace(root=root, dir=nova, hist=root / "Historical",
                          ntd=root / "NoTradingDay", logs=root / "logs",
                          luld={k: root / "luld" / k / "limitUpDown.csv"
                                for k in ("temp", "test", "pilot", "prod")},
                          td=root / "td" / "TradingData.csv")
    for d in (env.hist, env.ntd, env.logs, env.td.parent,
              *[p.parent for p in env.luld.values()]):
        d.mkdir(parents=True, exist_ok=True)
    s = {"CROSSCODE_PATH": Path(cc).as_posix(),
         "OUTPUT_DIR": env.hist.as_posix(),
         "NOTRADINGDAY_DIR": env.ntd.as_posix(),
         "LOG_DIR": env.logs.as_posix(),
         "KDB_TIMEZONE": "China Standard Time",
         "LULD_OUT_TEMP": env.luld["temp"].as_posix(),
         "LULD_OUT_TEST": env.luld["test"].as_posix(),
         "LULD_OUT_PILOT": env.luld["pilot"].as_posix(),
         "LULD_OUT_PROD": env.luld["prod"].as_posix(),
         "INDIA_NSE_STRA": "", "INDIA_BSE_STRA": "",
         "TD_OUTPUT_PATH": env.td.as_posix(),
         "MSCI_MAPPING_PATH": "", "OPEN_AUCTION_OVERRIDE_PATH": "",
         "HKEX_CAS_LIST_PATH": "", "INDIA_NSE_CAS_LIST_PATH": "",
         "INDIA_BSE_CAS_LIST_PATH": "", "WORKERS": "1"}
    s.update({k: str(v) for k, v in extra.items()})
    (nova / "settings.r").write_text(
        "".join(f'{k} <- "{v}"\n' for k, v in s.items()), encoding="utf-8")
    return env


def rjob(env, script, *args):
    t0 = time.monotonic()
    p = subprocess.run([RSCRIPT, str(env.dir / script), *map(str, args)],
                       capture_output=True)
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace")
    return SimpleNamespace(code=p.returncode, out=out,
                           secs=time.monotonic() - t0)


def ran(name, r):
    return check(f"{name} exits 0", r.code == 0,
                 f"exit {r.code}; last lines:\n" +
                 "\n".join(r.out.splitlines()[-15:]))


def tree(d):
    d = Path(d)
    return {p.relative_to(d).as_posix(): p.read_bytes()
            for p in sorted(d.rglob("*")) if p.is_file()}


def tick_lines(b):
    rows = list(csv.reader(io.StringIO(b.decode("utf-8"))))
    return rows[0], rows[1:]


def hist_file(code):
    return f"{code}/raw-{code}-{YMD}.csv"


def grouped(lines):
    """(time, last, cond, ex, mic) -> summed volume."""
    out = collections.Counter()
    for t, last, vol, cond, ex, mic in lines:
        out[(t, last, cond, ex, mic)] += int(vol)
    return out


def secs_of(t):
    h, m, s = map(int, t.split(":"))
    return h * 3600 + m * 60 + s


def first(pattern, text, cast=str):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None


# -- scenario 1 -------------------------------------------------------------

def scenario1(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    section("1. condensed vs raw")
    day = Day(small_names(), extra=[NOMAS])
    cc = day.write_crosscode(tmp / "CrossCode.csv")
    kA, kB = FakeKdb(day), FakeKdb(day)
    zA, _ = ab_build(tmp / "abA", cc, kA)
    with uncondensed():
        zB, logB = ab_build(tmp / "abB", cc, kB)
    check("AB wrote both zips", zA and zB and zA.exists() and zB.exists())
    check("zip A was read with the condensed query only",
          {k for k, _, _ in kA.asked} == {"condensed"})
    check("zip B was read with the uncondensed query only",
          {k for k, _, _ in kB.asked} == {"raw"}
          and any("ticks NOT condensed" in l for l in logB.lines))
    mA, mB = manifest(zA), manifest(zB)
    raw_n = sum(len(v) for s, v in day.raw.items() if s != NOMAS_SYM)
    check(f"B's prints are the raw prints ({mB['prints']} = {raw_n})",
          int(mB["prints"]) == raw_n)
    check(f"A's prints are fewer, condensed ({mA['prints']} < {raw_n})",
          int(mA["prints"]) < raw_n)
    check("closes.csv is the same in both zips",
          members(zA)["closes.csv"] == members(zB)["closes.csv"])
    check("quote_only.csv is the same in both zips",
          members(zA)["quote_only.csv"] == members(zB)["quote_only.csv"])

    eA, eB = nova_env(tmp / "novaA", cc), nova_env(tmp / "novaB", cc)
    for env, z, tag in ((eA, zA, "A"), (eB, zB, "B")):
        for job in ("historical.r", "limit_up_down.r", "trading_data.r"):
            ran(f"{tag}: {job}", rjob(env, job, z))
    tA, tB = tree(eA.hist), tree(eB.hist)
    check("the same tick files in A and B", sorted(tA) == sorted(tB),
          f"A only {sorted(set(tA) - set(tB))}, B only "
          f"{sorted(set(tB) - set(tA))}")
    bad_group, bad_vol, bad_order, bad_head, bad_truth = [], [], [], [], []
    by_code = {n.code: n for n in day.names}
    for f in sorted(set(tA) & set(tB)):
        hA, lA = tick_lines(tA[f])
        hB, lB = tick_lines(tB[f])
        if hA != hB:
            bad_head.append(f)
        gA, gB = grouped(lA), grouped(lB)
        if len(gA) != len(lA) or gA != gB:
            bad_group.append(f)
        if sum(int(l[2]) for l in lA) != sum(int(l[2]) for l in lB):
            bad_vol.append(f)
        times = [secs_of(l[0]) for l in lA if l[0]]
        if times != sorted(times):
            bad_order.append(f)
        n = by_code.get(f.split("/")[0])
        if n and n.kind == "trade":
            raw = day.raw[n.sym]
            if (len(lB) != len(raw) or sum(int(l[2]) for l in lB)
                    != sum(r["size"] for r in raw)):
                bad_truth.append(f)
    n_files = len(set(tA) & set(tB))
    check(f"every tick file's lines in A are the (second, price, cond, ex) "
          f"sums of B's ({n_files} files)", not bad_group, str(bad_group))
    check("total volume per name is equal", not bad_vol, str(bad_vol))
    check("B's lines per name are the raw prints, volume and count",
          not bad_truth, str(bad_truth))
    check("A's lines run in time order", not bad_order, str(bad_order))
    check("headers equal", not bad_head, str(bad_head))
    qo = hist_file("8888 HK")
    check("the quote-only file is written and equal",
          qo in tA and tA.get(qo) == tB.get(qo))
    check("NoTradingDay output is equal, with 8889 HK",
          tree(eA.ntd) == tree(eB.ntd)
          and b"8889 HK" in tree(eA.ntd).get("NoTradingDay Hong Kong.csv",
                                            b""))
    lA_, lB_ = eA.luld["temp"], eB.luld["temp"]
    luld_rows = (len(lA_.read_bytes().splitlines()) - 1
                 if lA_.exists() else 0)
    check(f"limitUpDown.csv byte-identical ({luld_rows} rows)",
          lA_.exists() and lB_.exists()
          and lA_.read_bytes() == lB_.read_bytes() and luld_rows > 0)
    td_rows = (len(eA.td.read_bytes().splitlines()) - 1
               if eA.td.exists() else 0)
    check(f"TradingData.csv byte-identical ({td_rows} rows)",
          eA.td.exists() and eB.td.exists()
          and eA.td.read_bytes() == eB.td.read_bytes() and td_rows > 0)
    #  The close per name, as the zip says, against the raw day.
    closes = {r["sym"]: r for r in member_rows(zA, "closes.csv")}
    want = {}
    for n in day.names:
        if n.kind == "trade" and n.sym:
            codes = SESSIONS[n.ext][2]
            want[n.sym] = str([r for r in day.raw[n.sym]
                               if r["cond"] == codes][-1]["price"])
    check("every traded name's close is its last close-code print",
          all(closes.get(s, {}).get("close") == p for s, p in want.items()),
          str({s: (closes.get(s, {}).get("close"), p)
               for s, p in want.items()}))

    section("1b. probes")
    check("an NZE-MAIN name with no equity_master row is asked of kdb, as "
          "Nova resolves it (AB config/markets.csv knows NZE-MAIN)",
          NOMAS_SYM in closes,
          "AB never asked for NOMAS.NZ: marketcfg.composite('NZE-MAIN') is "
          "'' on AB, while Nova's hist_markets.csv maps it to NZ")
    flip_probe(tmp / "flip")
    return SimpleNamespace(day=day, cc=cc, zA=zA, zB=zB, eA=eA, tA=tA)


def flip_probe(tmp):
    """A quote-only name's Exchange letter is the commonest ex among the
    market's LINES. Condensing can change which that is."""
    hk = [n for n in small_names() if n.bbg in ("700 HK", "8888 HK")]
    day = Day(hk)
    rows = []
    for i in range(10):         # ten H prints in one second: one line
        rows.append({"sym": "700.HK", T: ms_time(36000, i * 10),
                     "price": Decimal("385.2"), "size": 100, "cond": "",
                     "ex": "H"})
    for i in range(4):          # four X prints, four seconds: four lines
        rows.append({"sym": "700.HK", T: ms_time(37000 + i, 0),
                     "price": Decimal("385.4"),
                     "size": 100, "cond": "CA" if i == 3 else "",
                     "ex": "X"})
    day.raw["700.HK"] = rows
    tmp.mkdir(parents=True, exist_ok=True)
    cc = day.write_crosscode(tmp / "CrossCode.csv")
    zA, _ = ab_build(tmp / "abA", cc, FakeKdb(day))
    with uncondensed():
        zB, _ = ab_build(tmp / "abB", cc, FakeKdb(day))
    got = {}
    for tag, z in (("A", zA), ("B", zB)):
        env = nova_env(tmp / f"nova{tag}", cc)
        rjob(env, "historical.r", z)
        f = env.hist / hist_file("8888 HK")
        got[tag] = (tick_lines(f.read_bytes())[1][0][4] if f.exists()
                    else None)
    check("the quote-only Exchange letter survives condensing (10 H prints "
          "in one second vs 4 X prints)", got["A"] == got["B"],
          f"condensed gives {got['A']!r}, raw gives {got['B']!r}")


# -- scenario 2 -------------------------------------------------------------

def scenario2(tmp, s1):
    tmp.mkdir(parents=True, exist_ok=True)
    import numpy as np
    import pandas as pd
    section("2. time representations")
    day = s1.day
    target = ("6758.JP", 1)
    variants = [
        ("int seconds", dict(time_rep=lambda s: s)),
        ("datetime.timedelta", dict(time_rep=lambda s:
                                    dt.timedelta(seconds=s))),
        ("pandas.Timedelta", dict(time_rep=lambda s: pd.Timedelta(seconds=s))),
        ("numpy timedelta64[s] values", dict(time_rep=lambda s:
                                             np.timedelta64(s, "s"))),
        ("a frame, timedelta64[s] column", dict(frame=True)),
        ("null (None) on one line", dict(time_rep=lambda s: s,
                                         null_at=target)),
        ("null (NaT) in a frame", dict(frame=True, null_at=target)),
    ]
    base = None
    for i, (label, kw) in enumerate(variants):
        root = tmp / f"v{i}"
        try:
            z, _ = ab_build(root / "ab", s1.cc, FakeKdb(day, **kw))
        except Exception as e:                              # noqa: BLE001
            check(f"{label}: AB builds", False, f"{type(e).__name__}: {e}")
            continue
        env = nova_env(root / "nova", s1.cc)
        r = rjob(env, "historical.r", z)
        if not ran(f"{label}: historical.r", r):
            continue
        t = tree(env.hist)
        if base is None:
            base = t
        if "null" not in label:
            check(f"{label}: the same tick files as the first variant",
                  t == base,
                  f"differ: {[k for k in set(t) | set(base) if t.get(k) != base.get(k)]}")
            continue
        f = hist_file("6758 JP")
        diff = [k for k in set(t) | set(base)
                if t.get(k) != base.get(k) and k != f]
        _, lines = tick_lines(t[f]) if f in t else (None, [])
        _, want = tick_lines(base[f])
        blank = [j for j, l in enumerate(lines) if l[0] == ""]
        same_rest = (len(lines) == len(want) and all(
            a[1:] == b[1:] and (j == target[1] or a == b)
            for j, (a, b) in enumerate(zip(lines, want))))
        ab_line = [l for l in members(z)["ticks.csv"].decode().splitlines()
                   if l.startswith("6758.JP,,")]
        check(f"{label}: a blank time in ticks.csv and on that line of the "
              f"tick file, the file's other lines unchanged",
              blank == [target[1]] and same_rest and len(ab_line) == 1,
              f"blank lines {blank}; rest same {same_rest}; zip lines "
              f"{ab_line}")
        check(f"{label}: every other tick file unchanged (the lines after "
              f"the blank keep their own name's clock)", not diff,
              f"differ: {diff}")


# -- scenario 3 -------------------------------------------------------------

def scenario3(tmp, s1):
    tmp.mkdir(parents=True, exist_ok=True)
    section("3. workflow")
    day, cc = s1.day, s1.cc
    #  AB, interrupted in its second market, then run again.
    mkts = sorted({n.prim for n in day.names if n.sym})
    second = mkts[1]
    fail = {n.sym for n in day.names if n.prim == second and n.sym}
    export = tmp / "abR"
    try:
        ab_build(export, cc, FakeKdb(day, fail_syms=fail))
        check(f"AB stops in market {second}", False, "it did not raise")
    except extract.ExtractError as e:
        check(f"AB stops in market {second}: {str(e)[:70]}",
              f"market {second}" in str(e))
    stage = export / f"phase1-{YMD}"
    check(f"{mkts[0]} is staged, {second} is not, no zip",
          (stage / f"ticks-{mkts[0]}.csv").exists()
          and not (stage / f"ticks-{second}.csv").exists()
          and not (export / extract.bundle_name(DAY)).exists())
    k = FakeKdb(day)
    z, log = ab_build(export, cc, k)
    first_syms = {n.sym for n in day.names if n.prim == mkts[0] and n.sym}
    check(f"the rerun skips {mkts[0]} and reads the rest",
          not first_syms & set(k.tick_syms())
          and any(f"{mkts[0]} done already, skipped" in l for l in log.lines))
    check("the resumed zip is the uninterrupted one (bar 'exported at')",
          z and members(z) == members(s1.zA))

    #  AB --market on a finished day.
    k = FakeKdb(day)
    z2, _ = ab_build(export, cc, k, only=["NZ"])
    nz = {n.sym for n in day.names if n.prim == "NZ" and n.sym}
    check("AB --market NZ reads NZ only", set(k.tick_syms()) == nz,
          str(sorted(set(k.tick_syms()))))
    check("AB --market NZ rebuilds the same zip",
          z2 and members(z2) == members(s1.zA))

    #  Nova, end to end with run_phase1.cmd.
    env = nova_env(tmp / "novaC", cc)
    cmd = env.dir / "run_phase1.cmd"
    text = cmd.read_text(encoding="utf-8", errors="replace")
    win_r = str(Path(RSCRIPT))
    cmd.write_text(re.sub(r'^set "RSCRIPT=[^\r\n]*"', lambda m:
                          f'set "RSCRIPT={win_r}"', text, flags=re.M),
                   encoding="utf-8", newline="")
    line = f'cmd /s /c "echo.| "{cmd}" "{s1.zA}""'
    p = subprocess.run(line, capture_output=True)
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace")
    check("run_phase1.cmd exits 0 and says all three jobs done",
          p.returncode == 0 and "all three jobs done" in out,
          f"exit {p.returncode}:\n" + "\n".join(out.splitlines()[-15:]))
    check("run_phase1.cmd: tick files as historical.r alone wrote them",
          tree(env.hist) == s1.tA)
    luld = [env.luld[k] for k in ("temp", "test", "pilot", "prod")]
    check("run_phase1.cmd: limitUpDown.csv in all three environments, "
          "TradingData.csv, as in scenario 1",
          all(f.exists() for f in luld)
          and len({f.read_bytes() for f in luld}) == 1
          and luld[0].read_bytes() == s1.eA.luld["temp"].read_bytes()
          and env.td.exists()
          and env.td.read_bytes() == s1.eA.td.read_bytes())

    #  A second full historical.r run: nothing written, all skipped.
    before = {p: (p.stat().st_mtime_ns, p.read_bytes())
              for p in list(env.hist.rglob("*")) + list(env.ntd.rglob("*"))
              if p.is_file()}
    r = rjob(env, "historical.r", s1.zA)
    ran("second historical.r", r)
    written = [int(x) for x in re.findall(r"files written\s+(\d+)", r.out)]
    skipped = sum(int(x) for x in
                  re.findall(r"(\d+) files already there, skipped", r.out))
    after = {p: (p.stat().st_mtime_ns, p.read_bytes())
             for p in list(env.hist.rglob("*")) + list(env.ntd.rglob("*"))
             if p.is_file()}
    n_files = len(s1.tA)
    check(f"second run: 0 files written, all {n_files} skipped, "
          f"nothing on disk touched",
          written and not any(written) and skipped == n_files
          and before == after,
          f"written {written}, skipped {skipped}, unchanged "
          f"{before == after}")

    #  historical.r --market=NZE-MAIN, into a fresh folder.
    env = nova_env(tmp / "novaD", cc)
    r = rjob(env, "historical.r", s1.zA, "--market=NZE-MAIN")
    ran("historical.r --market=NZE-MAIN", r)
    t = tree(env.hist)
    nz_files = {k: v for k, v in s1.tA.items()
                if k.split("/")[0].endswith(" NZ")}
    check(f"--market=NZE-MAIN writes NZ only ({sorted(t)})",
          t == nz_files and t)
    ntd = tree(env.ntd)
    check("--market=NZE-MAIN writes no NoTradingDay row elsewhere",
          all(k == "NoTradingDay New Zealand.csv" for k in ntd))


# -- scenario 4: --big --------------------------------------------------------

BIG_MKTS = [("JT", "TYO-MAIN", "JP", "XTKS"), ("KP", "KSC-MAIN", "KS", "XKRX"),
            ("HK", "HKG-MAIN", "HK", "XHKG"), ("NZ", "NZE-MAIN", "NZ", "XNZE"),
            ("IS", "NSI-MAIN", "IS", "XNSE")]


class BigKdb:
    """~3,000,000 raw prints over ~15,000 syms, made a chunk at a time and
    condensed with pandas, as frames - so the day is never held whole."""

    def __init__(self, per_market=3000, mean=200):
        import numpy as np
        self.np = np
        self.mean = mean
        self.names = []
        rng = np.random.default_rng(7)
        for ext, market, comp, mic in BIG_MKTS:
            for i in range(per_market):
                ticker = f"B{i:05d}"
                u = rng.random()
                kind = "none" if u < 0.02 else "quote" if u < 0.03 else "trade"
                base = float(rng.choice([rng.integers(1, 50) / 10,
                                         rng.integers(10, 5000)]))
                tick = 0.01 if base < 10 else 1.0
                self.names.append(SimpleNamespace(
                    bbg=f"{ticker} {ext}", ticker=ticker, ext=ext,
                    market=market, sym=f"{ticker}.{comp}", comp=comp,
                    mic=mic, kind=kind, base=base, tick=tick))
        self.by_sym = {n.sym: n for n in self.names}
        self.master = [{"BloombergCode": n.bbg, "sym": n.sym,
                        "EQY_PRIM_EXCH_SHRT": n.ext,
                        "COMPOSITE_EXCH_CODE": n.comp,
                        "ID_MIC_PRIM_EXCH": n.mic,
                        "sym_bpipe": crosscode.bbg_dotted(n.bbg),
                        "sym_mbpipe": crosscode.bbg_full(n.bbg)}
                       for n in self.names]
        self.raw_rows = 0
        self.shared = 0
        self.gen_secs = 0.0

    def write_crosscode(self, path):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(CC_HEADER)
            for n in self.names:
                w.writerow([f"{n.ticker}.{n.market[:3]}", f"{n.ticker}.X",
                            "Equity", n.bbg, "Common Stock", n.market,
                            SESSIONS[n.ext][4], "ACTV", n.ticker, "", "",
                            ""])
        return path

    def em(self, q, *args):
        if q in (qattsource.MAXDATE_CLIENT_Q, qattsource.MAXDATE_SERVER_Q):
            return DAY
        s = set(args[-1]) if args else set()
        for query, col in ((qattsource.MASTER_BPIPE_Q, "sym_bpipe"),
                           (qattsource.MASTER_MBPIPE_Q, "sym_mbpipe"),
                           (qattsource.MASTER_SYM_Q, "sym")):
            if q == query:
                return [r for r in self.master if r[col] in s]
        if q == refdata.EQUITY_Q:
            return [{"sym": x, "PX_LAST": str(self.by_sym[x].base),
                     "EQY_BETA": "1", "volatility": "0.2",
                     "REL_INDEX": "IDX", "CUR_MKT_CAP": "1000",
                     "fx_last": "0.1", "ID_ISIN": "XX0000000000",
                     "INDUSTRY_SECTOR": "Industrial",
                     "LONG_COMP_NAME": "FIXTURE"}
                    for x in s if x in self.by_sym]
        if q == refdata.IDS_Q:
            return []
        if q == refdata.TBL_Q:
            return []
        raise AssertionError(f"equity_master was asked {q}")

    def _one(self, n):
        np = self.np
        rng = np.random.default_rng(zlib.crc32(n.sym.encode()))
        start, end, close, exs, _ = SESSIONS[n.ext]
        want = int(rng.gamma(2.0, self.mean / 2)) + 5
        k = max(2, int(want / 1.5))
        mult = np.where(rng.random(k) < 0.75, 1, rng.integers(2, 5, k))
        secs = np.sort(rng.integers(start, end, k))
        dup = rng.random(k) < 0.1
        dup[0] = False
        secs = np.where(dup, np.roll(secs, 1), secs)
        prices = np.round(np.maximum(
            n.base + n.tick * rng.integers(-20, 21, k), n.tick), 4)
        cond = np.where(rng.random(k) < 0.85, "", "X").astype(object)
        secs[-1] = end
        cond[-1] = close
        rep = np.repeat(np.arange(k), mult)
        m = len(rep)
        ms = np.sort(rng.integers(0, 1000, m))
        return {"sym": np.full(m, n.sym, dtype=object),
                "t": secs[rep] * 1000 + ms, "price": prices[rep],
                "size": rng.integers(1, 50, m) * 100,
                "cond": cond[rep], "ex": np.full(m, exs[0], dtype=object)}

    def qatt(self, q, *args):
        import pandas as pd
        np = self.np
        if q == extract.TABLES_Q:
            return ["qatt", "quote"]
        if q == qattsource.PARTITIONS_Q:
            return [last_weekday(DAY), DAY]
        if q == qattsource.COLUMNS_Q:
            return list(COLS)
        assert CONDENSED_MARK in q, q
        t0 = time.monotonic()
        parts = [self._one(self.by_sym[s]) for s in args[-1]
                 if self.by_sym[s].kind == "trade"]
        if not parts:
            return FakeTable(pd.DataFrame({c: [] for c in
                                           ("sym", T, "price", "cond", "ex",
                                            "size")}))
        raw = pd.DataFrame({c: np.concatenate([p[c] for p in parts])
                            for c in parts[0]})
        raw["sec"] = raw["t"] // 1000
        self.raw_rows += len(raw)
        keys = ["sym", "sec", "price", "cond", "ex"]
        self.shared += int(raw.duplicated(keys, keep=False).sum())
        g = raw.groupby(keys, sort=True, as_index=False)["size"].sum()
        g[T] = pd.to_timedelta(g["sec"], unit="s").astype("timedelta64[s]")
        g = g[["sym", T, "price", "cond", "ex", "size"]]
        self.gen_secs += time.monotonic() - t0
        return FakeTable(g)

    def quote(self, q, *args):
        if q == extract.TABLES_Q:
            return ["quote"]
        return [{"sym": s, "time": dt.time(15, 0, 0), "bid": Decimal("1.0"),
                 "ask": Decimal("1.2")}
                for s in args[-1] if self.by_sym[s].kind == "quote"]

    def conns(self):
        return {"em": self.em, "qatt": self.qatt, "quote": self.quote,
                "reconnect": lambda: self.qatt}


def scenario4(tmp):
    tmp.mkdir(parents=True, exist_ok=True)
    section("4. --big")
    big = BigKdb()
    cc = big.write_crosscode(tmp / "CrossCode.csv")
    t0 = time.monotonic()
    z, _ = ab_build(tmp / "ab", cc, big,
                    chunk=settings.DEFAULTS["SYM_CHUNK"])
    ab_secs = time.monotonic() - t0
    m = manifest(z)
    nums = {
        "syms": len(big.names),
        "raw prints": big.raw_rows,
        "condensed lines (manifest prints)": int(m["prints"]),
        "condensed / raw": round(int(m["prints"]) / big.raw_rows, 3),
        "raw prints sharing second/price/cond/ex with another":
            round(big.shared / big.raw_rows, 3),
        "AB build seconds": round(ab_secs, 1),
        "  of which fake kdb (generating + condensing)":
            round(big.gen_secs, 1),
        "zip MB": round(z.stat().st_size / 1e6, 1),
    }
    for w in (1, 4):
        env = nova_env(tmp / f"nova{w}", cc, WORKERS=w)
        r = rjob(env, "historical.r", z)
        ran(f"big historical.r WORKERS={w}", r)
        nums[f"historical.r WORKERS={w}: wall seconds"] = round(r.secs, 1)
        nums[f"historical.r WORKERS={w}: step 2 read+parse s"] = first(
            r"read and parse\s+([\d.]+) s", r.out, float)
        nums[f"historical.r WORKERS={w}: step 2 write s"] = first(
            r"\bwrite\s+([\d.]+) s", r.out, float)
        nums[f"historical.r WORKERS={w}: tick files written"] = first(
            r"files written\s+(\d+)\s+\d+ prints", r.out, int)
        nums[f"historical.r WORKERS={w}: quote-only files"] = first(
            r"quote-only files\s+(\d+)", r.out, int)
    for k, v in nums.items():
        print(f"  {k:<58}{v}")
    return nums


# -- main -------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--big", action="store_true")
    p.add_argument("--keep", action="store_true")
    a = p.parse_args(argv)
    if not Path(RSCRIPT).exists():
        print(f"FAIL  no Rscript at {RSCRIPT}; set P1_RSCRIPT")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="p1int-"))
    print(f"Phase1 integration, trade day {DAY} (today {TODAY}), in {tmp}")
    try:
        s1 = scenario1(tmp / "s1")
        scenario2(tmp / "s2", s1)
        scenario3(tmp / "s3", s1)
        if a.big:
            scenario4(tmp / "s4")
    finally:
        if a.keep:
            print(f"\nkept {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, g, _ in RESULTS if not g]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    print("all checks passed" if not failed else "SOME CHECKS FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
