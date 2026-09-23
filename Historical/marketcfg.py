#!/usr/bin/env python3
"""Per-market configuration, as a CSV the desk can edit in Excel.

The same `config/markets.csv` TradingData uses, and deliberately the same
file shape so the desk maintains one layout rather than two.  This job reads
three of its columns - the Fidessa market, the Bloomberg composite and the
timezone label - and ignores `NoShortSell` and `RespectShortSellPrice`
entirely.

`TimeZone` is this job's own addition, and it is a WINDOWS TIMEZONE ID -
the string TimeZoneInfo.FindSystemTimeZoneById takes - because the consumer
looks the label up rather than printing it.  That is why Japan reads "Tokyo
Standard Time" and not "Japan Standard Time", which is the natural name and
is not a Windows id at all.

Windows names several zones after ONE city and covers its neighbours with
it, so these are correct and only look wrong:

    Hong Kong  -> China Standard Time       (Beijing, Chongqing, Hong Kong)
    Jakarta    -> SE Asia Standard Time     (Bangkok, Hanoi, Jakarta)
    Bangkok    -> SE Asia Standard Time     the same one
    Kuala L.   -> Singapore Standard Time   (Kuala Lumpur, Singapore)
    Taipei     -> Taipei Standard Time      not "Taiwan"

MANILA IS China Standard Time, WHICH IS NOT THE ONE WINDOWS WOULD PICK.
Windows carries no Philippine zone, so the obvious id for it is Singapore
Standard Time - but a real BEL PM file says China Standard Time, and the
file on disk beats the reasoning.  Both are UTC+8 and neither keeps DST, so
the clock is the same either way; this is about matching what the consumer
already reads.

Every id in the column was checked against Get-TimeZone -ListAvailable on
2026-09-08 and every one resolves.  It is
the seventh header cell of the output CSV, naming the clock column one is
in - and what that clock is cannot be known until qatt_time_probe.py has
run.  Filling it in before then would be writing down a guess.

WHAT THE COMPOSITE IS FOR.  qatt is keyed on a sym built from the Bloomberg
ticker and the COMPOSITE exchange code: Toyota is `7203.JP`, not `7203.JT`.
The crosscode carries the PRIMARY code (`7203 JT`), so something has to
supply the composite.  equity_master does, authoritatively, and that is what
the job uses.  This file is the fallback for a name equity_master has no row
for - and the run reports how many names took it, because a fallback that
starts carrying real traffic is a fact worth seeing.

    python marketcfg.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Market:
    fidessa_market: str
    bbg_composite: str
    time_zone: str


def load(path):
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("FidessaMarket") or "").strip()
            if not key:
                continue
            out[key] = Market(
                fidessa_market=key,
                bbg_composite=(r.get("BBGComposite") or "").strip(),
                time_zone=(r.get("TimeZone") or "").strip())
    return out


def load_composites(path) -> dict:
    """{Bloomberg exchange code: the composite it is WRITTEN as}, from
    config/composites.csv - and only the codes that convert.

    THE LEGACY JOB'S OWN RULE, kept as a table because it is a table there:
    MarketConditionBBG.xml gives every BloombergMarketInfo a
    Convert2Composite flag and a CompositeExchangeCode, and the folder and
    the file take the composite when the flag is set.  `RIO AT` is written
    `RIO AU`; `7203 JT` is written `7203 JP`.  A code that does not convert,
    and a code the file has never heard of, keep what the crosscode says."""
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            code = (r.get("BBGCode") or "").strip()
            comp = (r.get("CompositeExchangeCode") or "").strip()
            convert = (r.get("Convert2Composite") or "").strip().upper()
            if not code or convert not in ("TRUE", "1", "YES"):
                continue
            if not comp:
                raise ValueError(
                    f"{path}: {code} converts to a composite but names "
                    f"none - a file cannot be called '{code} '")
            out[code] = comp
    return out


def composite(market: str, markets) -> str:
    """The composite for a Fidessa market, or "" if it is not configured.

    "" is not an error here.  The crosscode carries venues this file has
    never listed - JNX-MAIN and CHJ-MAIN among them - and the answer for
    those is that this file has no opinion, which the caller reports rather
    than guesses around."""
    m = markets.get((market or "").strip())
    return m.bbg_composite if m else ""


#  Every Windows id this config uses, checked against
#  `Get-TimeZone -ListAvailable` on 2026-09-08.  The point of the list is
#  that a typo cannot pass: a misspelt id is not a timezone anywhere, and
#  the consumer would find that out long after the file was written.
WINDOWS_TIME_ZONE_IDS = (
    "AUS Eastern Standard Time",      # Canberra, Melbourne, Sydney
    "China Standard Time",            # Beijing, Chongqing, Hong Kong
    "India Standard Time",            # Chennai, Kolkata, Mumbai, New Delhi
    "Korea Standard Time",            # Seoul
    "New Zealand Standard Time",      # Auckland, Wellington
    "SE Asia Standard Time",          # Bangkok, Hanoi, Jakarta
    "Singapore Standard Time",        # Kuala Lumpur, Singapore, and Manila
    "Taipei Standard Time",           # Taipei
    "Tokyo Standard Time",            # Osaka, Sapporo, Tokyo
)


#  THE SAME ZONES, AS THE TZ DATABASE NAMES THEM.  The column is a Windows
#  id because the consumer reads it; converting a clock needs the IANA name,
#  so the two are paired here and nowhere else.  A market added to
#  markets.csv with an id that is not in this table is refused by name
#  rather than left unconverted - a file stamped in the wrong clock looks
#  exactly like a file stamped in the right one.
IANA_OF = {
    "AUS Eastern Standard Time": "Australia/Sydney",
    "China Standard Time": "Asia/Hong_Kong",
    "India Standard Time": "Asia/Kolkata",
    "Korea Standard Time": "Asia/Seoul",
    "New Zealand Standard Time": "Pacific/Auckland",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
}


def zone(windows_id: str):
    """A Windows timezone id -> a tzinfo."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError as e:                                # pragma: no cover
        raise ValueError(f"no zoneinfo in this python ({e})") from e
    name = IANA_OF.get((windows_id or "").strip())
    if not name:
        raise ValueError(
            f"no tz database name for {windows_id!r}; add it to "
            f"marketcfg.IANA_OF beside the id in config/markets.csv")
    try:
        return ZoneInfo(name)
    except Exception as e:                                  # noqa: BLE001
        raise ValueError(
            f"{name} is not in this machine's tz database ({e}).  On "
            f"Windows python carries none of its own: pip install tzdata"
        ) from e


def shift_seconds(date, source_id: str, target_id: str) -> int:
    """How far to move a clock reading from `source_id` to `target_id` on
    that date: 08:30 in Hong Kong is 09:30 in Tokyo, so +3600.

    ONE OFFSET FOR THE WHOLE DAY, taken at midday.  Both zones' offsets are
    read at the same instant, so a market that keeps daylight saving -
    Sydney, Auckland - is followed rather than assumed, and the date is what
    decides.  A transition happens in the small hours of a Sunday, when no
    session is running, so no trading day needs two offsets."""
    if (source_id or "").strip() == (target_id or "").strip():
        return 0
    import datetime as dt
    src, tgt = zone(source_id), zone(target_id)
    noon = dt.datetime(date.year, date.month, date.day, 12, tzinfo=src)
    return int((noon.astimezone(tgt).utcoffset()
                - noon.utcoffset()).total_seconds())


def tz_of(markets, market: str) -> str:
    """The timezone label for a Fidessa market, or "" if unlisted.

    The mirror of composite(): the crosscode carries venues this file has
    never listed, and the answer for those is no opinion."""
    m = markets.get((market or "").strip())
    return m.time_zone if m else ""


def _self_test_zones(check, M):
    print("\nthe clock each market is written in")
    check("every id the config uses has a tz database name beside it - a "
          "market added without one cannot be converted",
          sorted(z for z in {v.time_zone for v in M.values()} if z
                 and z not in IANA_OF), [])
    import datetime as dt
    jan, jul = dt.date(2026, 1, 15), dt.date(2026, 7, 15)
    HK = "China Standard Time"
    check("Hong Kong to Tokyo is an hour on",
          shift_seconds(jan, HK, "Tokyo Standard Time"), 3600)
    check("to Mumbai, two and a half back",
          shift_seconds(jan, HK, "India Standard Time"), -9000)
    check("and to itself, nothing", shift_seconds(jan, HK, HK), 0)
    check("SYDNEY KEEPS DAYLIGHT SAVING: +3 in January, +2 in July, so the "
          "date decides and one offset per market would be wrong",
          (shift_seconds(jan, HK, "AUS Eastern Standard Time"),
           shift_seconds(jul, HK, "AUS Eastern Standard Time")),
          (10800, 7200))
    check("Auckland too",
          (shift_seconds(jan, HK, "New Zealand Standard Time"),
           shift_seconds(jul, HK, "New Zealand Standard Time")),
          (18000, 14400))
    try:
        zone("Mars Standard Time")
        check("an unknown id raised", False, True)
    except ValueError as e:
        check("an id with no mapping is refused, naming it and where to "
              "add it", ("Mars Standard Time" in str(e)
                         and "IANA_OF" in str(e)), True)


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    here = Path(__file__).resolve().parent
    M = load(here / "config" / "markets.csv")

    print("marketcfg --self-test\n\nthe shipped config")
    check("Tokyo composites to JP, which is what qatt is keyed on - not the "
          "JT the crosscode carries",
          composite("TYO-MAIN", M), "JP")
    check("Korea's two boards share one composite",
          (composite("KSC-MAIN", M), composite("KOE-MAIN", M)), ("KS", "KS"))
    check("every China board composites to CH",
          {composite(k, M) for k in
           ("SHA-MAIN", "SHH-MAIN", "SHZ-MAIN", "SSC-MAIN",
            "SZA-MAIN", "SZC-MAIN")},
          {"CH"})
    check("Australia's primary and composite are the same letters, which is "
          "why AU names hide this whole problem",
          composite("ASX-MAIN", M), "AU")

    print("\nthe timezone label, which is the seventh header cell")
    check("Tokyo is the WINDOWS id, not the natural name Japan Standard "
          "Time, because the consumer looks it up",
          M["TYO-MAIN"].time_zone, "Tokyo Standard Time")
    check("Sydney matches the one real file on record, and is a Windows id "
          "either way",
          M["ASX-MAIN"].time_zone, "AUS Eastern Standard Time")
    check("every label is a Windows id - a typo like Toyko would resolve to "
          "nothing at the far end, silently",
          sorted({v.time_zone for v in M.values()}
                 - set(WINDOWS_TIME_ZONE_IDS)), [])
    check("Hong Kong is covered by Beijing's zone, which is correct and "
          "only looks wrong", M["HKG-MAIN"].time_zone, "China Standard Time")
    check("Manila is China Standard Time, per a real BEL PM file - NOT "
          "the Singapore zone Windows would have you pick",
          M["PHS-MAIN"].time_zone, "China Standard Time")
    check("EVERY listed market has one - a blank writes a six-cell header "
          "and the consumer cannot tell which clock it is reading",
          sorted(k for k, v in M.items() if not v.time_zone), [])
    check("the boards that share a clock say the same thing",
          {M[k].time_zone for k in ("SHA-MAIN", "SHH-MAIN", "SHZ-MAIN",
                                    "SSC-MAIN", "SZA-MAIN", "SZC-MAIN")},
          {"China Standard Time"})
    check("and Korea's two boards do too",
          M["KSC-MAIN"].time_zone == M["KOE-MAIN"].time_zone, True)
    check("a market this file does not list has no label to give, which is "
          "no opinion rather than an error",
          tz_of(M, "JNX-MAIN"), "")

    print("\nmarkets this file does not list")
    check("a Japanese alternative venue is not in here, and that is not an "
          "error - it is no opinion",
          composite("JNX-MAIN", M), "")
    check("nor is an unknown market", composite("XXX-MAIN", M), "")
    check("nor a blank one", composite("", M), "")
    check("whitespace around a market name is not part of it",
          composite("  TYO-MAIN  ", M), "JP")

    _self_test_zones(check, M)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
