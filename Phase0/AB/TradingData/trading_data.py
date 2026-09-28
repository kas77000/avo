#!/usr/bin/env python3
"""Build TradingData.csv from the crosscode and kdb's equity_master.

The crosscode is the security master and drives the row set.  equity_master
supplies the reference data the R job got from Bloomberg.  Every other input
is optional: present means used, absent means those columns go blank and the
run says so.

WHAT IS NOT FILLED, AND WHY

  MsciCountryIndex, MsciSectorCountryIndex, MsciSectorIndex,
  MsciSectorRegionIndex   need msci_mapping.csv
  OpenAggressivityPct     fills when the auction override CSV is supplied
  Segment NO_CAS for HKG   fills when the HKEX dico list is supplied
  Segment CAS for NSI/BSE  fills when the two India lists are supplied
  Segment CAS for HK ETFs  needs TRADING_CONDITIONS_1, and cannot be had

  Segment for HK ETFs is the one genuinely unavailable field.  It comes from
  TRADING_CONDITIONS_1 via an intraday Bloomberg call at :357 and has no
  equivalent in equity_master.  qatt.cond was considered and ruled out.

THREE SOURCES ARE UNVERIFIED and print a banner rather than being trusted
silently:

  Volatility10D   equity_master.volatility may not be the 10-day figure
                  Bloomberg's VOLATILITY_10D returns.  It is a fraction and
                  is written out times 100, as a percentage.
  MarketCap/Capi  assumes fx_last is a local->USD rate matching load_FXdatas.
                  CUR_MKT_CAP is taken to be in millions and scaled up.
  Sector          equity_master has no GICS_SECTOR_NAME, so every row takes
                  the :295 fallback and differs from the R wherever GICS had
                  a value

TWO R BUGS ARE PRESERVED VERBATIM and reported.  The R job is the reference,
and a port that quietly diverges is worse than one that diverges loudly.

  :113   `if (length(idx) == 0)` gates the Bloomberg top-up on the set of
         rows that need it being EMPTY, so it has never run.  Moot here -
         there is no second call to gate - but the count of rows that would
         have entered it is reported, because that is the number that says
         whether fixing it in R would change anything.
  :599   SubscribeFeedAtStartup is set to F for everything and then to F
         again for India.  The commented-out original at :562 used T.  The
         column is therefore always FALSE.

    python trading_data.py --self-test
    python trading_data.py --demo
    python trading_data.py --compare OLD.csv

--compare prints the per-column summary AND writes every difference to
compare-report.csv (--report moves it).  The printed form caps at five
examples a column, which is right for reading and wrong for the decision it
supports: Volatility10D's spread cannot be measured from a sample.  The CSV
is uncapped.

    status,code,column,old,new
    only_in_old,005930.KP,,,
    differs,BHP.AU,Volatility10D,18.40,19.75

It also writes close-deviation.csv beside that report: how far our Close
sits from the old file's, (new - old) / old in percent, bucketed.  Names
with no close on one side are counted on their own lines, not as a
deviation.  The printed form adds the mean, median, p5, p95, min and max,
and close-deviation.html draws it, to show whether the deviation is a
Gaussian centred on zero: the non-zero deviations in equal bins symmetric
about zero, a normal fit over them, and skew and excess kurtosis (both 0 for
a Gaussian).  One file that opens in a browser.

    bucket,count,share_pct
    0%,4210,93.10
    0% to 0.1%,160,3.54

`code` is the #FidessaCode.  LimitUpDown's report names rows by
BloombergCode instead, because ITS output carries one and this job's does
not: the twenty columns start at #FidessaCode and never mention a Bloomberg
code, so putting one here would mean joining the crosscode into a
comparison that is otherwise two output files and nothing else.

NOTE THAT --compare RUNS THE JOB FIRST, and a run copies to OUTPUT_PATH.
Comparing publishes.  Point OUTPUT_PATH somewhere harmless for a diff that
should not.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import os
import shutil
import sys
import time
from pathlib import Path

import auction
import caslist
import columns
import crosscode
import equitymaster
import marketcfg
import msci

OUTPUT_COLUMNS = [
    "#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex",
    "MsciCountryIndex", "MsciSectorCountryIndex", "MsciSectorIndex",
    "MsciSectorRegionIndex", "Segment", "Beta", "Close", "Volatility10D",
    "NoShortSell", "RespectShortSellPrice", "OpenAggressivityPct",
    "MarketCap", "ISIN", "SubscribeFeedAtStartup"]

# The columns the six-field brief names.  Their fill rates are reported.
KEY_COLUMNS = ("Close", "Beta", "Volatility10D", "Index", "MarketCap")

# Where --compare writes its full list of differences.  What it prints is a
# summary capped at five examples a column; this is the record.
COMPARE_REPORT = "compare-report.csv"
COMPARE_COLUMNS = ["status", "code", "column", "old", "new"]

# --compare also writes how far our Close sits from the old file's, as a
# distribution, to this name beside the report.  The deviation is RELATIVE,
# (new - old) / old in percent: the universe spans currencies, so an
# absolute gap of 5 is noise on a won price and a disaster on a dollar one.
CLOSE_REPORT = "close-deviation.csv"
CLOSE_COLUMNS = ["bucket", "count", "share_pct"]
# Upper edges, in percent, of the signed buckets either side of zero.  Zero
# itself is its own bucket: an identical close is the answer the cutover
# wants, and it should not hide inside "under 0.1%".
CLOSE_EDGES = ("0.1", "1", "5", "10")

# equity_master stores volatility as a fraction; Volatility10D is a
# percentage.
VOL_SCALE = 100

# equity_master stores CUR_MKT_CAP in millions.  The R job's Capi thresholds
# at :165-168 are 300000000 / 2000000000 / 10000000000 in raw units, so the
# MarketCap column is raw units too and the stored figure has to be scaled
# up before either the column or the bucket is right.
CAP_SCALE = 1000000

# The fields the R job's dead :120 top-up would have refetched.
TOPUP_FIELDS = ("CUR_MKT_CAP", "EQY_BETA", "volatility", "INDUSTRY_SECTOR")

_D = equitymaster._to_decimal
_T = equitymaster._text


def _plain(d) -> str:
    """A Decimal as the R job would print it: decimal notation, no exponent,
    no trailing zeros.

    normalize() strips the zeros but pushes large values into exponent form
    (1.365E+11), so format(.., 'f') puts them back.  Without this every
    MarketCap would carry the scale of CUR_MKT_CAP times fx_last - the demo
    printed 136500000000.000 - and --compare would report a difference on
    every single row against an R file that writes 136500000000."""
    if d is None:
        return ""
    return format(d.normalize(), "f")


def _measure(value, scale=1) -> str:
    """Beta, Close and Volatility10D: no value writes nothing, and zero is
    no value.

    equity_master carries 0 where Bloomberg had nothing, which is why the R
    job counts `== 0` alongside is.na and "" at :109-110 when it decides a
    row is missing its reference data.  A zero close, a zero beta, a zero
    volatility - none of those are measurements, so they go out blank rather
    than as "0" and let the fill rates say how much is really there.

    `scale` is Volatility10D's percentage conversion.  It multiplies a value
    that is there and leaves a value that is not alone: no value stays no
    value, rather than becoming a scaled zero."""
    d = _D(value)
    if d is None or d == 0:
        return ""
    return _plain(d * scale)


def build_rows(rows, master, markets, mapping, sym_hits, cas=None,
               hkex=None, override=None) -> list:
    """One output dict per crosscode row.  Missing reference data leaves a
    column blank; it never becomes zero, and a zero that came back from kdb
    is treated as missing for Beta, Close and Volatility10D."""
    staged = []
    for row in rows:
        rec = None
        for cand in equitymaster.sym_candidates(row, markets):
            if cand in master:
                rec = master[cand]
                sym_hits[row.fidessa_code] = cand
                break
        rec = rec or {}

        cap = _D(rec.get("CUR_MKT_CAP"))
        fx = _D(rec.get("fx_last"))
        market_cap = (cap * CAP_SCALE * fx
                      if cap is not None and fx is not None else None)

        rel_index = _T(rec.get("REL_INDEX"))
        #  equity_master carries Bloomberg's "N.A." verbatim; it is the
        #  absence of a sector, not the name of one, so it never reaches
        #  the Sector column or the msci lookup.
        industry = columns.present(_T(rec.get("INDUSTRY_SECTOR")))

        isin = _T(rec.get("ID_ISIN"))

        seg = columns.segment_cn(row.market, row.sec_type)
        if seg is None and row.market == "ASX-MAIN":
            seg = columns.segment_asx(row.ticker, row.sec_type)
        if seg is None:
            seg = columns.SEGMENT_DEFAULT
        #  :577 then :580-581, both AFTER the market rules and both
        #  overwriting them.  Hong Kong first, India second; they touch
        #  different markets, so the order is fidelity rather than effect.
        seg = caslist.segment_hkex(hkex, row.market, row.bbg,
                                   row.sec_type) or seg
        seg = caslist.segment(cas, row.market, isin) or seg

        out = {
            "#FidessaCode": row.fidessa_code,
            "Type": row.sec_type,
            "Sector": columns.sector("", industry),
            "Capi": columns.capi_bucket(market_cap),
            "Index": rel_index,
            "ICBIndex": "",            # filled by the propagation below
            "Segment": seg,
            "Beta": _measure(rec.get("EQY_BETA")),
            "Close": _measure(rec.get("PX_LAST")),
            #  equity_master holds volatility as a fraction, the CSV wants
            #  it as a percentage: 0.21 goes out as 21.
            "Volatility10D": _measure(rec.get("volatility"), VOL_SCALE),
            "NoShortSell": marketcfg.no_short_sell(row.market, markets),
            "RespectShortSellPrice": marketcfg.respect_short_sell(
                row.market, row.sec_type, row.is_reit, markets),
            #  :325 joins the override on RicCode and leaves every
            #  other row NA, which write.csv renders as nothing.
            "OpenAggressivityPct": (override or {}).get(row.ric, ""),
            "MarketCap": _plain(market_cap),
            "ISIN": isin,
            "SubscribeFeedAtStartup": "FALSE",    # :599, always FALSE
        }
        out.update(msci.resolve(mapping, row.market, "", industry))
        staged.append({"row": row, "icb_seed": rel_index, "out": out})

    # :606 sorts by FidessaMarket then RicCode, before the columns are cut
    # down - so the sort keys are not in the output at all.
    staged.sort(key=lambda s: (s["row"].market, s["row"].ric))

    icb = columns.propagate_icb([
        {"ric": s["row"].ric, "bbg": s["row"].bbg,
         "market": s["row"].market, "icb": s["icb_seed"]} for s in staged])
    for s, value in zip(staged, icb):
        s["out"]["ICBIndex"] = value

    return [s["out"] for s in staged]


def topup_candidates(rows, master, markets) -> int:
    """How many rows the R job's :120 top-up WOULD have refetched, if :113
    said `> 0` instead of `== 0`.  This is the number that says whether
    fixing that bug in R would change anything."""
    n = 0
    for row in rows:
        rec = {}
        for cand in equitymaster.sym_candidates(row, markets):
            if cand in master:
                rec = master[cand]
                break
        if any(_T(rec.get(f)) in ("", "0") for f in TOPUP_FIELDS):
            n += 1
    return n


def fx_seen(master, sym_hits) -> list:
    """(currency, rate, rows) for every CRNCY the matched rows carry.

    THIS IS WHAT SETTLES WHETHER fx_last CONVERTS TO USD OR TO EUR, and it
    needs no reference rates to read: a currency quoted against itself is 1
    and nothing else is, so the base is whichever row shows 1.

    It answers the direction too.  fx_last is used as a local->base rate, so
    a currency worth less than the base has to come back below 1: AUD at
    0.65 is local->USD, AUD at 1.54 is USD->local and the multiplication in
    build_rows is inverted."""
    seen = {}
    for sym in sym_hits.values():
        rec = master.get(sym, {})
        crncy = _T(rec.get("CRNCY"))
        rate = _D(rec.get("fx_last"))
        if not crncy or rate is None:
            continue
        key = (crncy, _plain(rate))
        seen[key] = seen.get(key, 0) + 1
    return sorted((c, r, n) for (c, r), n in seen.items())


def validate(out_rows) -> list:
    problems = []
    if not out_rows:
        problems.append("no rows to write")
        return problems
    if not any(r["Close"] for r in out_rows):
        problems.append("not one row has a Close")
    return problems


def write_csv(path, out_rows):
    """Matches R's write.csv(row.names=F, na="", quote=FALSE)."""
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS,
                           quoting=csv.QUOTE_NONE, escapechar="\\",
                           extrasaction="ignore")
        w.writeheader()
        for r in out_rows:
            w.writerow({c: r.get(c, "") for c in OUTPUT_COLUMNS})


def report(out_rows, rows, excluded, sym_hits, date_used, date_asked,
           mapping, markets, topup=None, master=None):
    print(f"\n  crosscode rows      {len(rows)}")
    for e in excluded:
        print(f"  excluded            {len(e.rows):6d}  {e.reason}")
    #  date_used comes back RAW from q, so render it rather than printing the
    #  pykx repr; comparison is on the rendered form for the same reason.
    used_text = equitymaster.date_text(date_used)
    print(f"  equity_master date  {used_text}"
          + ("" if used_text == str(date_asked)
             else f"  (asked {date_asked})"))
    print(f"  syms matched        {len(sym_hits)} / {len(rows)}")

    by_suffix = {}
    for sym in sym_hits.values():
        suffix = sym.rsplit(".", 1)[-1]
        by_suffix[suffix] = by_suffix.get(suffix, 0) + 1
    if by_suffix:
        print("  sym suffixes that hit: "
              + ", ".join(f"{k}={v}" for k, v in sorted(by_suffix.items())))

    unconfigured = sorted({r.market for r in rows
                           if r.market and r.market not in markets})
    if unconfigured:
        print("  markets with no row in config/markets.csv: "
              + ", ".join(unconfigured))

    print("\n  fill rates")
    n = len(out_rows) or 1
    for c in KEY_COLUMNS:
        filled = sum(1 for r in out_rows if r[c])
        print(f"    {c:<22} {filled:6d} / {len(out_rows)}  "
              f"{100 * filled // n:3d}%")

    fx = fx_seen(master or {}, sym_hits)
    if fx:
        print("\n  fx_last by currency - the base is whichever one is 1")
        for crncy, rate, n in fx[:25]:
            print(f"    {crncy:<6} {rate:<18} {n:6d} rows")
        base = sorted({c for c, r, _ in fx if _D(r) == 1})
        if base:
            print(f"    -> fx_last converts to {', '.join(base)}, so "
                  f"MarketCap is in {base[0]}")
        else:
            print("    -> nothing is quoted at 1, so the base is not in this "
                  "universe; read it off a rate above")
            print("       (a currency worth less than the base is BELOW 1; "
                  "above 1 means the rate is inverted)")

    if mapping is None:
        print("\n  ! msci_mapping.csv not supplied - the four Msci* columns "
              "are blank")

    if topup is not None:
        print(f"\n  the :113 bug: {topup} row(s) would have entered the dead "
              "top-up if it read `> 0`")

    print("\n  ! UNVERIFIED SOURCES - confirm before cutover")
    print("    Volatility10D  from equity_master.volatility x100; the "
          "definition is NOT confirmed to be Bloomberg's VOLATILITY_10D")
    print("    MarketCap/Capi CUR_MKT_CAP x1e6 * fx_last; the millions "
          "scale is read off the data, and fx_last's")
    print("                   direction is assumed to be local->USD")
    print("    Sector         equity_master has no GICS_SECTOR_NAME, so "
          "every row takes the :295 INDUSTRY_SECTOR fallback")


def compare(old_rows, new_rows) -> dict:
    """Per-column agreement against the R job's output.  This is the cutover
    instrument: it turns Volatility10D's unverified definition into a
    measured spread rather than an argument."""
    old = {r["#FidessaCode"]: r for r in old_rows}
    new = {r["#FidessaCode"]: r for r in new_rows}
    shared = sorted(set(old) & set(new))

    cols = {}
    for c in OUTPUT_COLUMNS:
        if c == "#FidessaCode":
            continue
        same = differ = 0
        examples = []
        for k in shared:
            a, b = old[k].get(c, ""), new[k].get(c, "")
            if a == b:
                same += 1
            else:
                differ += 1
                if len(examples) < 5:
                    examples.append((k, a, b))
        if same or differ:
            cols[c] = {"same": same, "differ": differ, "examples": examples}

    return {"shared": len(shared),
            "only_old": sorted(set(old) - set(new)),
            "only_new": sorted(set(new) - set(old)),
            "columns": cols}


def differences(old_rows, new_rows):
    """Every disagreement, one record each - NOT the five per column that
    print_compare shows.  The printed form is the summary; this is the
    record, and Volatility10D's spread is only measurable from all of it.

    Grouped by column rather than by name, matching the printed order, so
    the file sorts the way the question is asked: which column disagrees,
    and on how many names.

    A row is named by its #FidessaCode, which is the key the two files
    agree on and the first column of the output.  LimitUpDown's report names
    rows by BloombergCode instead, and the difference is not an oversight:
    ITS OUTPUT CARRIES ONE AND THIS JOB'S DOES NOT.  The twenty columns
    start at #FidessaCode and never mention a Bloomberg code, so putting one
    here would mean joining the crosscode into a comparison that is
    otherwise two output files and nothing else."""
    old = {r["#FidessaCode"]: r for r in old_rows}
    new = {r["#FidessaCode"]: r for r in new_rows}
    shared = sorted(set(old) & set(new))

    out = []
    for k in sorted(set(old) - set(new)):
        out.append({"status": "only_in_old", "code": k, "column": "",
                    "old": "", "new": ""})
    for k in sorted(set(new) - set(old)):
        out.append({"status": "only_in_new", "code": k, "column": "",
                    "old": "", "new": ""})

    for c in OUTPUT_COLUMNS:
        if c == "#FidessaCode":
            continue
        for k in shared:
            a, b = old[k].get(c, ""), new[k].get(c, "")
            if a != b:
                out.append({"status": "differs", "code": k, "column": c,
                            "old": a, "new": b})
    return out


def close_deviations(old_rows, new_rows) -> dict:
    """Our Close against the old file's, name by name, as percentages.

    Only names in both files count, and only where BOTH have a close: a
    blank on one side is not a deviation of any size, so it is counted
    under its own heading rather than folded in.  A zero in the old file is
    no close either - it is what the R job writes when Bloomberg had none,
    and dividing by it would say nothing."""
    old = {r["#FidessaCode"]: r for r in old_rows}
    new = {r["#FidessaCode"]: r for r in new_rows}
    devs, blank_old, blank_new = [], 0, 0
    for k in sorted(set(old) & set(new)):
        a, b = _D(old[k].get("Close")), _D(new[k].get("Close"))
        if a is None or a == 0:
            blank_old += 1
        elif b is None:
            blank_new += 1
        else:
            devs.append((b - a) / a * 100)
    return {"devs": devs, "blank_old": blank_old, "blank_new": blank_new}


def close_distribution(d) -> list:
    """(bucket, count) for every shared name: the signed buckets from most
    negative to most positive, then the two kinds of blank."""
    edges = [_D(e) for e in CLOSE_EDGES]
    spans = list(zip(("0",) + CLOSE_EDGES, CLOSE_EDGES))
    up = [f"{lo}% to {hi}%" for lo, hi in spans] + [f"> {CLOSE_EDGES[-1]}%"]
    down = ([f"< -{CLOSE_EDGES[-1]}%"]
            + [f"-{hi}% to {'-' if lo != '0' else ''}{lo}%"
               for lo, hi in reversed(spans)])
    labels = down + ["0%"] + up
    counts = [0] * len(labels)
    mid = len(edges) + 1                       # the "0%" bucket
    for v in d["devs"]:
        if v == 0:
            counts[mid] += 1
            continue
        i = sum(1 for e in edges if abs(v) > e)  # 0 .. len(edges)
        counts[mid + 1 + i if v > 0 else mid - 1 - i] += 1
    return (list(zip(labels, counts))
            + [("no close in old", d["blank_old"]),
               ("no close in new", d["blank_new"])])


def _pct(devs, q):
    """The q-th percentile of sorted devs, nearest rank."""
    return devs[min(len(devs) - 1, int(q * len(devs)))]


def write_close_report(path, dist) -> str:
    total = sum(n for _, n in dist) or 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CLOSE_COLUMNS)
        for label, n in dist:
            w.writerow([label, n, f"{100 * n / total:.2f}"])
    return str(path)


def close_stats(devs) -> list:
    """(name, value) for the summary both the screen and the chart show."""
    devs = sorted(devs)
    if not devs:
        return []
    return [("mean", sum(devs) / len(devs)), ("median", _pct(devs, .5)),
            ("p5", _pct(devs, .05)), ("p95", _pct(devs, .95)),
            ("min", devs[0]), ("max", devs[-1])]


def print_close(d, dist):
    stats = close_stats(d["devs"])
    print(f"\n  Close deviation, (new - old) / old, over {len(d['devs'])} "
          f"names with a close in both")
    if stats:
        print("    " + "   ".join(f"{k} {v:+.4f}%" for k, v in stats[:4]))
        print("    " + "   ".join(f"{k} {v:+.4f}%" for k, v in stats[4:]))
    shape = shape_stats([float(v) for v in d["devs"] if v != 0])
    if shape:
        print(f"    non-zero only ({shape['n']}): sd {shape['sd']:.4f}%   "
              f"skew {shape['skew']:+.2f}   "
              f"excess kurtosis {shape['kurtosis']:+.2f}   (a Gaussian is 0, 0)")
    most = max((n for _, n in dist), default=0) or 1
    for label, n in dist:
        print(f"    {label:<18} {n:6d}  {'#' * round(40 * n / most)}")


CLOSE_CHART = "close-deviation.html"

# The chart's histogram: equal-width bins, symmetric about zero, over the
# 1st to 99th percentile.  Equal widths are what make the SHAPE readable -
# the report's buckets are uneven on purpose and would hide it.
CHART_BINS = 40


def shape_stats(xs) -> dict:
    """How Gaussian a list of floats is.  A normal has skew 0 and excess
    kurtosis 0; kurtosis well above 0 means fat tails - most names agree
    closely and a few are far off.

    `centre` and `width` are the robust fit the chart draws: the median and
    1.4826 x the median absolute deviation, which is the sd for a normal.
    Mean and sd would let a handful of far-off names flatten the curve
    until it fits nothing."""
    n = len(xs)
    if n < 2:
        return {}
    mean = sum(xs) / n
    m2 = sum((x - mean) ** 2 for x in xs) / n
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    s = sorted(xs)
    med = s[n // 2]
    mad = sorted(abs(x - med) for x in xs)[n // 2]
    return {"n": n, "sd": m2 ** .5,
            "skew": m3 / m2 ** 1.5 if m2 else 0.0,
            "kurtosis": m4 / m2 ** 2 - 3 if m2 else 0.0,
            "centre": med, "width": 1.4826 * mad or m2 ** .5}


_CHART_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Close deviation</title>
<style>
:root {{ --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --bar:#2a78d6;
  --ring:rgba(11,11,11,.10); }}
@media (prefers-color-scheme: dark) {{ :root {{ --page:#0d0d0d;
  --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --bar:#3987e5;
  --ring:rgba(255,255,255,.10); }} }}
body {{ margin:0; padding:24px 16px; background:var(--page); color:var(--ink);
  font:14px/1.45 system-ui, sans-serif; }}
main {{ max-width:820px; margin:0 auto; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
h2 {{ font-size:13px; font-weight:600; color:var(--ink2); margin:0 0 6px; }}
.sub {{ color:var(--ink2); margin:0 0 16px; }}
.card {{ background:var(--surface); border:1px solid var(--ring);
  border-radius:8px; padding:16px; margin-bottom:16px; position:relative; }}
.stats {{ display:flex; flex-wrap:wrap; gap:4px 24px; margin-bottom:12px; }}
.stats div {{ color:var(--ink2); }}
.stats b {{ color:var(--ink); font-variant-numeric:tabular-nums; }}
.legend {{ display:flex; gap:20px; color:var(--ink2); font-size:13px;
  margin-bottom:8px; }}
.legend i {{ display:inline-block; vertical-align:middle; margin-right:6px; }}
.sw {{ width:10px; height:10px; border-radius:2px; background:var(--bar); }}
.ln {{ width:18px; height:0; border-top:2px solid var(--ink); }}
.zl {{ width:18px; height:0; border-top:1px dashed var(--ink2); }}
svg {{ width:100%; height:auto; display:block; }}
svg text {{ fill:var(--muted); font-size:11px; }}
svg text.ink {{ fill:var(--ink2); }}
.bar {{ fill:var(--bar); }}
.hit {{ fill:transparent; }}
.bar.on {{ opacity:.75; }}
#tip {{ position:absolute; pointer-events:none; display:none;
  background:var(--surface); border:1px solid var(--ring); border-radius:6px;
  padding:6px 8px; box-shadow:0 2px 8px rgba(0,0,0,.12); white-space:nowrap; }}
#tip span {{ color:var(--ink2); }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th, td {{ text-align:right; padding:4px 8px; border-bottom:1px solid var(--grid); }}
th:first-child, td:first-child {{ text-align:left; }}
th {{ color:var(--ink2); font-weight:600; }}
.note {{ color:var(--ink2); font-size:13px; margin:8px 0 0; }}
</style></head><body><main>
<h1>Close deviation, new against old</h1>
<p class="sub">(new &minus; old) / old in percent &middot; old {old}
&middot; new {new}</p>
<div class="card">
<h2>All {n} names with a close in both files</h2>
<div class="stats">{stats}</div>
<h2>Shape of the non-zero deviations</h2>
<div class="stats">{shape}</div>
<p class="note">A Gaussian has skew 0 and excess kurtosis 0.  Skew away from
0 means one side is heavier; kurtosis well above 0 means fat tails.</p>
</div>
<div class="card" id="chart">
<div class="legend"><span><i class="sw"></i>names per bin</span>
<span><i class="ln"></i>normal fit (median, 1.4826&middot;MAD)</span>
<span><i class="zl"></i>zero</span></div>
<svg viewBox="0 0 {w} {h}" role="img"
 aria-label="Histogram of the non-zero Close deviations with a normal fit">{svg}</svg>
<div id="tip"></div>
<p class="note">{note}</p>
</div>
<div class="card"><h2>The buckets in close-deviation.csv</h2>
<table><thead><tr><th>bucket</th><th>count</th>
<th>share %</th></tr></thead><tbody>{rows}</tbody></table></div>
</main><script>
const tip = document.getElementById("tip"), box = document.getElementById("chart");
document.querySelectorAll(".hit").forEach(h => {{
  const bar = h.nextElementSibling;
  h.addEventListener("mousemove", e => {{
    const r = box.getBoundingClientRect();
    tip.innerHTML = "<b>" + h.dataset.label + "</b><br><span>" +
      h.dataset.count + " names</span>";
    tip.style.display = "block";
    let x = e.clientX - r.left + 12;
    if (x + tip.offsetWidth > r.width) x = e.clientX - r.left - tip.offsetWidth - 12;
    tip.style.left = x + "px"; tip.style.top = (e.clientY - r.top - 44) + "px";
    bar.classList.add("on");
  }});
  h.addEventListener("mouseleave", () => {{
    tip.style.display = "none"; bar.classList.remove("on"); }});
}});
</script></body></html>
"""


def _num(x) -> str:
    return "0" if x == 0 else f"{x:+.3g}"


def write_close_chart(path, dev, dist, old_name="", new_name="") -> str:
    """The distribution as a page, to answer one question by eye: is the
    deviation a Gaussian centred on zero, or something else?

    The histogram is of the NON-ZERO deviations, in equal bins symmetric
    about zero, with the normal fit drawn over it.  Identical closes are
    counted on the zero line rather than binned: when most names agree
    exactly they are one spike that would flatten everything else to
    nothing.  One self-contained file - inline SVG, no library."""
    from html import escape
    import math
    nz = sorted(float(v) for v in dev["devs"] if v != 0)
    zeros = len(dev["devs"]) - len(nz)
    w, h, left, top, bottom, right = 820, 320, 44, 24, 40, 24
    plot_w, plot_h = w - left - right, h - top - bottom

    parts, note = [], ""
    shape = shape_stats(nz)
    if nz:
        #  The 1st-99th percentile, but no wider than five fit widths
        #  either side of the centre: a fat tail would otherwise set the
        #  axis and squeeze the core - the part whose shape is the question
        #  - into a handful of bins.  A Gaussian has nothing beyond 5 sigma.
        lim = max(abs(_pct(nz, .01)), abs(_pct(nz, .99))) or abs(nz[-1])
        if shape and shape["width"]:
            lim = min(lim, abs(shape["centre"]) + 5 * shape["width"])
        bw = 2 * lim / CHART_BINS
        counts = [0] * CHART_BINS
        below = above = 0
        for v in nz:
            if v < -lim:
                below += 1
            elif v > lim:
                above += 1
            else:
                counts[min(CHART_BINS - 1, int((v + lim) / bw))] += 1
        curve = []
        if shape and shape["width"]:
            mu, sg, k = shape["centre"], shape["width"], sum(counts) * bw
            for j in range(161):
                x = -lim + 2 * lim * j / 160
                curve.append((x, k / (sg * math.sqrt(2 * math.pi))
                              * math.exp(-.5 * ((x - mu) / sg) ** 2)))
        most = max(max(counts), max((c for _, c in curve), default=0))
        step = next(s * 10 ** e for e in range(12) for s in (1, 2, 5)
                    if most <= 5 * s * 10 ** e)
        ymax = max(step, math.ceil(most / step) * step)

        def X(v):
            return left + plot_w * (v + lim) / (2 * lim)

        def Y(v):
            return top + plot_h * (1 - v / ymax)

        for v in range(0, ymax + 1, step):
            parts.append(f'<line x1="{left}" x2="{w - right}" y1="{Y(v):.1f}" '
                         f'y2="{Y(v):.1f}" stroke="var(--grid)"/>'
                         f'<text x="{left - 6}" y="{Y(v) + 4:.1f}" '
                         f'text-anchor="end">{v}</text>')
        slot = plot_w / CHART_BINS
        base = top + plot_h
        for i, n in enumerate(counts):
            lo = -lim + i * bw
            bx, bwid = left + i * slot + 1, slot - 2
            bh = plot_h * n / ymax
            r = min(4, bh, bwid / 2)
            yt = base - bh
            d = (f"M{bx:.1f},{base:.1f}V{yt + r:.1f}"
                 f"Q{bx:.1f},{yt:.1f} {bx + r:.1f},{yt:.1f}"
                 f"H{bx + bwid - r:.1f}Q{bx + bwid:.1f},{yt:.1f} "
                 f"{bx + bwid:.1f},{yt + r:.1f}V{base:.1f}Z") if n else ""
            parts.append(
                f'<rect class="hit" x="{left + i * slot:.1f}" y="{top}" '
                f'width="{slot:.1f}" height="{plot_h}" '
                f'data-label="{_num(lo)}% to {_num(lo + bw)}%" '
                f'data-count="{n}"/><path class="bar" d="{d}"/>')
        if curve:
            parts.append('<polyline fill="none" stroke="var(--ink)" '
                         'stroke-width="2" stroke-linejoin="round" points="'
                         + " ".join(f"{X(x):.1f},{Y(c):.1f}" for x, c in curve)
                         + '"/>')
        parts.append(f'<line x1="{left}" x2="{w - right}" y1="{base}" '
                     f'y2="{base}" stroke="var(--axis)"/>')
        for t in (-lim, -lim / 2, 0, lim / 2, lim):
            parts.append(f'<text x="{X(t):.1f}" y="{base + 16}" '
                         f'text-anchor="middle">{_num(t)}</text>')
        parts.append(f'<line x1="{X(0):.1f}" x2="{X(0):.1f}" y1="{top - 8}" '
                     f'y2="{base}" stroke="var(--ink2)" stroke-dasharray="3 3"/>'
                     f'<text class="ink" x="{X(0) + 6:.1f}" y="{top - 10}">'
                     f'{zeros} identical (exactly 0%) not binned</text>'
                     f'<text x="{left + plot_w / 2:.1f}" y="{h - 4}" '
                     f'text-anchor="middle">deviation, %</text>')
        note = (f"{len(nz)} non-zero deviations in {CHART_BINS} bins of "
                f"{bw:.3g}%, the 1st to 99th percentile or five fit widths "
                f"either side, whichever is narrower. "
                f"{below} below {_num(-lim)}% and {above} above "
                f"{_num(lim)}% are off the axis &mdash; min and max above "
                f"say how far.")
    else:
        note = (f"Nothing to draw: every one of the {zeros} closes is "
                f"identical." if zeros else
                "Nothing to draw: no name has a close in both files.")

    stats = "".join(f"<div>{k} <b>{v:+.4f}%</b></div>"
                    for k, v in close_stats(dev["devs"])) or "<div>&mdash;</div>"
    shape_html = "".join(
        f"<div>{k} <b>{v}</b></div>" for k, v in (
            ("n", shape["n"]), ("sd", f"{shape['sd']:.4f}%"),
            ("skew", f"{shape['skew']:+.2f}"),
            ("excess kurtosis", f"{shape['kurtosis']:+.2f}"),
            ("fit centre", f"{shape['centre']:+.4f}%"),
            ("fit width", f"{shape['width']:.4f}%"))) if shape else \
        "<div>fewer than two non-zero deviations</div>"
    total = sum(n for _, n in dist) or 1
    rows = "".join(f"<tr><td>{escape(label)}</td><td>{n}</td>"
                   f"<td>{100 * n / total:.2f}</td></tr>" for label, n in dist)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CHART_PAGE.format(
        n=len(dev["devs"]), old=escape(str(old_name)),
        new=escape(str(new_name)), stats=stats, shape=shape_html, w=w, h=h,
        svg="".join(parts), note=note, rows=rows), encoding="utf-8")
    return str(path)


def write_compare_report(path, records) -> str:
    """A run with nothing to report still writes the header, which is the
    readable way to say there was nothing to report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COMPARE_COLUMNS,
                           lineterminator="\n")
        w.writeheader()
        for d in records:
            w.writerow(d)
    return str(path)


def print_compare(d):
    print(f"\n  rows in both        {d['shared']}")
    print(f"  only in the old     {len(d['only_old'])}")
    print(f"  only in the new     {len(d['only_new'])}")
    print("\n  column                    same  differ")
    for c, v in d["columns"].items():
        print(f"    {c:<22} {v['same']:6d}  {v['differ']:6d}")
        for k, a, b in v["examples"]:
            print(f"        {k}  old={a!r}  new={b!r}")


def read_output(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def demo() -> int:
    """The whole pipeline on canned data.  No kdb, no files, no licence."""
    import tempfile
    here = Path(__file__).resolve().parent
    markets = marketcfg.load(here / "config" / "markets.csv")

    rows = [
        crosscode.Row("BHP.AU", "BHP.AX", "BHP AU", "BHP", "AU", "Equity",
                      "Equity", "ASX-MAIN", "AUD", False),
        crosscode.Row("STW.AU", "STW.AX", "STW AU", "STW", "AU", "ETF",
                      "Equity", "ASX-MAIN", "AUD", False),
        crosscode.Row("005930.KR", "005930.KS", "005930 KP", "005930", "KP",
                      "Equity", "Equity", "KSC-MAIN", "KRW", False),
        crosscode.Row("823.HK", "823.HK", "823 HK", "823", "HK", "Equity",
                      "REIT", "HKG-MAIN", "HKD", True),
    ]
    master = {
        "BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9, "volatility": 0.21,
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 210000.0,
                   "fx_last": 0.65, "ID_ISIN": "AU000000BHP4",
                   "INDUSTRY_SECTOR": "Basic Materials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        "STW.AU": {"PX_LAST": 72.1, "EQY_BETA": 1.0, "volatility": 0.11,
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 4200.0,
                   "fx_last": 0.65, "ID_ISIN": "AU0000STW014",
                   "INDUSTRY_SECTOR": "Financials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        # Korea hits on the COMPOSITE, not the crosscode's own KP suffix -
        # which is the whole reason sym resolution tries two candidates.
        "005930.KS": {"PX_LAST": 71000.0, "EQY_BETA": 1.1,
                      "volatility": 0.28, "REL_INDEX": "KOSPI",
                      "CUR_MKT_CAP": 4.2e8, "fx_last": 0.00072,
                      "ID_ISIN": "KR7005930003",
                      "INDUSTRY_SECTOR": "Technology",
                      "MARKET_STATUS": "ACTV", "CRNCY": "KRW"},
    }

    hits = {}
    out = build_rows(rows, master, markets, None, hits)
    problems = validate(out)
    when = datetime.date(2026, 9, 2)

    print("trading_data --demo")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TradingData.csv"
        write_csv(p, out)
        print("\n" + p.read_text(encoding="utf-8"))
        report(out, rows, [], hits, when, when, None, markets,
               topup_candidates(rows, master, markets), master)

    if hits.get("005930.KR") == "005930.KS":
        print("\n  note: Korea matched on the composite (KS), not the "
              "crosscode's own suffix (KP)")
    print("  note: 823.HK is a REIT, so RespectShortSellPrice stays TRUE; a "
          "Hong Kong ETF would be FALSE")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return 0 if not problems else 1


def say(line=""):
    """Progress, flushed.

    A LIVE RUN MUST NARRATE ITSELF.  Everything interesting here - the
    connect, and a fetch of tens of thousands of syms - happens before the
    report, so a silent run is indistinguishable from a hung one.  flush
    because stdout block-buffers the moment it is piped to a file, which is
    how this job is actually launched."""
    print(line, flush=True)


def input_file_lines(paths, now=None):
    """WHEN EACH INPUT WAS LAST WRITTEN, and how long ago, so the output
    says whether this run read today's CrossCode or one left over from last
    week.  A path left blank in local_settings is not an input and is
    skipped - the run already says "not supplied" for it."""
    now = now or datetime.datetime.now()
    lines = ["  input files (last modified):"]
    for p in paths:
        if not p:
            continue
        p = Path(p)
        try:
            mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            lines.append(f"    MISSING              {p}")
            continue
        hours = (now - mtime).total_seconds() / 3600
        age = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.0f} days ago"
        lines.append(f"    {mtime:%Y-%m-%d %H:%M:%S}  ({age})  {p}")
    return lines


def run(crosscode_path, server, output_path, temp_path,
        mapping_path="", date=None, nse_cas_path="", bse_cas_path="",
        hkex_cas_path="", override_path="") -> int:
    started = time.time()

    def step(label):
        say(f"[{time.time() - started:6.1f}s] {label}")

    #  BEFORE ANY OF THEM IS READ, so a run that then fails on one still
    #  says how old it was.
    for line in input_file_lines([crosscode_path, mapping_path,
                                  nse_cas_path, bse_cas_path, hkex_cas_path,
                                  override_path]):
        say(line)

    step(f"reading crosscode {crosscode_path}")
    rows, excluded = crosscode.load(crosscode_path)
    say(f"  {len(rows)} rows kept, {sum(len(e.rows) for e in excluded)} "
        f"excluded")

    here = Path(__file__).resolve().parent
    markets = marketcfg.load(here / "config" / "markets.csv")
    say(f"  {len(markets)} markets configured")
    mapping = msci.load(mapping_path) if mapping_path else None
    say("  msci mapping " + ("loaded" if mapping else "not supplied"))
    cas = caslist.load_india(nse_cas_path, bse_cas_path)
    for market, path in ((caslist.NSE_MARKET, nse_cas_path),
                         (caslist.BSE_MARKET, bse_cas_path)):
        if not path:
            say(f"  {market} cas list not supplied")
        else:
            say(f"  {market} cas list {len(cas.get(market, ())):6d} isins  "
                f"{path}")
    hkex = caslist.load_hkex(hkex_cas_path) if hkex_cas_path else set()
    if not hkex_cas_path:
        say("  HKEX cas list not supplied")
    else:
        say(f"  HKEX cas list      {len(hkex):6d} codes  {hkex_cas_path}")
        if not hkex:
            #  The HK match is inverted, so an empty list is the difference
            #  between marking nothing and marking everything.  It marks
            #  nothing, and that has to be said out loud.
            say("  ! the HKEX list is EMPTY - no Hong Kong row will be "
                "marked NO_CAS")
    override = auction.load(override_path, say) if override_path else {}
    if not override_path:
        say("  auction override not supplied - OpenAggressivityPct is blank")
    else:
        say(f"  auction override   {len(override):6d} codes  {override_path}")

    host, _, port = server.partition(":")
    step(f"connecting to equity_master at {server}")
    conn = equitymaster.connect(host, port, log=say)

    asked = date or (datetime.date.today() - datetime.timedelta(days=1))
    step(f"resolving the partition on or before {asked}")
    used, how = equitymaster.resolve_date(conn, asked, log=say)
    say(f"  partition {equitymaster.date_text(used)}, "
        f"resolved by the {how.split()[0]} form")

    syms = []
    for row in rows:
        syms.extend(equitymaster.sym_candidates(row, markets))
    syms = sorted(set(syms))
    step(f"fetching {len(syms)} syms in one round trip - this is the slow one")
    master = equitymaster.fetch(conn, used, syms, log=say)
    say(f"  {len(master)} syms came back with a row")

    step("building rows")
    hits = {}
    out = build_rows(rows, master, markets, mapping, hits, cas, hkex,
                     override)
    say(f"  {len(out)} output rows, {len(hits)} matched a sym")
    if override:
        #  The override is keyed on RicCode, and nothing checks that the
        #  file spells a RIC the way the crosscode does.  A file that
        #  loads and then matches nothing is the failure to catch here.
        took = sum(1 for r in out if r["OpenAggressivityPct"])
        say(f"  {took} rows took an override percentage")
        if not took:
            say("  ! NOT ONE RicCode on the override matched the crosscode")

    step("validating")
    problems = validate(out)
    report(out, rows, excluded, hits, used, asked, mapping, markets,
           topup_candidates(rows, master, markets), master)

    if problems:
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        return 1

    write_csv(temp_path, out)
    shutil.copyfile(temp_path, output_path)
    print(f"\n  wrote {len(out)} rows to {output_path}")
    return 0


def _settings():
    try:
        import local_settings
    except ImportError:
        raise SystemExit(
            "local_settings.py not found.  Copy local_settings.py.example "
            "and fill it in.")
    return local_settings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build TradingData.csv")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--compare", metavar="OLD.csv")
    ap.add_argument("--report", default=COMPARE_REPORT, metavar="CSV",
                    help=f"where --compare writes every difference "
                         f"(default {COMPARE_REPORT})")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.demo:
        return demo()

    s = _settings()
    date = datetime.date.fromisoformat(args.date) if args.date else None

    #  A kdb failure has already said everything useful - the label, the
    #  query and the q type of every argument.  A traceback on top of that
    #  only buries it.
    try:
        rc = run(s.CROSSCODE_PATH, s.EQUITY_MASTER_SERVER, s.OUTPUT_PATH,
                 s.TEMP_PATH, getattr(s, "MSCI_MAPPING_PATH", ""), date,
                 getattr(s, "INDIA_NSE_CAS_LIST_PATH", ""),
                 getattr(s, "INDIA_BSE_CAS_LIST_PATH", ""),
                 getattr(s, "HKEX_CAS_LIST_PATH", ""),
                 getattr(s, "OPEN_AUCTION_OVERRIDE_PATH", ""))
    except equitymaster.KdbError as e:
        say(f"\n  FAILED: {e}")
        return 1

    if args.compare:
        old, new = read_output(args.compare), read_output(s.OUTPUT_PATH)
        print_compare(compare(old, new))
        records = differences(old, new)
        print(f"\n  {len(records)} difference(s) written to "
              f"{write_compare_report(args.report, records)}")
        dev = close_deviations(old, new)
        dist = close_distribution(dev)
        print_close(dev, dist)
        close_path = Path(args.report).with_name(CLOSE_REPORT)
        print(f"\n  Close distribution written to "
              f"{write_close_report(close_path, dist)}")
        chart = write_close_chart(close_path.with_name(CLOSE_CHART), dev,
                                  dist, args.compare, s.OUTPUT_PATH)
        print(f"  and drawn in {chart}")
    return rc


def self_test() -> int:
    import tempfile
    from decimal import Decimal
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("trading_data --self-test\n\nthe output shape")
    check("twenty columns", len(OUTPUT_COLUMNS), 20)
    check("the first is the hashed fidessa code",
          OUTPUT_COLUMNS[0], "#FidessaCode")
    check("the order matches :467-470",
          OUTPUT_COLUMNS[:6],
          ["#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex"])
    check("and the tail", OUTPUT_COLUMNS[-3:],
          ["MarketCap", "ISIN", "SubscribeFeedAtStartup"])

    here = Path(__file__).resolve().parent
    M = marketcfg.load(here / "config" / "markets.csv")

    row = crosscode.Row(
        fidessa_code="BHP.AU", ric="BHP.AX", bbg="BHP AU", ticker="BHP",
        bbg_ext="AU", sec_type="Equity", bbg_sec_type="Equity",
        market="ASX-MAIN", currency="AUD", is_reit=False)
    master = {"BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9,
                         "volatility": 0.21, "REL_INDEX": "AS51",
                         "CUR_MKT_CAP": 200.0, "fx_last": 0.65,
                         "ID_ISIN": "AU000000BHP4",
                         "INDUSTRY_SECTOR": "Basic Materials",
                         "MARKET_STATUS": "ACTV", "CRNCY": "AUD"}}
    hits = {}
    out = build_rows([row], master, M, None, hits)
    r = out[0]

    print("\nthe six fields that matter")
    check("Close is PX_LAST", r["Close"], "40.5")
    check("Beta is EQY_BETA, not the lowercase beta", r["Beta"], "0.9")
    check("Volatility10D is the volatility column, as a percentage",
          r["Volatility10D"], "21")
    check("Index is REL_INDEX", r["Index"], "AS51")
    check("MarketCap is CUR_MKT_CAP, in millions, times fx_last",
          r["MarketCap"], "130000000")
    check("and carries no trailing zeros, as R's write.csv does not",
          "." in r["MarketCap"], False)
    check("Capi buckets off the converted value", r["Capi"], "MICRO")

    print("\nthe rest")
    check("ISIN comes straight across", r["ISIN"], "AU000000BHP4")
    check("Sector falls back to INDUSTRY_SECTOR", r["Sector"], "Basic Materials")
    check("ICBIndex is seeded from REL_INDEX, per :137",
          r["ICBIndex"], "AS51")
    check("an ASX equity is bucketed alphabetically", r["Segment"], "A-B")
    check("Australia can short", r["NoShortSell"], "FALSE")
    check("and has no short-sell price rule",
          r["RespectShortSellPrice"], "")
    check("no mapping means the Msci columns are blank",
          [r[c] for c in ("MsciCountryIndex", "MsciSectorIndex")], ["", ""])
    check("SubscribeFeedAtStartup is always FALSE, per the :599 bug",
          r["SubscribeFeedAtStartup"], "FALSE")
    check("the sym that hit is recorded", hits["BHP.AU"], "BHP.AU")

    print("\na row equity_master does not have")
    out = build_rows([row], {}, M, None, {})
    r = out[0]
    check("the crosscode columns still fill",
          (r["#FidessaCode"], r["Type"]), ("BHP.AU", "Equity"))
    check("and everything from kdb is blank, not zero",
          [r[c] for c in ("Close", "Beta", "MarketCap", "ISIN")],
          ["", "", "", ""])

    print("\nindia, and its closing-auction list")
    nse = crosscode.Row(
        fidessa_code="RELIANCE.IN", ric="RELI.NS", bbg="RELIANCE IN",
        ticker="RELIANCE", bbg_ext="IN", sec_type="Equity",
        bbg_sec_type="Equity", market="NSI-MAIN", currency="INR",
        is_reit=False)
    bse = crosscode.Row(
        fidessa_code="RELIANCE.IB", ric="RELI.BO", bbg="RELIANCE IB",
        ticker="RELIANCE", bbg_ext="IB", sec_type="Equity",
        bbg_sec_type="Equity", market="BSE-MAIN", currency="INR",
        is_reit=False)
    em = {"RELIANCE.IN": {"ID_ISIN": "INE002A01018", "PX_LAST": 1400.0}}

    hits = {}
    got = build_rows([bse], em, M, None, hits)[0]
    check("a Bombay row is looked up under .IN",
          (hits.get("RELIANCE.IB"), got["ISIN"]),
          ("RELIANCE.IN", "INE002A01018"))
    hits = {}
    build_rows([nse], em, M, None, hits)
    check("an NSE row with no .IS sym falls back to .IN",
          hits.get("RELIANCE.IN"), "RELIANCE.IN")
    hits = {}
    build_rows([nse], dict(em, **{"RELIANCE.IS": {"PX_LAST": 1401.0}}), M,
               None, hits)
    check("and takes .IS when equity_master has both",
          hits.get("RELIANCE.IN"), "RELIANCE.IS")

    check("with no list, an Indian row takes the default segment",
          build_rows([nse], em, M, None, {})[0]["Segment"],
          columns.SEGMENT_DEFAULT)

    cas = {caslist.NSE_MARKET: {"INE002A01018"}}
    check("on the NSE list it is CAS, which :580 writes over whatever the "
          "market rules chose",
          build_rows([nse], em, M, None, {}, cas)[0]["Segment"], "CAS")
    check("and the SAME isin on Bombay is not, because only the NSE list "
          "was supplied",
          build_rows([bse], em, M, None, {}, cas)[0]["Segment"],
          columns.SEGMENT_DEFAULT)
    check("a row equity_master has no ISIN for cannot be marked",
          build_rows([nse], {}, M, None, {}, cas)[0]["Segment"],
          columns.SEGMENT_DEFAULT)
    check("and a Japanese row is untouched by any of it",
          build_rows([row], master, M, None, {}, cas)[0]["Segment"], "A-B")

    print("\nhong kong, where the list says who KEEPS an auction")
    hk = crosscode.Row(
        fidessa_code="700.HK", ric="0700.HK", bbg="700 HK", ticker="700",
        bbg_ext="HK", sec_type="Equity", bbg_sec_type="Equity",
        market="HKG-MAIN", currency="HKD", is_reit=False)
    hk_off = crosscode.Row(
        fidessa_code="1234.HK", ric="1234.HK", bbg="1234 HK",
        ticker="1234", bbg_ext="HK", sec_type="Equity",
        bbg_sec_type="Equity", market="HKG-MAIN", currency="HKD",
        is_reit=False)
    hk_warrant = crosscode.Row(
        fidessa_code="9999.HK", ric="9999.HK", bbg="9999 HK",
        ticker="9999", bbg_ext="HK", sec_type="Warrant",
        bbg_sec_type="Equity", market="HKG-MAIN", currency="HKD",
        is_reit=False)
    codes = {"700 HK"}

    check("a name ON the list keeps the default segment",
          build_rows([hk], {}, M, None, {}, None, codes)[0]["Segment"],
          columns.SEGMENT_DEFAULT)
    check("one that is NOT on it is NO_CAS - the match is inverted",
          build_rows([hk_off], {}, M, None, {}, None, codes)[0]["Segment"],
          "NO_CAS")
    check("a warrant is exempt, per :339",
          build_rows([hk_warrant], {}, M, None, {}, None,
                     codes)[0]["Segment"], columns.SEGMENT_DEFAULT)
    check("NO LIST MARKS NOTHING, which for an inverted match is the "
          "difference between marking none and marking every HK name",
          [build_rows([r], {}, M, None, {})[0]["Segment"]
           for r in (hk, hk_off)],
          [columns.SEGMENT_DEFAULT, columns.SEGMENT_DEFAULT])

    print("\nthe open-auction override, which is all OpenAggressivityPct is")
    check("no override leaves the column blank, as the R job's NA does",
          build_rows([row], master, M, None, {})[0]["OpenAggressivityPct"],
          "")
    check("the join is on RicCode, per :325 - not on the fidessa code",
          build_rows([row], master, M, None, {}, None, None,
                     {"BHP.AX": "50"})[0]["OpenAggressivityPct"], "50")
    check("so a file that spells the key some other way matches nothing",
          build_rows([row], master, M, None, {}, None, None,
                     {"BHP.AU": "50"})[0]["OpenAggressivityPct"], "")
    check("a row that is not on the override is blank, not zero",
          build_rows([hk], {}, M, None, {}, None, None,
                     {"BHP.AX": "50"})[0]["OpenAggressivityPct"], "")

    print("\na row equity_master has, carrying zeros")
    zero = {"BHP.AU": dict(master["BHP.AU"], PX_LAST=0.0, EQY_BETA=0,
                           volatility="0.00")}
    r = build_rows([row], zero, M, None, {})[0]
    check("a zero close is no close, so it is blank not \"0\"", r["Close"], "")
    check("the same for beta", r["Beta"], "")
    check("and for volatility, which a x100 would only have kept at zero",
          r["Volatility10D"], "")
    check("a real negative beta is still a value",
          build_rows([row], {"BHP.AU": dict(master["BHP.AU"],
                                            EQY_BETA=-0.4)},
                     M, None, {})[0]["Beta"], "-0.4")

    print("\nwhich currency fx_last converts to")
    fxm = {"A.AU": {"CRNCY": "AUD", "fx_last": 0.65},
           "B.US": {"CRNCY": "USD", "fx_last": 1.0},
           "C.AU": {"CRNCY": "AUD", "fx_last": 0.65}}
    seen = fx_seen(fxm, {"a": "A.AU", "b": "B.US", "c": "C.AU"})
    check("one line per currency, with the row count",
          seen, [("AUD", "0.65", 2), ("USD", "1", 1)])
    check("and the base is the one quoted at 1",
          [c for c, r, _ in seen if _D(r) == 1], ["USD"])
    check("a row with no CRNCY or no rate is not a currency",
          fx_seen({"X": {"fx_last": 0.5}, "Y": {"CRNCY": "JPY"}},
                  {"x": "X", "y": "Y"}), [])

    print("\nsorting, per :606")
    rows = [
        crosscode.Row("Z.AU", "ZZZ.AX", "ZZZ AU", "ZZZ", "AU", "Equity",
                      "Equity", "ASX-MAIN", "AUD", False),
        crosscode.Row("A.HK", "1.HK", "1 HK", "1", "HK", "Equity",
                      "Equity", "HKG-MAIN", "HKD", False),
        crosscode.Row("A.AU", "AAA.AX", "AAA AU", "AAA", "AU", "Equity",
                      "Equity", "ASX-MAIN", "AUD", False),
    ]
    out = build_rows(rows, {}, M, None, {})
    check("by FidessaMarket then RicCode, not by FidessaCode",
          [x["#FidessaCode"] for x in out], ["A.AU", "Z.AU", "A.HK"])

    print("\nvalidation")
    check("no rows is fatal", validate([])[0].startswith("no rows"), True)
    good = build_rows([row], master, M, None, {})
    check("a good run has nothing to say", validate(good), [])

    print("\nwriting")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TradingData.csv"
        write_csv(p, good)
        text = p.read_text(encoding="utf-8")
        check("the header is the twenty columns",
              text.splitlines()[0], ",".join(OUTPUT_COLUMNS))
        check("nothing is quoted", '"' in text, False)
        check("no index column was added",
              text.splitlines()[1].startswith("BHP.AU,"), True)

    print("\ncomparing against the R job's output")
    old = [{"#FidessaCode": "A", "Close": "10.0", "Beta": "1.0"},
           {"#FidessaCode": "B", "Close": "20.0", "Beta": "2.0"}]
    new = [{"#FidessaCode": "A", "Close": "10.0", "Beta": "1.1"},
           {"#FidessaCode": "C", "Close": "30.0", "Beta": "3.0"}]
    d = compare(old, new)
    check("rows only the old file has", d["only_old"], ["B"])
    check("rows only the new file has", d["only_new"], ["C"])
    check("a column that agrees", d["columns"]["Close"]["same"], 1)
    check("a column that does not", d["columns"]["Beta"]["differ"], 1)
    check("and it shows an example",
          d["columns"]["Beta"]["examples"][0], ("A", "1.0", "1.1"))

    print("\nthe same comparison, as the report carries it")
    recs = differences(old, new)
    check("a row for every difference and every name only one file has, "
          "each named by its #FidessaCode - the key the two files agree on, "
          "and the only name this job's output carries",
          recs,
          [{"status": "only_in_old", "code": "B", "column": "",
            "old": "", "new": ""},
           {"status": "only_in_new", "code": "C", "column": "",
            "old": "", "new": ""},
           {"status": "differs", "code": "A", "column": "Beta",
            "old": "1.0", "new": "1.1"}])

    #  The printed form caps at five a column.  A cutover needs all of them:
    #  Volatility10D's spread cannot be measured from a sample.
    many_old = [{"#FidessaCode": f"N{i}", "Volatility10D": "10.0"}
                for i in range(20)]
    many_new = [{"#FidessaCode": f"N{i}", "Volatility10D": "11.0"}
                for i in range(20)]
    check("THE REPORT IS NOT CAPPED AT THE FIVE EXAMPLES THE SCREEN SHOWS",
          len(differences(many_old, many_new)), 20)
    check("while the printed form still shows five",
          len(compare(many_old, many_new)["columns"]["Volatility10D"]
              ["examples"]), 5)

    with tempfile.TemporaryDirectory() as dd:
        p = Path(dd) / "report.csv"
        write_compare_report(p, recs)
        lines = p.read_text(encoding="utf-8").splitlines()
        check("the report is the columns, then a row per difference",
              lines,
              [",".join(COMPARE_COLUMNS),
               "only_in_old,B,,,", "only_in_new,C,,,",
               "differs,A,Beta,1.0,1.1"])
        write_compare_report(p, [])
        check("nothing to report still writes the header",
              p.read_text(encoding="utf-8").splitlines(),
              [",".join(COMPARE_COLUMNS)])

    print("\nhow far our Close sits from the old file's")
    old = [{"#FidessaCode": k, "Close": c} for k, c in
           (("A", "100"), ("B", "100"), ("C", "100"), ("D", "100"),
            ("E", ""), ("F", "0"), ("G", "50"))]
    new = [{"#FidessaCode": k, "Close": c} for k, c in
           (("A", "100"), ("B", "100.05"), ("C", "88"), ("D", "130"),
            ("E", "10"), ("F", "10"), ("G", ""), ("H", "1"))]
    dev = close_deviations(old, new)
    check("the deviation is relative, in percent, signed new minus old",
          sorted(dev["devs"]), [-12, 0, Decimal("0.05"), 30])
    check("a blank or zero old close, and a blank new one, are counted "
          "apart rather than as a deviation",
          (dev["blank_old"], dev["blank_new"]), (2, 1))
    dist = dict(close_distribution(dev))
    check("the buckets run most negative to most positive, zero its own",
          list(dist)[:11],
          ["< -10%", "-10% to -5%", "-5% to -1%", "-1% to -0.1%",
           "-0.1% to 0%", "0%", "0% to 0.1%", "0.1% to 1%", "1% to 5%",
           "5% to 10%", "> 10%"])
    check("and each name lands in one",
          (dist["0%"], dist["0% to 0.1%"], dist["< -10%"], dist["> 10%"],
           dist["no close in old"], dist["no close in new"]),
          (1, 1, 1, 1, 2, 1))
    check("every name in both files is accounted for exactly once",
          sum(dist.values()), 7)
    with tempfile.TemporaryDirectory() as dd:
        p = Path(dd) / "close.csv"
        write_close_report(p, close_distribution(dev))
        lines = p.read_text(encoding="utf-8").splitlines()
        check("the file is a row per bucket with its share",
              (lines[0], lines[6]),
              (",".join(CLOSE_COLUMNS), "0%,1,14.29"))
        page = Path(write_close_chart(Path(dd) / "c.html", dev,
                                      close_distribution(dev))).read_text(
                                          encoding="utf-8")
        check("the chart bins in equal widths, not the report's buckets",
              page.count('class="hit"'), CHART_BINS)
        check("and says how many identical closes it left out of the bins",
              "1 identical (exactly 0%) not binned" in page, True)
        check("and an empty comparison still draws",
              "no name has a close" in Path(write_close_chart(
                  Path(dd) / "e.html", close_deviations([], []),
                  close_distribution(close_deviations([], [])))).read_text(
                      encoding="utf-8"), True)

    print("\nhow Gaussian the deviations are")
    sym = [-2.0, -1.0, -1.0, 0.5, 0.5, 1.0, 1.0, 2.0, -0.5, -0.5]
    got = shape_stats(sym)
    check("a symmetric spread has no skew", round(got["skew"], 9), 0)
    check("one far-off name makes the tails fat",
          shape_stats(sym + [40.0])["kurtosis"] > 3, True)
    check("and the fit's width barely moves for it, where the sd balloons",
          (round(shape_stats(sym + [40.0])["width"] / got["width"], 1),
           shape_stats(sym + [40.0])["sd"] > 5 * got["sd"]), (1.0, True))
    check("fewer than two values has no shape", shape_stats([1.0]), {})

    print("\nthe input files' modified times")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "CrossCode.csv"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (0, datetime.datetime(2026, 9, 23, 6, 0).timestamp()))
        got = input_file_lines([str(f), "", str(Path(d) / "gone.csv")],
                               now=datetime.datetime(2026, 9, 23, 7, 30))
        check("each file gets its modified time and its age",
              got[1], f"    2026-09-23 06:00:00  (1.5h ago)  {f}")
        check("a blank setting is skipped and a file that is not there "
              "says so", (len(got), got[2].startswith("    MISSING")),
              (3, True))
        old = input_file_lines([str(f)],
                               now=datetime.datetime(2026, 9, 30, 6, 0))
        check("an old one is counted in days", "(7 days ago)" in old[1],
              True)

    print("\nthe demo runs end to end with no kdb")
    check("demo returns success", demo(), 0)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
