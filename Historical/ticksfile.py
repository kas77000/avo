#!/usr/bin/env python3
"""The output side: what a file is called, what is already on disk, and the
CSV itself.

THE NAME IS THE STATE.  There is no manifest.  A name with no
`raw-<code>-*.csv` in the output directory has never been fetched and takes
the full backfill; one with files takes only the day it is missing.  That
makes a deleted file self-healing and a half-finished run resumable, and it
removes the failure where a manifest says a name is done and the disk
disagrees - which fails in the direction that silently skips a stock.

THE FORMAT IS BLOOMBERG'S, reproduced rather than improved.  Six data
columns, and a SEVENTH header cell carrying the name of the timezone the
clock is in:

    #Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern Standard Time
    09:31:33,0.105,1000,T,T,XASX

That trailing header cell is not a column - no data row has a seventh field.
It is a label the Bloomberg add-in wrote, and it is reproduced because
whatever reads these files downstream was written against it.

    THE LABEL COMES FROM config/markets.csv AND SHIPS BLANK.  It says what
    the clock in column one IS, and that is not known until
    qatt_time_probe.py has run - if the answer turns out to be qatt`time,
    the honest label is Hong Kong for every market, not the local zone.  A
    blank writes a six cell header, and the run reports how many names had
    no label rather than inventing one.

    python ticksfile.py --self-test
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

COLUMNS = ["#Time", "Last", "Volume", "Condition", "Exchange", "MicCode"]

PREFIX = "raw-"
SUFFIX = ".csv"

#  raw-<code>-<8 digits>.csv, where <code> may itself contain '-' and ' '
#  (SCB-R TB).  Anchored on the date, so the code is whatever is left.
NAME_RE = re.compile(r"^raw-(?P<bbg>.+)-(?P<date>\d{8})\.csv$")


def filename(bbg: str, date) -> str:
    """('7203 JT', 2026-09-03) -> 'raw-7203 JT-20260903.csv'."""
    return f"{PREFIX}{bbg}-{date:%Y%m%d}{SUFFIX}"


def parse_filename(name: str):
    """The inverse, or None if this is not one of ours.

    The date is matched first and the code is whatever precedes it, because
    a Bloomberg code can contain the same '-' the name uses as a separator:
    splitting from the left turns 'SCB-R TB' into 'SCB'."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return m.group("bbg"), dt.datetime.strptime(
            m.group("date"), "%Y%m%d").date()
    except ValueError:
        return None


def existing_dates(directory, bbg: str) -> set:
    """Every date already on disk for one name."""
    d = Path(directory)
    if not d.is_dir():
        return set()
    out = set()
    for path in d.iterdir():
        parsed = parse_filename(path.name)
        if parsed and parsed[0] == bbg:
            out.add(parsed[1])
    return out


def days_wanted(have: set, partitions: list, backfill: int) -> list:
    """Which dates to fetch for one name: the window, minus what is tried.

    ONE RULE, NOT TWO.  The window is the last `backfill` partitions that
    actually exist; `have` is every date already tried, which means files on
    disk AND misses in the cache.  Subtract, and the two populations fall out
    on their own:

        nothing tried     the whole window          - a backfill
        window all tried  nothing                   - a no-op re-run
        one day short     that day                  - the daily run

    An earlier version branched on "is `have` empty" and took only the newest
    day for anything non-empty.  That is subtly wrong once misses count as
    tried: a first run interrupted after recording one miss left the name
    looking known, and its backfill was never finished by any later run.
    Subtracting from a window cannot do that - whatever is missing is fetched,
    however it came to be missing.

    The cost is that a deleted file IS re-fetched. That is the self-healing
    the output directory was chosen for, not a regression."""
    if not partitions:
        return []
    #  At least one, so a backfill of 0 still keeps the name up to date
    #  rather than freezing it forever.
    n = max(1, int(backfill))
    return [d for d in partitions[-n:] if d not in have]


def write(path, rows, mic: str, tz_label: str = "") -> int:
    """One name, one day.  Returns the row count written.

    newline="" is required, not cosmetic: without it csv writes \\r\\r\\n on
    Windows and every other line of the file reads as blank."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS + ([tz_label] if tz_label else []))
        for r in rows:
            w.writerow([r["time"], _num(r["price"]), _num(r["size"]),
                        r["cond"], r["ex"], mic])
    return len(rows)


def _num(value) -> str:
    """A Decimal as the file carries it: no exponent, no trailing zeroes
    the source did not have, and blank for nothing.

    str(Decimal) is right for both - Decimal keeps the scale it was built
    with, so 0.105 stays 0.105 and 1000 stays 1000 - except that a value
    large or small enough to go exponential has to be pulled back."""
    if value is None:
        return ""
    text = str(value)
    if "E" in text or "e" in text:
        return format(value, "f")
    return text


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

    D = dt.date

    print("ticksfile --self-test\n\nnaming a file")
    check("the primary code and the date, as the sample file has them",
          filename("EAU AU", D(2026, 8, 17)), "raw-EAU AU-20260817.csv")
    check("Toyota is named by its primary, never the composite",
          filename("7203 JT", D(2026, 9, 3)), "raw-7203 JT-20260903.csv")

    print("\nreading one back")
    check("round trip", parse_filename("raw-EAU AU-20260817.csv"),
          ("EAU AU", D(2026, 8, 17)))
    check("a code containing the separator survives, because the date is "
          "matched first",
          parse_filename("raw-SCB-R TB-20260817.csv"),
          ("SCB-R TB", D(2026, 8, 17)))
    check("something else in the directory is not ours",
          parse_filename("volume_curve.csv"), None)
    check("nor is a file with no date", parse_filename("raw-EAU AU.csv"), None)
    check("nor one whose date is not a date",
          parse_filename("raw-EAU AU-20261341.csv"), None)
    check("nor the right shape with the wrong extension",
          parse_filename("raw-EAU AU-20260817.txt"), None)

    print("\nwhat to fetch: the window, minus what is already tried")
    P = [D(2026, 8, 31), D(2026, 9, 1), D(2026, 9, 2), D(2026, 9, 3)]
    check("a name with nothing tried takes the last N partitions",
          days_wanted(set(), P, 2), [D(2026, 9, 2), D(2026, 9, 3)])
    check("a backfill deeper than the store yields what the store has, not "
          "requests for days it never held",
          days_wanted(set(), P, 99), P)
    check("a name up to date except for today takes today",
          days_wanted({D(2026, 9, 2), D(2026, 9, 1), D(2026, 8, 31)}, P, 60),
          [D(2026, 9, 3)])
    check("and nothing once it has everything, so a second run today is a "
          "no-op rather than a rewrite",
          days_wanted(set(P), P, 60), [])
    check("A GAP IS FILLED, and this is the case an earlier version got "
          "wrong: one tried day used to make a name look known, so an "
          "interrupted backfill was never finished",
          days_wanted({D(2026, 9, 3)}, P, 60),
          [D(2026, 8, 31), D(2026, 9, 1), D(2026, 9, 2)])
    check("a day outside the window does not drag it back",
          days_wanted({D(2026, 8, 31)}, P, 2),
          [D(2026, 9, 2), D(2026, 9, 3)])
    check("no partitions, nothing to do", days_wanted(set(), [], 60), [])
    check("a zero backfill still keeps a name up to date rather than "
          "freezing it forever",
          days_wanted(set(), P, 0), [D(2026, 9, 3)])
    check("and asks for nothing once that day is tried",
          days_wanted({D(2026, 9, 3)}, P, 0), [])

    print("\nnumbers as the file carries them")
    check("a price keeps its scale", _num(Decimal("0.105")), "0.105")
    check("a whole size stays whole", _num(Decimal("1000")), "1000")
    check("a trailing zero the source had is kept - 0.10 and 0.1 are "
          "different ticks",
          _num(Decimal("0.10")), "0.10")
    check("nothing writes an empty cell", _num(None), "")
    check("a tiny number does not go exponential in the file",
          _num(Decimal("0.00000001")), "0.00000001")
    check("nor does a huge one", _num(Decimal("1E+10")), "10000000000")

    print("\nwriting a file")
    rows = [{"time": "09:31:33", "price": Decimal("0.105"),
             "size": Decimal("1000"), "cond": "T", "ex": "T"},
            {"time": "09:59:10", "price": Decimal("0.105"),
             "size": Decimal("848"), "cond": "OA", "ex": "T"},
            {"time": "10:00:15", "price": Decimal("0.105"),
             "size": Decimal("4998"), "cond": "", "ex": "H"}]

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / filename("EAU AU", D(2026, 8, 17))
        check("the row count comes back", write(p, rows, "XASX",
                                                "AUS Eastern Standard Time"),
              3)
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        check("the header carries the six columns and the timezone label "
              "as a seventh cell",
              lines[0],
              "#Time,Last,Volume,Condition,Exchange,MicCode,"
              "AUS Eastern Standard Time")
        check("a data row has six fields and no seventh",
              lines[1], "09:31:33,0.105,1000,T,T,XASX")
        check("an empty condition is an empty field, not the word None",
              lines[3], "10:00:15,0.105,4998,,H,XASX")
        check("three prints, one header", len(lines), 4)
        check("no blank line between rows - the Windows csv trap",
              "\r\r\n" in text, False)

        p2 = Path(d) / "no_label.csv"
        write(p2, rows, "XASX", "")
        check("no timezone label writes a six cell header rather than a "
              "trailing comma",
              p2.read_text(encoding="utf-8").splitlines()[0],
              "#Time,Last,Volume,Condition,Exchange,MicCode")

        p3 = Path(d) / "deep" / "er" / "empty.csv"
        check("a day with no prints still writes a file, so the name is "
              "not re-fetched every run",
              write(p3, [], "XASX", ""), 0)
        check("and the directory is created on the way",
              p3.read_text(encoding="utf-8").splitlines(),
              ["#Time,Last,Volume,Condition,Exchange,MicCode"])

        check("the directory now answers for what it holds",
              existing_dates(d, "EAU AU"), {D(2026, 8, 17)})
        check("and says nothing about a name it does not have",
              existing_dates(d, "7203 JT"), set())
        check("a directory that does not exist is empty, not an error",
              existing_dates(Path(d) / "nope", "EAU AU"), set())

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
