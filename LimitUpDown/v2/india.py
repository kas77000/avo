#!/usr/bin/env python3
"""India: the two things a bare config row would get wrong.

India is not one venue with a Bloomberg limit on it.  It is two exchanges,
a set of names that must NOT be given a limit at all, and a third venue
whose rows exist in no crosscode line of their own.

THE STATIC LIMIT EXCLUSION.  Some Indian names have their limits configured
inside the ATS strategy files - in-nse_drv.stra and in-bse_drv.stra - and
for those, Nova's own configuration is the authority.  Publishing a
limitUpDown row for one would OVERRIDE a number a person set deliberately,
which is worse than publishing nothing.  So the strategy file is read and
every name it lists is removed from the universe.

    R: KeepOnlyStaticLimitIndia, LimitUpDown.r:112-152

The name of that R function reads backwards until you see it from the ATS's
side: the ATS keeps its own static limit, so this job must not send one.

A STRATEGY FILE THAT WILL NOT READ IS FATAL, exactly as R's stop() makes it.
The failure is silent in the dangerous direction - an empty exclusion list
looks identical to a run where nobody was configured, and the symptom is
names quietly receiving a limit that overrides the desk's.

    read.csv(file, header = F, sep = " ", skip = 8)   ->  STRAT_SKIP
    as.character(unique(stratNSI[, "V2"]))            ->  STRAT_FIELD

WHERE THE PATH LIVES: the ExcludeFile column of config/markets.csv, one per
venue, beside the venue's cutoff and its source.  R keeps StraNSI and
StraBSE in config_cash.xml, a file away from the venue list; here the venue
row carries everything about the venue.

Whitespace-separated and read with str.split(), not a single space: the same
lesson TradingData's caslist.py learned from these files.  R's sep=" " would
turn a double space into an empty field; splitting on runs of whitespace
cannot.

THE BSE SECONDARY VENUE.  A name listed on the NSE may also be reachable on
the BSE under a different Bloomberg code and a different RIC, and the
crosscode says so in three columns of the SAME row - VenueList naming
BSE-SECONDARY, plus BSEBloombergCode and BSERic.  There is no separate line
to pick up, so those rows are synthesised here and published TWICE, once as
BSE-MAIN and once as BSE-SECONDARY, off one set of Bloomberg limits.

    R: CreateLimitSecondaryVenueIndia, LimitUpDown.r:387-414

A code the crosscode ALREADY carries in its own BloombergCode column is not
synthesised - it is priced by the ordinary path instead, and doing both
would publish the same name twice under one venue.

ONE DELIBERATE DIFFERENCE FROM R.  R builds the secondary list from the
crosscode as read; this builds it from the universe as filtered, so a
secondary listing is never derived from a line that lost a duplicate or is
not ACTV.  The rule is already this file's: a band off a delisted line is
worse than no band.

    python india.py --self-test
"""

from __future__ import annotations

from pathlib import Path

import crosscode

#  The venues whose universe the strategy files prune.  Two, hardcoded,
#  because R hardcodes two - this is not a mechanism other markets can opt
#  into, it is India's ATS configuration.
NSE_VENUE = "NSI-MAIN"
BSE_VENUE = "BSE-MAIN"
STATIC_LIMIT_VENUES = (NSE_VENUE, BSE_VENUE)

#  The .stra file's preamble, and which whitespace-separated field carries
#  the mnemonic.  R: skip = 8, then column V2 - the second.
STRAT_SKIP = 8
STRAT_FIELD = 1

#  The value VenueList must contain for a row to carry a BSE listing, and
#  the venue name the duplicated output row is published under.
SECONDARY_MARKER = "BSE-SECONDARY"
SECONDARY_VENUE = "BSE-SECONDARY"


class IndiaError(Exception):
    pass


# =============================================================================
# THE STATIC LIMIT EXCLUSION
# =============================================================================

def parse_strategy(text: str):
    """The mnemonics an ATS strategy file configures a static limit for.

    Eight lines of preamble, then whitespace-separated records whose second
    field is the mnemonic.  A short line is skipped rather than fatal - R
    reads these with header = F and a malformed tail row has never been the
    question this file answers."""
    out = set()
    for line in text.splitlines()[STRAT_SKIP:]:
        fields = line.split()
        if len(fields) <= STRAT_FIELD:
            continue
        name = fields[STRAT_FIELD].strip()
        if name:
            out.add(name)
    return out


def read_strategy(path, venue_id: str):
    """One strategy file -> its mnemonics.  Every failure is fatal.

    R stops on each of these, and it is right to: an unreadable file yields
    an EMPTY exclusion list, which is indistinguishable from a file that
    configures nobody, and the only symptom is Indian names quietly getting
    a published limit that overrides the one the desk set."""
    if not str(path or "").strip():
        raise IndiaError(
            f"{venue_id} is in markets.csv and has reached its cutoff, but "
            f"its ExcludeFile column in markets.csv is blank. Fill it in "
            f"- without the file every name whose limit the ATS configures "
            f"would be published one anyway, overriding it.")
    p = Path(path)
    if not p.is_file():
        raise IndiaError(f"{venue_id}: strategy file {p} does not exist")
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        raise IndiaError(f"{venue_id}: cannot read strategy file {p}: {e}")
    names = parse_strategy(text)
    if not names:
        raise IndiaError(
            f"{venue_id}: strategy file {p} lists no names after its "
            f"{STRAT_SKIP} line preamble. An empty exclusion list looks "
            f"exactly like a working one, so this is refused rather than "
            f"published around.")
    return names


def strategy_lists(venue_ids, paths):
    """venue_ids in scope + {venue_id: ExcludeFile} -> {venue_id: mnemonics}.

    ONLY THE VENUES IN SCOPE ARE READ, which is what makes a blank
    ExcludeFile harmless at 07:30 and fatal at 10:49.  R does the same,
    keying on whether the venue survived its own cutoff filter.  It also
    means the file is not touched on a run that could not use it, so an
    India share being down cannot take out Japan."""
    out = {}
    for vid in STATIC_LIMIT_VENUES:
        if vid not in set(venue_ids):
            continue
        out[vid] = read_strategy(paths.get(vid), vid)
    return out


# =============================================================================
# THE BSE SECONDARY VENUE
# =============================================================================

def secondary_rows(rows):
    """The universe -> the BSE listings that have no crosscode line of their
    own.

    Three filters, all R's:

      VenueList names BSE-SECONDARY, and the row carries a BSE code
      the code is not ALREADY somebody's own BloombergCode
      one row per code

    The result is priced by the ordinary Bloomberg path, under BSE-MAIN;
    publish_both() below is what turns each priced row into two."""
    own = {r.bbg for r in rows}
    out, seen = [], set()
    for r in rows:
        if SECONDARY_MARKER not in r.venue_list:
            continue
        if not r.bse_bbg or not r.bse_ric:
            continue
        if r.bse_bbg in own or r.bse_bbg in seen:
            continue
        seen.add(r.bse_bbg)
        out.append(crosscode.Row(
            ric=r.bse_ric, bbg=r.bse_bbg,
            ticker=crosscode.ticker_of(r.bse_bbg),
            security=crosscode.security_name(r.bse_bbg),
            fidessa_code=r.fidessa_code, venue_id=BSE_VENUE,
            status=r.status))
    return out


def publish_both(priced):
    """Priced BSE-MAIN rows -> the same rows again under BSE-SECONDARY.

    Returns only the copies; the caller keeps the originals.  One set of
    limits, two venues, which is what R writes."""
    return [dict(r, Venue=SECONDARY_VENUE) for r in priced]


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
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
        except IndiaError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    PREAMBLE = "".join(f"# preamble line {n}\n" for n in range(1, 9))
    BODY = ("1 RELIANCE something else\n"
            "2 TCS something else\n"
            "3 INFY something else\n")

    print("india --self-test\n\nreading an ATS strategy file")
    check("the second field of every record past the preamble",
          sorted(parse_strategy(PREAMBLE + BODY)),
          ["INFY", "RELIANCE", "TCS"])
    check("the preamble is not data - eight lines, whatever they say",
          parse_strategy("RELIANCE\n" * 8 + "1 TCS x\n"), {"TCS"})
    check("runs of whitespace collapse, which R's sep=' ' would not - a "
          "double space there makes an empty second field and the whole "
          "exclusion list comes back wrong",
          parse_strategy(PREAMBLE + "1   TCS   x\n"), {"TCS"})
    check("a tab is whitespace too",
          parse_strategy(PREAMBLE + "1\tTCS\tx\n"), {"TCS"})
    check("a line too short to have a second field is skipped, not fatal",
          parse_strategy(PREAMBLE + "1\n" + "2 TCS x\n"), {"TCS"})
    check("a blank line is skipped", parse_strategy(PREAMBLE + "\n"), set())
    check("the same name twice is one name",
          parse_strategy(PREAMBLE + "1 TCS x\n2 TCS y\n"), {"TCS"})
    check("nothing but preamble yields nothing",
          parse_strategy(PREAMBLE), set())

    print("\nfiles that must be refused, because an empty list is silent")
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "in-nse_drv.stra"
        good.write_text(PREAMBLE + BODY, encoding="utf-8")
        check("a good file reads", sorted(read_strategy(good, "NSI-MAIN")),
              ["INFY", "RELIANCE", "TCS"])
        raises("no path configured at all",
               lambda: read_strategy("", "NSI-MAIN"),
               "ExcludeFile column in markets.csv is blank")
        raises("a path that is not there",
               lambda: read_strategy(Path(d) / "nope.stra", "NSI-MAIN"),
               "does not exist")
        empty = Path(d) / "empty.stra"
        empty.write_text(PREAMBLE, encoding="utf-8")
        raises("a file with a preamble and no names - it would pass every "
               "name through and the only symptom is a limit overriding "
               "the desk's",
               lambda: read_strategy(empty, "BSE-MAIN"), "lists no names")

    print("\nonly the venues that have reached their cutoff are read")
    with tempfile.TemporaryDirectory() as d:
        nse = Path(d) / "nse.stra"
        nse.write_text(PREAMBLE + BODY, encoding="utf-8")
        paths = {"NSI-MAIN": nse, "BSE-MAIN": ""}
        check("a blank ExcludeFile costs nothing before its cutoff",
              sorted(strategy_lists(["TYO-MAIN"], paths)), [])
        check("and neither does a venue that is not India's",
              sorted(strategy_lists(["TYO-MAIN", "SET-MAIN"], paths)), [])
        check("the venue in scope is read", sorted(
            strategy_lists(["NSI-MAIN"], paths)["NSI-MAIN"]),
            ["INFY", "RELIANCE", "TCS"])
        raises("but a blank one for a venue that IS in scope stops the "
               "run, exactly as R's stop() does",
               lambda: strategy_lists(["NSI-MAIN", "BSE-MAIN"], paths),
               "ExcludeFile column in markets.csv is blank")

    print("\nthe BSE secondary venue")

    def row(ric, bbg, code, venue_id, venue_list="", bse_bbg="", bse_ric=""):
        return crosscode.Row(
            ric=ric, bbg=bbg, ticker=crosscode.ticker_of(bbg),
            security=crosscode.security_name(bbg), fidessa_code=code,
            venue_id=venue_id, status="ACTV", venue_list=venue_list,
            bse_bbg=bse_bbg, bse_ric=bse_ric)

    rows = [row("RELI.NS", "RIL IN", "RELIANCE.IN", "NSI-MAIN",
                "NSI-MAIN|BSE-SECONDARY", "500325 IB", "RELI.BO"),
            row("TCS.NS", "TCS IN", "TCS.IN", "NSI-MAIN", "NSI-MAIN"),
            row("INFY.NS", "INFO IN", "INFY.IN", "NSI-MAIN",
                "NSI-MAIN|BSE-SECONDARY", "500209 IB", "INFY.BO")]
    got = secondary_rows(rows)
    check("one synthesised row per BSE listing",
          [(r.ric, r.bbg) for r in got],
          [("RELI.BO", "500325 IB"), ("INFY.BO", "500209 IB")])
    check("published under BSE-MAIN, and asked of Bloomberg under its own "
          "code", [(r.venue_id, r.security) for r in got],
          [("BSE-MAIN", "500325 IB Equity"),
           ("BSE-MAIN", "500209 IB Equity")])
    check("the fidessa code comes from the row that named the listing",
          [r.fidessa_code for r in got], ["RELIANCE.IN", "INFY.IN"])
    check("a row that names no secondary venue contributes nothing",
          [r.ric for r in secondary_rows([rows[1]])], [])

    check("a row whose VenueList names BSE-SECONDARY but carries no BSE "
          "code is not synthesised out of nothing",
          secondary_rows([row("X.NS", "X IN", "X.IN", "NSI-MAIN",
                              "BSE-SECONDARY")]), [])

    already = rows + [row("500325.BO", "500325 IB", "RELIANCE.IN",
                          "BSE-MAIN")]
    check("a code the crosscode ALREADY carries is left to the ordinary "
          "path - synthesising it too would publish one name twice under "
          "BSE-MAIN", [r.bbg for r in secondary_rows(already)],
          ["500209 IB"])

    twice = [rows[0], row("RELI2.NS", "RIL2 IN", "RELIANCE2.IN", "NSI-MAIN",
                          "BSE-SECONDARY", "500325 IB", "RELI.BO")]
    check("two rows naming one BSE code yield one row",
          [r.bbg for r in secondary_rows(twice)], ["500325 IB"])

    print("\none set of limits, published under both venues")
    priced = [{"#ReutersCode": "RELI.BO", "BloombergCode": "500325 IB",
               "LimitDate": "2026-09-09", "LimitUpPrice": "1600",
               "LimitDownPrice": "1400", "FidessaCode": "RELIANCE.IN",
               "Venue": "BSE-MAIN"}]
    both = publish_both(priced)
    check("the copy is BSE-SECONDARY", [r["Venue"] for r in both],
          ["BSE-SECONDARY"])
    check("with the same numbers",
          (both[0]["LimitUpPrice"], both[0]["LimitDownPrice"]),
          ("1600", "1400"))
    check("and the original is untouched, so the caller keeps both",
          priced[0]["Venue"], "BSE-MAIN")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
