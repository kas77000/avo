#!/usr/bin/env python3
"""The universe: which instruments we owe a limit for today.

Two files and a clock.  config/markets.csv says which venues exist and when
each one's data is ready; CrossCode.csv says what trades on them.

SIX FILTERS, in this order:

  security type      Equity and ETF only
  configured venue   a FidessaMarket we know
  cutoff             a venue whose Time has passed
  foreign ticker     Thailand only: no /F or /Q line
  static limit       India only: not a name the ATS strategy file configures
  BloombergStatus    ACTV only, applied after deduplication

THE LAST TWO ARE ONE MARKET EACH, and each is why that market was out of
scope until somebody wrote it.

  THAILAND lists the same company three ways - the local line, the foreign
  board (/F) and the NVDR (/Q) - and Nova trades the local one.  Publishing
  a band for the other two would put limits on instruments the ATS has no
  order flow for.  (LimitUpDown.r:178-184)

  INDIA has names whose limits are configured inside the ATS strategy file
  named by the venue's ExcludeFile column.  For those, Nova's own config is
  the authority, and a published row would OVERRIDE a number a person set.
  So they are dropped here rather than priced.  (LimitUpDown.r:112-152)

  The mnemonic is matched with its first two characters stripped, which is
  what R's sub("..", "", Mnemo) does - the crosscode prefixes a country
  code the strategy file does not carry.

THE ACTV FILTER IS THE ONE WE ALREADY HAD THE DATA FOR.  CrossCode carries
BloombergStatus, and dedupe below already trusts it to choose between two
rows claiming one code.  Trusting it once more - to drop a delisted name
that happens to be the ONLY claimant of its code - costs one comparison and
removes v2's dependency on Bloomberg's MARKET_STATUS, which is a STATIC
field and may not be served to a datafeed entitlement at all.

A BLANK status is NOT a status of 'not active'.  It means CrossCode has no
opinion, so the row is kept - the same rule bpipe.band_from applies to a
field Bloomberg did not serve.  A MISSING COLUMN is different and fatal: it
would silently empty the universe, so it is refused at the header.

THE CUTOFF IS CUMULATIVE BY TIME OF DAY.  Each run rewrites the WHOLE output
with only the venues that are open, so an 07:30 run publishes Japan and
Korea and the 09:03 run republishes those and adds the rest.  Changing that
silently would strand a market.

DEDUPLICATION PREFERS THE LIVE LISTING.  A repeated BloombergCode is settled
by BloombergStatus - the ACTV row wins - and only falls back to "keep the
first" when that does not settle it.  Both halves matter: the fallback alone
would sometimes keep a delisted line over a live one.

Nothing is dropped quietly: every filter returns what it removed.

This module does not care where a venue's band comes from.  It builds the
universe; marketcfg.Config.by_source splits it afterwards into the names
Bloomberg prices and the names we compute.

THREE COLUMNS RIDE ALONG UNUSED BY ANY FILTER - VenueList, BSEBloombergCode
and BSERic.  They are how a row says "this company is also reachable on the
BSE, under this other code", a listing with no crosscode line of its own.
india.secondary_rows turns them into rows; nothing else reads them, and a
crosscode without the columns simply has none to offer.

    python crosscode.py --self-test
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

KEEP_TYPES = ("Equity", "ETF")
STATUS_COLUMN = "BloombergStatus"
ACTIVE_STATUS = ("ACTV",)

#  Thailand's foreign board and NVDR lines, which Nova does not trade.
#  R greps the same two on the BloombergCode: LimitUpDown.r:180.
THAI_VENUE = "SET-MAIN"
FOREIGN_TICKER = re.compile(r"/F|/Q")

#  The crosscode prefixes its Mnemo with a two character country code that
#  the ATS strategy file does not carry.  R: sub("..", "", Mnemo).
MNEMO_PREFIX = 2


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Row:
    ric: str
    bbg: str
    ticker: str          # the code without its exchange: '600001 CG' -> '600001'
    security: str        # the Bloomberg identifier: "7203 JT Equity"
    fidessa_code: str
    venue_id: str
    status: str
    #  The BSE listing this row also stands for, when it has one.  Read but
    #  never filtered on here - see india.secondary_rows.
    venue_list: str = ""
    bse_bbg: str = ""
    bse_ric: str = ""


@dataclass(frozen=True)
class Dropped:
    """One name that did not make the file.

    Carries the BLOOMBERG code as well as the RIC.  A report that says only
    "412 no MIN_LIMIT" cannot be acted on: the code you paste into a
    terminal to check the name by hand is the Bloomberg one, and the venue
    is what tells you a whole market has gone missing rather than a
    scattering of names."""
    ric: str
    bbg: str = ""
    venue_id: str = ""
    detail: str = ""

    def __str__(self) -> str:
        shown = f"{self.ric} ({self.bbg or '?'})"
        return f"{shown} {self.detail}" if self.detail else shown


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)

    def by_venue(self) -> dict:
        out = {}
        for d in self.rows:
            out.setdefault(d.venue_id or "(unknown)", []).append(d)
        return out


def security_name(bbg: str) -> str:
    """'7203 JT' -> '7203 JT Equity', the identifier Bloomberg wants."""
    bbg = (bbg or "").strip()
    if not bbg:
        return ""
    if bbg.upper().endswith(" EQUITY"):
        return bbg
    return f"{bbg} Equity"


def ticker_of(bbg: str) -> str:
    """'600001 CG' -> '600001'.  Only a computed venue uses this, to match a
    tier's SymPrefix."""
    return (bbg or "").rsplit(" ", 1)[0]


def mnemo_key(mnemo: str) -> str:
    """'INRELIANCE' -> 'RELIANCE', the form an ATS strategy file uses.

    R's sub("..", "", Mnemo) strips the first two characters, a country code
    the strategy file does not carry.  A mnemonic shorter than that yields
    the empty string and matches nothing, which is right: there is no name
    there to exclude."""
    return (mnemo or "")[MNEMO_PREFIX:]


def dedupe(rows):
    """Settle a BloombergCode claimed by more than one row.

    ONE index of rows to drop, from whichever branch is non-empty:

      some row in a duplicated group is not ACTV
          -> drop EVERY non-ACTV row in that group.  If none of them is
             ACTV that drops the whole group and the code is published by
             nobody.  Not an oversight worth 'fixing': a band taken off a
             delisted line is worse than no band, and Nova tolerates a
             missing name.
      every duplicate is ACTV
          -> drop the later ones, keep the first.

    A code that appears once is never touched by either."""
    groups = {}
    for r in rows:
        groups.setdefault(r.bbg, []).append(r)
    duplicated = {code for code, g in groups.items() if len(g) > 1}

    #  First branch: every non-ACTV row of a duplicated code.
    doomed = {id(r) for r in rows
              if r.bbg in duplicated and r.status.upper() != "ACTV"}
    if not doomed:
        #  Second branch: the later occurrences only.
        seen = set()
        for r in rows:
            if r.bbg in seen:
                doomed.add(id(r))
            seen.add(r.bbg)

    kept = [r for r in rows if id(r) not in doomed]
    removed = [Dropped(ric=r.ric, bbg=r.bbg, venue_id=r.venue_id)
               for r in rows if id(r) in doomed]
    return kept, removed


def load(path, venues: dict, now: time, strat=None):
    """The universe, and everything that did not make it.

    `strat` is {venue_id: set of mnemonics whose limits the ATS configures},
    from india.strategy_lists.  Empty or absent means no venue has one and
    the filter never fires - the state of every market but India."""
    strat = strat or {}
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path} does not exist")

    kept = []
    by_reason = {}

    def drop(reason, ric, bbg="", venue_id=""):
        by_reason.setdefault(reason, []).append(
            Dropped(ric=ric, bbg=bbg, venue_id=venue_id))

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or STATUS_COLUMN not in reader.fieldnames:
            #  Fatal, not a warning.  Without the column every row reads as
            #  'no opinion', the ACTV filter passes everything, and the only
            #  symptom is delisted names quietly getting a band.
            raise ConfigError(
                f"{path} has no {STATUS_COLUMN} column - that column IS the "
                f"ACTV filter, and without it every name looks alive")
        for r in reader:
            ric = (r.get("RicCode") or "").strip()
            sec_type = (r.get("Type") or "").strip()
            venue_id = (r.get("FidessaMarket") or "").strip()
            #  read up front: every drop below should be able to name the
            #  Bloomberg code, not just the RIC
            bbg = (r.get("BloombergCode") or "").strip()
            if sec_type not in KEEP_TYPES:
                drop("security type not Equity/ETF", ric, bbg, venue_id)
                continue
            venue = venues.get(venue_id)
            if venue is None:
                #  Named, not merely counted.  This line IS how a market
                #  that nobody configured announces itself - it is what
                #  would have said "TYO-MAIN" the first time Japan was
                #  looked for.  One report line per unknown venue.
                drop(f"venue {venue_id or '(blank)'} not in markets.csv", ric,
                     bbg, venue_id)
                continue
            if now < venue.cutoff:
                drop("cutoff not reached", ric, bbg, venue_id)
                continue
            if not bbg:
                drop("no BloombergCode to ask Bloomberg about", ric, bbg,
                     venue_id)
                continue
            if venue_id == THAI_VENUE and FOREIGN_TICKER.search(bbg):
                #  The foreign board and the NVDR, which Nova does not
                #  trade.  Dropped here rather than at the Bloomberg fetch
                #  so they are counted with every other exclusion and never
                #  paid for in a request.
                drop("Thai foreign or NVDR line, not the local one", ric,
                     bbg, venue_id)
                continue
            configured = strat.get(venue_id)
            if configured and mnemo_key(
                    (r.get("Mnemo") or "").strip()) in configured:
                drop("limit is configured in the ATS strategy file", ric,
                     bbg, venue_id)
                continue
            kept.append(Row(ric=ric, bbg=bbg, ticker=ticker_of(bbg),
                            security=security_name(bbg),
                            fidessa_code=(r.get("FidessaCode") or "").strip(),
                            venue_id=venue_id,
                            status=(r.get("BloombergStatus") or "").strip(),
                            venue_list=(r.get("VenueList") or "").strip(),
                            bse_bbg=(r.get("BSEBloombergCode") or "").strip(),
                            bse_ric=(r.get("BSERic") or "").strip()))

    kept, deduped = dedupe(kept)
    if deduped:
        by_reason["duplicate BloombergCode"] = deduped

    #  AFTER dedupe, deliberately.  Dedupe's job is to pick the live row out
    #  of a group claiming one code; this drops what is left over - a name
    #  that is not ACTV and had no competitor to lose to.  Run the other way
    #  round, dedupe would never see a non-ACTV row and its preference would
    #  be dead code.
    live = []
    for r in kept:
        status = r.status.strip()
        if not status or status.upper() in ACTIVE_STATUS:
            live.append(r)
        else:
            drop(f"BloombergStatus is {status}, not ACTV", r.ric, r.bbg,
                 r.venue_id)
    kept = live

    if not kept and not by_reason:
        raise ConfigError(f"{path} is empty")

    return kept, [Excluded(reason=k, rows=v)
                  for k, v in sorted(by_reason.items())]


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile

    import marketcfg
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except ConfigError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    print("crosscode --self-test\n\nbuilding the bloomberg identifier")
    check("a code becomes a security", security_name("7203 JT"),
          "7203 JT Equity")
    check("one that already says Equity is left alone",
          security_name("7203 JT Equity"), "7203 JT Equity")
    check("case does not fool it", security_name("7203 JT equity"),
          "7203 JT equity")
    check("nothing in, nothing out", security_name(""), "")

    print("\nstripping the country code off a crosscode mnemonic")
    check("the ATS strategy file spells it without one",
          mnemo_key("INRELIANCE"), "RELIANCE")
    check("a mnemonic too short to have one matches nothing",
          mnemo_key("IN"), "")
    check("nothing in, nothing out", mnemo_key(""), "")

    print("\nsplitting the code into a ticker, for a computed venue's tiers")
    check("the code without its exchange", ticker_of("600001 CG"), "600001")
    check("a ticker with a space keeps everything but the last word",
          ticker_of("BRK/A US Equity"), "BRK/A US")
    check("no space at all", ticker_of("ABC"), "ABC")

    HDR = ("RicCode,BloombergCode,FidessaCode,FidessaMarket,Type,"
           "BloombergStatus\n")

    def venue(vid, cutoff):
        return marketcfg.Venue(country="X", venue_id=vid, cutoff=cutoff,
                               source="bloomberg", tick_source="",
                               min_price=None, rounding="none")

    V = {"TYO-MAIN": venue("TYO-MAIN", time(7, 30)),
         "SHA-MAIN": venue("SHA-MAIN", time(9, 3))}
    IN_TH = {"SET-MAIN": venue("SET-MAIN", time(10, 39)),
             "NSI-MAIN": venue("NSI-MAIN", time(10, 49)),
             "BSE-MAIN": venue("BSE-MAIN", time(10, 49))}

    print("\nfiltering the universe")
    BODY = ("7203.T,7203 JT,7203.JP,TYO-MAIN,Equity,ACTV\n"
            "600001.SS,600001 CG,600001.CN,SHA-MAIN,Equity,ACTV\n"
            "9999.T,9999 JT,9999.JP,TYO-MAIN,Warrant,ACTV\n"
            "ABC.KS,ABC KP,ABC.KR,KSC-MAIN,Equity,ACTV\n")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR + BODY, encoding="utf-8")

        rows, excl = load(cc, V, time(10, 0))
        check("both open venues, equities only",
              sorted(r.ric for r in rows), ["600001.SS", "7203.T"])
        check("and each carries the identifier Bloomberg wants",
              sorted(r.security for r in rows),
              ["600001 CG Equity", "7203 JT Equity"])
        reasons = {e.reason: e.rows for e in excl}
        check("the warrant is reported, not vanished",
              [d.ric for d in reasons["security type not Equity/ETF"]],
          ["9999.T"])
        check("the venue nobody configured is named, so one report line "
              "says which market is missing rather than just how many rows",
              [d.ric for d in
               reasons["venue KSC-MAIN not in markets.csv"]], ["ABC.KS"])

        rows, excl = load(cc, V, time(8, 30))
        check("at 08:30 Shanghai has not opened yet",
              [r.ric for r in rows], ["7203.T"])
        check("and says so", [d.ric for d in {e.reason: e.rows for e in
              excl}["cutoff not reached"]], ["600001.SS"])

        rows, _ = load(cc, V, time(7, 30))
        check("a cutoff is reached AT its time, not after",
              [r.ric for r in rows], ["7203.T"])

        rows, _ = load(cc, V, time(6, 0))
        check("before every cutoff, nothing is published", rows, [])

    print("\nThailand: the foreign board and the NVDR are not the local line")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR +
                      "PTT.BK,PTT TB,PTT.TH,SET-MAIN,Equity,ACTV\n"
                      "PTTf.BK,PTT/F TB,PTT.TH,SET-MAIN,Equity,ACTV\n"
                      "PTTn.BK,PTT/Q TB,PTT.TH,SET-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, excl = load(cc, IN_TH, time(11, 0))
        check("only the local line is published - Nova has no order flow "
              "for the other two, and a band on them is a band on an "
              "instrument nobody trades",
              [r.ric for r in rows], ["PTT.BK"])
        check("and both are named, not silently gone",
              [d.ric for d in {e.reason: e.rows for e in excl}[
                  "Thai foreign or NVDR line, not the local one"]],
              ["PTTf.BK", "PTTn.BK"])

        cc.write_text(HDR + "A.T,A/F JT,A.JP,TYO-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(11, 0))
        check("the filter is Thailand's alone - a slash in a Japanese code "
              "is somebody else's share class, not an NVDR",
              [r.ric for r in rows], ["A.T"])

    print("\nIndia: names whose limit the ATS strategy file already sets")
    IHDR = ("RicCode,BloombergCode,FidessaCode,FidessaMarket,Type,"
            "BloombergStatus,Mnemo\n")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(IHDR +
                      "RELI.NS,RIL IN,RELIANCE.IN,NSI-MAIN,Equity,ACTV,"
                      "INRELIANCE\n"
                      "TCS.NS,TCS IN,TCS.IN,NSI-MAIN,Equity,ACTV,INTCS\n"
                      "RELI.BO,500325 IB,RELIANCE.IN,BSE-MAIN,Equity,ACTV,"
                      "INRELIANCE\n", encoding="utf-8")
        rows, excl = load(cc, IN_TH, time(11, 0),
                          {"NSI-MAIN": {"RELIANCE"}})
        check("a name the strategy file configures gets NO published limit "
              "- one would override the number a person set",
              sorted(r.ric for r in rows), ["RELI.BO", "TCS.NS"])
        check("and it is named, with its venue",
              [(d.ric, d.venue_id) for d in {e.reason: e.rows for e in excl}[
                  "limit is configured in the ATS strategy file"]],
              [("RELI.NS", "NSI-MAIN")])
        check("the exclusion is per venue: the same company on the OTHER "
              "exchange is untouched unless that exchange's own file lists "
              "it",
              [r.ric for r in load(cc, IN_TH, time(11, 0),
                                   {"BSE-MAIN": {"RELIANCE"}})[0]
               if r.venue_id == "BSE-MAIN"], [])
        rows, _ = load(cc, IN_TH, time(11, 0), None)
        check("no strategy list at all means the filter never fires, which "
              "is every market but India", len(rows), 3)
        rows, _ = load(cc, IN_TH, time(11, 0), {"NSI-MAIN": {"INRELIANCE"}})
        check("the match is on the mnemonic WITHOUT its country code - the "
              "raw crosscode value matches nobody", len(rows), 3)

    print("\nthe BSE listing a row carries for somebody else")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(
            "RicCode,BloombergCode,FidessaCode,FidessaMarket,Type,"
            "BloombergStatus,VenueList,BSEBloombergCode,BSERic\n"
            "RELI.NS,RIL IN,RELIANCE.IN,NSI-MAIN,Equity,ACTV,"
            "NSI-MAIN|BSE-SECONDARY,500325 IB,RELI.BO\n",
            encoding="utf-8")
        rows, _ = load(cc, IN_TH, time(11, 0))
        check("the three columns ride along, unfiltered, for india.py",
              (rows[0].venue_list, rows[0].bse_bbg, rows[0].bse_ric),
              ("NSI-MAIN|BSE-SECONDARY", "500325 IB", "RELI.BO"))
        cc.write_text(HDR + "A.T,A JT,A.JP,TYO-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(11, 0))
        check("a crosscode without the columns simply has none to offer",
              (rows[0].venue_list, rows[0].bse_bbg, rows[0].bse_ric),
              ("", "", ""))

    print("\ndeduplicating a repeated bloomberg code")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR +
                      "A.T,DUP JT,A.JP,TYO-MAIN,Equity,DLST\n"
                      "B.T,DUP JT,B.JP,TYO-MAIN,Equity,ACTV\n"
                      "C.T,SOLO JT,C.JP,TYO-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, excl = load(cc, V, time(10, 0))
        check("the ACTV row wins even though it is second - keeping the "
              "first would publish a delisted line",
              sorted(r.ric for r in rows), ["B.T", "C.T"])
        check("and the loser is named",
              [d.ric for d in {e.reason: e.rows for e in
                               excl}["duplicate BloombergCode"]], ["A.T"])

        cc.write_text(HDR +
                      "A.T,DUP JT,A.JP,TYO-MAIN,Equity,ACTV\n"
                      "B.T,DUP JT,B.JP,TYO-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(10, 0))
        check("when both are ACTV the first one keeps the code",
              [r.ric for r in rows], ["A.T"])

        cc.write_text(HDR +
                      "A.T,DUP JT,A.JP,TYO-MAIN,Equity,DLST\n"
                      "B.T,DUP JT,B.JP,TYO-MAIN,Equity,DLST\n"
                      "C.T,SOLO JT,C.JP,TYO-MAIN,Equity,ACTV\n",
                      encoding="utf-8")
        rows, excl = load(cc, V, time(10, 0))
        check("when NONE of them is ACTV the code is published by nobody - "
              "the whole group goes, and a band off a delisted line is "
              "worse than no band",
              [r.ric for r in rows], ["C.T"])
        check("both losers named",
              [d.ric for d in {e.reason: e.rows for e in
                               excl}["duplicate BloombergCode"]],
              ["A.T", "B.T"])

        cc.write_text(HDR +
                      "A.T,DUP JT,A.JP,TYO-MAIN,Equity,DLST\n"
                      "B.T,DUP JT,B.JP,TYO-MAIN,Equity,ACTV\n"
                      "C.T,DUP JT,C.JP,TYO-MAIN,Equity,DLST\n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(10, 0))
        check("three rows, one ACTV: every non-ACTV goes, not just the "
              "later ones", [r.ric for r in rows], ["B.T"])

    print("\nthe ACTV filter, which is why v2 needs no MARKET_STATUS")
    with tempfile.TemporaryDirectory() as d:
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR +
                      "A.T,A JT,A.JP,TYO-MAIN,Equity,ACTV\n"
                      "B.T,B JT,B.JP,TYO-MAIN,Equity,DLST\n",
                      encoding="utf-8")
        rows, excl = load(cc, V, time(10, 0))
        check("a delisted name with NOBODY to lose a duplicate to is still "
              "dropped - the case dedupe alone never saw",
              [r.ric for r in rows], ["A.T"])
        check("and it is named by its status, not miscounted as a duplicate",
              [d.ric for d in {e.reason: e.rows for e in excl}[
                  "BloombergStatus is DLST, not ACTV"]], ["B.T"])
        d = {e.reason: e.rows for e in excl}[
            "BloombergStatus is DLST, not ACTV"][0]
        check("a dropped name carries its BLOOMBERG code, which is what "
              "you paste into a terminal to check it by hand",
              (d.bbg, d.venue_id), ("B JT", "TYO-MAIN"))
        check("and prints as both codes together",
              str(d), "B.T (B JT)")

        cc.write_text(HDR +
                      "A.T,A JT,A.JP,TYO-MAIN,Equity,ACTV\n"
                      "B.T,B JT,B.JP,TYO-MAIN,Equity,\n"
                      "C.T,C JT,C.JP,TYO-MAIN,Equity,  \n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(10, 0))
        check("a BLANK status is 'no opinion', not 'not active' - otherwise "
              "one empty column empties the whole published file",
              sorted(r.ric for r in rows), ["A.T", "B.T", "C.T"])

        cc.write_text(HDR + "A.T,A JT,A.JP,TYO-MAIN,Equity,actv\n",
                      encoding="utf-8")
        rows, _ = load(cc, V, time(10, 0))
        check("case does not decide whether a name trades",
              [r.ric for r in rows], ["A.T"])

    print("\nfiles that must be refused")
    with tempfile.TemporaryDirectory() as d:
        raises("a crosscode that is not there",
               lambda: load(Path(d) / "nope.csv", V, time(10, 0)),
               "does not exist")
        cc = Path(d) / "CrossCode.csv"
        cc.write_text(HDR, encoding="utf-8")
        raises("a crosscode with no rows at all", lambda: load(cc, V,
                                                              time(10, 0)),
               "is empty")
        nostatus = Path(d) / "NoStatus.csv"
        nostatus.write_text(
            "RicCode,BloombergCode,FidessaCode,FidessaMarket,Type\n"
            "A.T,A JT,A.JP,TYO-MAIN,Equity\n", encoding="utf-8")
        raises("a crosscode with no BloombergStatus column - it would pass "
               "every name through the ACTV filter, and the only symptom "
               "would be delisted names quietly getting a band",
               lambda: load(nostatus, V, time(10, 0)),
               "has no BloombergStatus column")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
