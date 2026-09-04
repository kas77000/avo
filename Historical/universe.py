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

THREE RENAME.  A Chinese stock is spelt three different ways and this is
     where the third is made.  Shanghai's 600000 is `600000 C1` in the
     crosscode, `600000.CH` in kdb, and `raw-600000 CG-...` on disk.  The MIC
     decides which suffix - XSHG takes CG, XSHE takes CS - and the MIC is not
     one of the crosscode's seven columns, so this cannot happen until
     equity_master has answered.

     Nothing is excluded today.  EXCLUDED_MICS is empty and the machinery
     around it is kept, because "China is out for now" was true last week and
     may be again.

Every row that falls out is kept with a reason and counted, and so is every
row whose file could NOT be renamed.  A universe that quietly shrinks - or
quietly writes half its Chinese names under the wrong code - is the failure
this whole module exists to prevent.

    python universe.py --self-test
"""

from __future__ import annotations

from dataclasses import dataclass, field

#  Nothing is excluded today.  This is where a market goes when it has to
#  come out, and the reporting for it is already built - see build().
EXCLUDED_MICS = ()

#  SHANGHAI AND SHENZHEN ARE NAMED DIFFERENTLY ON DISK.  A Shanghai line is
#  `600000 C1` in the crosscode and `600000.CH` in kdb, but the file it
#  writes is `raw-600000 CG-...`.  Three spellings of one stock, and this is
#  the third.
#
#  The MIC decides, not the Fidessa market: SHA/SHH/SSC/SZA/SHZ/SZC do not
#  say on their face which side of the border they are, and guessing wrong
#  mislabels every Chinese file with nothing in the output to show for it.
#  A name with no MIC therefore keeps its crosscode code and is counted.
MIC_FILE_EXT = {"XSHG": "CG", "XSHE": "CS"}


@dataclass(frozen=True)
class Name:
    bbg: str          # "7203 JT" - the primary, and what names the file
    sym: str          # "7203.JP" - the qatt key
    mic: str          # "XTKS"
    rows: tuple       # every crosscode row that collapsed into this one
    source: str       # "equity_master" or "markets.csv"


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


def file_code(bbg: str, ticker: str, mic: str) -> str:
    """What the file is called.

    Normally the crosscode's own BloombergCode - ticker and primary exchange
    code, `7203 JT`.  Shanghai and Shenzhen are the exception: their primary
    codes are C1 and C2 and the consumer expects CG and CS, so the ticker
    takes those instead."""
    ext = MIC_FILE_EXT.get((mic or "").upper())
    return f"{ticker} {ext}" if ext and ticker else bbg


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


def build(rows, master: dict, markets) -> tuple:
    """(names, excluded, tally)."""
    by_sym, excluded = {}, {}
    tally = {"equity_master": 0, "markets.csv": 0, "no primary match": 0,
             "renamed by MIC": 0, "china without a MIC": 0}

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

        bbg = file_code(chosen.bbg, chosen.ticker, mic)
        if bbg != chosen.bbg:
            tally["renamed by MIC"] += 1
        elif sym.endswith(".CH"):
            #  A China sym with no MIC to rename it: the file keeps C1 or C2
            #  and is not what the consumer is looking for.  Loud in the
            #  tally rather than silent on disk.
            tally["china without a MIC"] += 1

        names.append(Name(bbg=bbg, sym=sym, mic=mic,
                          rows=tuple(group_rows), source=source))

    names.sort(key=lambda n: n.bbg)
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

    print("\nChina: in, and named a third way")
    check("Shanghai's file takes CG, not the crosscode's C1",
          file_code("600000 C1", "600000", "XSHG"), "600000 CG")
    check("Shenzhen's takes CS, not C2",
          file_code("000001 C2", "000001", "XSHE"), "000001 CS")
    check("everywhere else keeps the crosscode's own code",
          file_code("7203 JT", "7203", "XTKS"), "7203 JT")
    check("a Hong Kong name is untouched - Stock Connect lines are XHKG, "
          "and renaming them would be wrong",
          file_code("700 HK", "700", "XHKG"), "700 HK")
    check("no MIC, no rename - the file keeps C1 rather than taking a "
          "guessed suffix",
          file_code("600000 C1", "600000", ""), "600000 C1")

    names, excl, tally = build([tyo, bhp, sha, szn], MASTER, M)
    check("all four names are fetched - nothing is excluded any more",
          [n.bbg for n in names],
          ["000001 CS", "600000 CG", "7203 JT", "BHP AU"])
    check("nothing was excluded", excl, [])
    check("but both are still looked up on the composite sym, which is "
          "what kdb keys them by",
          sorted(n.sym for n in names if n.sym.endswith(".CH")),
          ["000001.CH", "600000.CH"])
    check("and the renames are counted", tally["renamed by MIC"], 2)

    print("\nChina with no equity_master row")
    names, excl, tally = build([sha], {}, M)
    check("markets.csv still builds the right kdb key from the composite",
          [n.sym for n in names], ["600000.CH"])
    check("but with no MIC the file keeps C1, which is NOT what the "
          "consumer wants",
          [n.bbg for n in names], ["600000 C1"])
    check("so it is counted, loudly", tally["china without a MIC"], 1)

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
    check("is empty, not an error", build([], {}, M), ([], [], {
        "equity_master": 0, "markets.csv": 0, "no primary match": 0,
        "renamed by MIC": 0, "china without a MIC": 0}))

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
