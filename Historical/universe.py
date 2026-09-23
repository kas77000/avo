#!/usr/bin/env python3
"""Crosscode rows plus equity_master -> the names this job actually fetches.

Three things happen here, and all three need the kdb answer in hand, which
is why none of them live in crosscode.py.

ONE  RESOLVE.  A crosscode row carries `7203 JT` - ticker and PRIMARY
     exchange code.  qatt is keyed on the COMPOSITE: `7203.JP`.
     equity_master supplies the composite authoritatively; config/markets.csv
     is the fallback for a name it has no row for, and the tally says how
     many took it.

TWO  COLLAPSE.  `7203 JT`, `7203 JE` and `7203 JI` are three crosscode rows -
     Tokyo, JNX and Chi-X Japan - and ONE `7203.JP` in qatt, because the
     composite consolidates the venues.  They therefore make one file, not
     three.  Miss this and the same file is written three times per run,
     each write racing the last, and the tick counts look fine in every one.

     WHICH ROW NAMES THE FILE is then a real question, and the answer is the
     primary listing: the row whose exchange code equals equity_master's
     EQY_PRIM_EXCH_SHRT.  So Toyota's file is `raw-7203 JT-...`, never
     `raw-7203 JE-...`, and never the composite.

THREE RENAME.  Some markets are written under their COMPOSITE code rather
     than the crosscode's own: `RIO AT` is `RIO AU` on disk, `7203 JT` is
     `7203 JP`, and an Indian line is `IN` whichever board it trades on.
     config/composites.csv holds that rule, taken from the legacy job's
     MarketConditionBBG.xml - Convert2Composite and CompositeExchangeCode
     per Bloomberg exchange code - and it names the FOLDER and the FILE
     alike.  A code that does not convert, and one the file has never heard
     of, keep what the crosscode says.

     Nothing is excluded today.  EXCLUDED_MICS is empty and the machinery
     around it is kept, because "China is out for now" was true last week and
     may be again.

Every row that falls out is kept with a reason and counted, and so is every
exchange code composites.csv does not list.  A universe that quietly shrinks
- or quietly writes half a market under a code the consumer is not looking
for - is the failure this whole module exists to prevent.

    python universe.py --self-test
"""

from __future__ import annotations

from dataclasses import dataclass, field

#  Nothing is excluded today.  This is where a market goes when it has to
#  come out, and the reporting for it is already built - see build().
EXCLUDED_MICS = ()

#  CHINA IS NOT RENAMED.  An earlier version wrote Shanghai as `600000 CG`
#  and Shenzhen as `000001 CS`, chosen by MIC.  The legacy job's own config
#  says otherwise - C1, C2, CG and CS all carry Convert2Composite=false -
#  and that config is now the one rule for every market, so a Shanghai line
#  keeps the `600000 C1` the crosscode gives it.


@dataclass(frozen=True)
class Name:
    bbg: str          # "7203 JP" - what names the FILE and the folder
    sym: str          # "7203.JP" - the qatt key
    mic: str          # "XTKS"
    rows: tuple       # every crosscode row that collapsed into this one
    source: str       # "equity_master" or "markets.csv"
    #  What names the FOLDER.  The same string as bbg: the folder and the
    #  file take one code, the composite where composites.csv says so.  The
    #  field is kept because everything downstream - the store, compare.py,
    #  the zips - is written in terms of the two, and they were once
    #  different (China's file was renamed by MIC, its folder was not).
    crosscode_bbg: str = ""


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)


def resolve_sym(row, master: dict, markets) -> tuple:
    """(sym, source).  equity_master first, the configured composite second.

    Returns ("", "") when neither can answer - the caller reports it as a
    name with no qatt key rather than inventing one.  A guessed sym does not
    fail; it silently matches nothing, and an empty file is indistinguishable
    from a name that did not trade."""
    import marketcfg

    m = master.get(row.bbg)
    if m and m.get("sym"):
        return m["sym"], "equity_master"

    comp = marketcfg.composite(row.market, markets)
    if row.ticker and comp:
        return f"{row.ticker}.{comp}", "markets.csv"
    return "", ""


def file_code(bbg: str, ticker: str, ext: str, composites) -> str:
    """What the folder and the file are called.

    Normally the crosscode's own BloombergCode - ticker and exchange code,
    `500325 IB`.  When composites.csv says that code converts, the ticker
    takes the composite instead: `500325 IN`.  The ticker is what carries,
    so a name with none keeps its code whatever the table says."""
    comp = (composites or {}).get((ext or "").strip())
    return f"{ticker} {comp}" if comp and ticker else bbg


def primary_row(rows, prim_ext: str):
    """Among the crosscode rows sharing one qatt sym, the primary listing.

    Falls back to the first row in crosscode order, which is stable between
    runs, so a name whose primary we cannot identify still gets ONE file with
    a name that does not change from day to day."""
    if prim_ext:
        for r in rows:
            if r.bbg_ext.upper() == prim_ext.upper():
                return r, True
    return rows[0], False


def build(rows, master: dict, markets, composites=None) -> tuple:
    """(names, excluded, tally).  `composites` is marketcfg.load_composites."""
    by_sym, excluded = {}, {}
    tally = {"equity_master": 0, "markets.csv": 0, "no primary match": 0,
             "written as the composite": 0, "no code in composites.csv": 0}

    def drop(reason, who):
        excluded.setdefault(reason, []).append(who)

    for row in rows:
        sym, source = resolve_sym(row, master, markets)
        if not sym:
            drop("no equity_master row and no configured composite", row.bbg)
            continue
        mic = (master.get(row.bbg) or {}).get("ID_MIC_PRIM_EXCH", "").upper()
        if mic in EXCLUDED_MICS:
            drop(f"MIC {mic}", row.bbg)
            continue
        by_sym.setdefault(sym, []).append((row, source, mic))

    names = []
    for sym, group in by_sym.items():
        group_rows = [r for r, _s, _m in group]
        #  The primary code and the MIC are properties of the NAME, so take
        #  them from whichever row equity_master answered for - a fallback
        #  row carries neither.
        prim_ext, mic = "", ""
        for r, _s, m in group:
            em = master.get(r.bbg) or {}
            prim_ext = prim_ext or em.get("EQY_PRIM_EXCH_SHRT", "")
            mic = mic or m
        chosen, matched = primary_row(group_rows, prim_ext)
        if not matched:
            tally["no primary match"] += 1
        source = next((s for _r, s, _m in group if s == "equity_master"),
                      group[0][1])
        tally[source] += 1

        bbg = file_code(chosen.bbg, chosen.ticker, chosen.bbg_ext,
                        composites)
        if bbg != chosen.bbg:
            tally["written as the composite"] += 1
        elif (chosen.bbg_ext or "").strip() not in (composites or {}):
            #  A code composites.csv has never heard of keeps the crosscode's
            #  own spelling.  Counted, because a market added upstream should
            #  be a line in that file rather than a silent default.
            tally["no code in composites.csv"] += 1

        #  THE FOLDER AND THE FILE ARE THE SAME CODE.  They differed only
        #  while China was renamed and the folder kept the crosscode's own.
        names.append(Name(bbg=bbg, sym=sym, mic=mic,
                          rows=tuple(group_rows), source=source,
                          crosscode_bbg=bbg))

    names.sort(key=lambda n: n.bbg)

    #  TWO NAMES, ONE FILE.  ticksfile.safe() drops what Windows refuses, so
    #  `HPHT* SP` is `HPHT SP` on disk - and if the crosscode also carries a
    #  real `HPHT SP` on another sym, both would write the same file, each
    #  overwriting the other, every run.  The first in code order keeps the
    #  path; the rest are excluded by name, never merged in silence.
    import ticksfile
    taken, kept = {}, []
    for n in names:
        where = (ticksfile.folder(n.crosscode_bbg), ticksfile.safe(n.bbg))
        if where in taken:
            drop(f"same file on disk as {taken[where]} once * ? etc. are "
                 f"removed", n.bbg)
            continue
        taken[where] = n.bbg
        kept.append(n)
    names = kept

    return (names,
            [Excluded(reason=k, rows=v) for k, v in sorted(excluded.items())],
            tally)


def self_test() -> int:
    import marketcfg
    from pathlib import Path
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    class Row:
        def __init__(self, bbg, ticker, ext, market):
            self.bbg, self.ticker = bbg, ticker
            self.bbg_ext, self.market = ext, market

        def __repr__(self):
            return f"Row({self.bbg})"

        def __eq__(self, other):
            return isinstance(other, Row) and self.bbg == other.bbg

    M = marketcfg.load(Path(__file__).resolve().parent / "config"
                       / "markets.csv")

    def em(sym, prim, comp, mic):
        return {"sym": sym, "EQY_PRIM_EXCH_SHRT": prim,
                "COMPOSITE_EXCH_CODE": comp, "ID_MIC_PRIM_EXCH": mic}

    tyo = Row("7203 JT", "7203", "JT", "TYO-MAIN")
    jnx = Row("7203 JE", "7203", "JE", "JNX-MAIN")
    chj = Row("7203 JI", "7203", "JI", "CHJ-MAIN")
    bhp = Row("BHP AU", "BHP", "AU", "ASX-MAIN")
    sha = Row("600000 C1", "600000", "C1", "SHA-MAIN")
    szn = Row("000001 C2", "000001", "C2", "SZA-MAIN")
    orphan = Row("ZZZ QQ", "ZZZ", "QQ", "NOWHERE-MAIN")

    MASTER = {
        "7203 JT": em("7203.JP", "JT", "JP", "XTKS"),
        "7203 JE": em("7203.JP", "JT", "JP", "XTKS"),
        "7203 JI": em("7203.JP", "JT", "JP", "XTKS"),
        "BHP AU": em("BHP.AU", "AU", "AU", "XASX"),
        "600000 C1": em("600000.CH", "C1", "CH", "XSHG"),
        "000001 C2": em("000001.CH", "C2", "CH", "XSHE")}

    print("universe --self-test\n\nresolving a crosscode row to a qatt sym")
    check("equity_master answers, and its sym is the composite one",
          resolve_sym(tyo, MASTER, M), ("7203.JP", "equity_master"))
    check("with no equity_master row, the configured composite builds it",
          resolve_sym(tyo, {}, M), ("7203.JP", "markets.csv"))
    check("a market markets.csv does not list cannot be resolved either way",
          resolve_sym(orphan, {}, M), ("", ""))
    check("and neither can a Japanese alternative venue on the fallback "
          "path - JNX-MAIN is not in markets.csv",
          resolve_sym(jnx, {}, M), ("", ""))
    check("but equity_master resolves it fine, which is why it is asked first",
          resolve_sym(jnx, MASTER, M), ("7203.JP", "equity_master"))

    print("\nchoosing the row that names the file")
    check("the primary listing wins over the alternative venues",
          primary_row([jnx, chj, tyo], "JT"), (tyo, True))
    check("order among the rows does not change the answer",
          primary_row([tyo, jnx, chj], "JT"), (tyo, True))
    check("with no primary to match, the first row is taken and the caller "
          "is told",
          primary_row([jnx, chj], "JT"), (jnx, False))
    check("and with no primary code at all",
          primary_row([jnx, chj], ""), (jnx, False))

    print("\ncollapsing venues into one name")
    names, excl, tally = build([tyo, jnx, chj, bhp], MASTER, M)
    check("three Japanese venue rows and one Australian make TWO names, "
          "not four", len(names), 2)
    check("and the Japanese one is named by its primary, not by JE or JI "
          "and not by the composite JP",
          [n.bbg for n in names], ["7203 JT", "BHP AU"])
    check("all three rows are remembered against it",
          sorted(r.bbg for r in names[0].rows),
          ["7203 JE", "7203 JI", "7203 JT"])
    check("the qatt key is the composite", names[0].sym, "7203.JP")
    check("the MIC comes along for the filter and the CSV",
          names[0].mic, "XTKS")
    check("nothing was excluded", excl, [])
    check("and both names resolved off equity_master",
          tally["equity_master"], 2)

    print("\nthe composite decides the name, from composites.csv")
    import marketcfg as _mc
    COMP = _mc.load_composites(Path(__file__).resolve().parent / "config"
                               / "composites.csv")
    check("the table holds only the codes that convert, and what they "
          "convert to", COMP, {"AT": "AU", "IB": "IN", "IS": "IN",
                               "JT": "JP"})
    check("an Australian line is written as the composite",
          file_code("RIO AT", "RIO", "AT", COMP), "RIO AU")
    check("Tokyo's too", file_code("7203 JT", "7203", "JT", COMP),
          "7203 JP")
    check("both Indian boards land on IN, and their tickers keep them apart",
          (file_code("RELIANCE IS", "RELIANCE", "IS", COMP),
           file_code("500325 IB", "500325", "IB", COMP)),
          ("RELIANCE IN", "500325 IN"))
    check("CHINA IS NOT RENAMED - the legacy config says C1 and C2 do not "
          "convert, and this table is now the only rule",
          (file_code("600000 C1", "600000", "C1", COMP),
           file_code("000001 C2", "000001", "C2", COMP)),
          ("600000 C1", "000001 C2"))
    check("a code the table does not list keeps the crosscode's own",
          file_code("700 HK", "700", "HK", COMP), "700 HK")
    check("and so does one with no ticker to build a name from",
          file_code("7203 JT", "", "JT", COMP), "7203 JT")

    names, excl, tally = build([tyo, bhp, sha, szn], MASTER, M, COMP)
    check("Tokyo is written JP and Australia AU; China keeps C1 and C2",
          [n.bbg for n in names],
          ["000001 C2", "600000 C1", "7203 JP", "BHP AU"])
    check("THE FOLDER IS THE SAME CODE AS THE FILE now that nothing is "
          "renamed by MIC",
          [n.crosscode_bbg for n in names],
          ["000001 C2", "600000 C1", "7203 JP", "BHP AU"])
    check("the conversion is counted - and BHP AU is ALREADY a composite "
          "code, so only Tokyo converts here",
          tally["written as the composite"], 1)
    check("the codes the table does not list are counted too: AU, C1, C2",
          tally["no code in composites.csv"], 3)
    check("the qatt key is untouched by any of it - it is kdb's, not the "
          "file's", sorted(n.sym for n in names),
          ["000001.CH", "600000.CH", "7203.JP", "BHP.AU"])

    print("\nthe exclusion mechanism, still there and switched off")
    check("nothing is excluded today", EXCLUDED_MICS, ())
    saved = globals()["EXCLUDED_MICS"]
    try:
        globals()["EXCLUDED_MICS"] = ("XSHG",)
        names, excl, tally = build([tyo, sha], MASTER, M)
        check("turning one back on drops it, with a reason naming it",
              ({e.reason: e.rows for e in excl}, [n.bbg for n in names]),
              ({"MIC XSHG": ["600000 C1"]}, ["7203 JT"]))
    finally:
        globals()["EXCLUDED_MICS"] = saved

    print("\nnames that cannot be resolved at all")
    names, excl, tally = build([tyo, orphan], MASTER, M)
    check("the orphan is dropped", [n.bbg for n in names], ["7203 JT"])
    check("with a reason that names it rather than a count",
          {e.reason: e.rows for e in excl},
          {"no equity_master row and no configured composite": ["ZZZ QQ"]})

    print("\nthe fallback's traffic is visible")
    names, excl, tally = build([tyo, bhp], {"BHP AU": MASTER["BHP AU"]}, M)
    check("Toyota took the config fallback, BHP took equity_master",
          (tally["markets.csv"], tally["equity_master"]), (1, 1))
    check("a name resolved by fallback carries no MIC, so nothing can "
          "rename its file or exclude it - the tally is how you notice",
          [(n.bbg, n.mic) for n in names if n.source == "markets.csv"],
          [("7203 JT", "")])

    print("\nan empty universe")
    check("is empty, not an error", build([], {}, M, COMP), ([], [], {
        "equity_master": 0, "markets.csv": 0, "no primary match": 0,
        "written as the composite": 0, "no code in composites.csv": 0}))

    print("\na * in the crosscode code")
    star = Row("HPHT* SP", "HPHT*", "SP", "SES-MAIN")
    names, excl, _ = build([star], {"HPHT* SP": em("HPHT.SP", "SP", "SP",
                                                   "XSES")}, M)
    check("the folder is still the crosscode's own BloombergCode",
          names[0].crosscode_bbg, "HPHT* SP")
    plain = Row("HPHT SP", "HPHT", "SP", "SES-MAIN")
    names, excl, _ = build(
        [star, plain],
        {"HPHT* SP": em("HPHTX.SP", "SP", "SP", "XSES"),
         "HPHT SP": em("HPHT.SP", "SP", "SP", "XSES")}, M)
    check("two syms that would land on ONE file keep one name, not two "
          "overwriting each other", [n.bbg for n in names], ["HPHT SP"])
    check("and the other is excluded, named",
          [(e.reason.startswith("same file on disk"), e.rows) for e in excl],
          [(True, ["HPHT* SP"])])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
