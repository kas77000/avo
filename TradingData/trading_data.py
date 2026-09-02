#!/usr/bin/env python3
"""Build TradingData.csv from the crosscode and kdb's equity_master.

The crosscode is the security master and drives the row set.  equity_master
supplies the reference data the R job got from Bloomberg.  Every other input
is optional: present means used, absent means those columns go blank and the
run says so.

WHAT IS NOT FILLED, AND WHY

  MsciCountryIndex, MsciSectorCountryIndex, MsciSectorIndex,
  MsciSectorRegionIndex   need msci_mapping.csv
  OpenAggressivityPct     needs the auction override CSV
  Segment for HKG/NSI/BSE needs the dico and CAS lists

  Segment for HK ETFs is the one genuinely unavailable field.  It comes from
  TRADING_CONDITIONS_1 via an intraday Bloomberg call at :357 and has no
  equivalent in equity_master.  qatt.cond was considered and ruled out.

THREE SOURCES ARE UNVERIFIED and print a banner rather than being trusted
silently:

  Volatility10D   equity_master.volatility may not be the 10-day figure
                  Bloomberg's VOLATILITY_10D returns
  MarketCap/Capi  assumes fx_last is a local->USD rate matching load_FXdatas
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
"""

from __future__ import annotations

import argparse
import csv
import datetime
import shutil
import sys
from pathlib import Path

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


def build_rows(rows, master, markets, mapping, sym_hits) -> list:
    """One output dict per crosscode row.  Missing reference data leaves a
    column blank; it never becomes zero."""
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
        market_cap = cap * fx if cap is not None and fx is not None else None

        rel_index = _T(rec.get("REL_INDEX"))
        industry = _T(rec.get("INDUSTRY_SECTOR"))

        seg = columns.segment_cn(row.market, row.sec_type)
        if seg is None and row.market == "ASX-MAIN":
            seg = columns.segment_asx(row.ticker, row.sec_type)
        if seg is None:
            seg = columns.SEGMENT_DEFAULT

        out = {
            "#FidessaCode": row.fidessa_code,
            "Type": row.sec_type,
            "Sector": columns.sector("", industry),
            "Capi": columns.capi_bucket(market_cap),
            "Index": rel_index,
            "ICBIndex": "",            # filled by the propagation below
            "Segment": seg,
            "Beta": _plain(_D(rec.get("EQY_BETA"))),
            "Close": _plain(_D(rec.get("PX_LAST"))),
            "Volatility10D": _plain(_D(rec.get("volatility"))),
            "NoShortSell": marketcfg.no_short_sell(row.market, markets),
            "RespectShortSellPrice": marketcfg.respect_short_sell(
                row.market, row.sec_type, row.is_reit, markets),
            "OpenAggressivityPct": "",
            "MarketCap": _plain(market_cap),
            "ISIN": _T(rec.get("ID_ISIN")),
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
           mapping, markets, topup=None):
    print(f"\n  crosscode rows      {len(rows)}")
    for e in excluded:
        print(f"  excluded            {len(e.rows):6d}  {e.reason}")
    print(f"  equity_master date  {date_used}"
          + ("" if date_used == date_asked else f"  (asked {date_asked})"))
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

    if mapping is None:
        print("\n  ! msci_mapping.csv not supplied - the four Msci* columns "
              "are blank")

    if topup is not None:
        print(f"\n  the :113 bug: {topup} row(s) would have entered the dead "
              "top-up if it read `> 0`")

    print("\n  ! UNVERIFIED SOURCES - confirm before cutover")
    print("    Volatility10D  from equity_master.volatility; the definition "
          "is NOT confirmed to be Bloomberg's VOLATILITY_10D")
    print("    MarketCap/Capi CUR_MKT_CAP * fx_last; fx_last's direction is "
          "assumed to be local->USD")
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
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 2.1e11,
                   "fx_last": 0.65, "ID_ISIN": "AU000000BHP4",
                   "INDUSTRY_SECTOR": "Basic Materials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        "STW.AU": {"PX_LAST": 72.1, "EQY_BETA": 1.0, "volatility": 0.11,
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 4.2e9,
                   "fx_last": 0.65, "ID_ISIN": "AU0000STW014",
                   "INDUSTRY_SECTOR": "Financials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        # Korea hits on the COMPOSITE, not the crosscode's own KP suffix -
        # which is the whole reason sym resolution tries two candidates.
        "005930.KS": {"PX_LAST": 71000.0, "EQY_BETA": 1.1,
                      "volatility": 0.28, "REL_INDEX": "KOSPI",
                      "CUR_MKT_CAP": 4.2e14, "fx_last": 0.00072,
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
               topup_candidates(rows, master, markets))

    if hits.get("005930.KR") == "005930.KS":
        print("\n  note: Korea matched on the composite (KS), not the "
              "crosscode's own suffix (KP)")
    print("  note: 823.HK is a REIT, so RespectShortSellPrice stays TRUE; a "
          "Hong Kong ETF would be FALSE")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return 0 if not problems else 1


def run(crosscode_path, server, output_path, temp_path,
        mapping_path="", date=None) -> int:
    rows, excluded = crosscode.load(crosscode_path)
    here = Path(__file__).resolve().parent
    markets = marketcfg.load(here / "config" / "markets.csv")
    mapping = msci.load(mapping_path) if mapping_path else None

    host, _, port = server.partition(":")
    conn = equitymaster.connect(host, port)

    asked = date or (datetime.date.today() - datetime.timedelta(days=1))
    used = equitymaster.resolve_date(conn, asked)

    syms = []
    for row in rows:
        syms.extend(equitymaster.sym_candidates(row, markets))
    master = equitymaster.fetch(conn, used, sorted(set(syms)))

    hits = {}
    out = build_rows(rows, master, markets, mapping, hits)
    problems = validate(out)
    report(out, rows, excluded, hits, used, asked, mapping, markets,
           topup_candidates(rows, master, markets))

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
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.demo:
        return demo()

    s = _settings()
    date = datetime.date.fromisoformat(args.date) if args.date else None

    rc = run(s.CROSSCODE_PATH, s.EQUITY_MASTER_SERVER, s.OUTPUT_PATH,
             s.TEMP_PATH, getattr(s, "MSCI_MAPPING_PATH", ""), date)

    if args.compare:
        print_compare(compare(read_output(args.compare),
                              read_output(s.OUTPUT_PATH)))
    return rc


def self_test() -> int:
    import tempfile
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
                         "CUR_MKT_CAP": 2000000.0, "fx_last": 0.65,
                         "ID_ISIN": "AU000000BHP4",
                         "INDUSTRY_SECTOR": "Basic Materials",
                         "MARKET_STATUS": "ACTV", "CRNCY": "AUD"}}
    hits = {}
    out = build_rows([row], master, M, None, hits)
    r = out[0]

    print("\nthe six fields that matter")
    check("Close is PX_LAST", r["Close"], "40.5")
    check("Beta is EQY_BETA, not the lowercase beta", r["Beta"], "0.9")
    check("Volatility10D is the volatility column", r["Volatility10D"], "0.21")
    check("Index is REL_INDEX", r["Index"], "AS51")
    check("MarketCap is CUR_MKT_CAP times fx_last",
          r["MarketCap"], "1300000")
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

    print("\nthe demo runs end to end with no kdb")
    check("demo returns success", demo(), 0)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
