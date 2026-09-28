#!/usr/bin/env python3
"""Every (name, date) kdb has already been asked about and had nothing for.

WHY.  qatt holds only the names we subscribe to, and only for as long as the
HDB keeps them.  A crosscode name that was never subscribed has no prints on
any date, ever - and without a record of having asked, every run asks again,
for every day of the backfill, forever.  On a universe of tens of thousands
that is the difference between a job that finishes and one that does not.

This is the record.  It is a long-term cache of ABSENCE, and it is consulted
alongside the output directory: a date is "already tried" if there is a file
for it OR a line in here.

    BloombergCode,Sym,Date,FirstTried
    600000 CG,600000.CH,2026-09-02,2026-09-03

WHAT COUNTS AS A MISS, and this is the whole safety of the thing: kdb
ANSWERED, and the answer was empty.  A query that raised, a server that was
down, a partition that could not be read - none of those reach here.  They
are failures, they are not facts about the data, and a cache that recorded
them would turn a five-minute outage into a permanent hole nobody ever sees.
The caller only records what it actually received.

STILL, A CACHE OF ABSENCE CAN GO STALE.  A name gets subscribed; a vendor
backfills a day.  `--retry-misses` ignores this file for one run and rewrites
it from what that run actually finds, which is the escape hatch.

The file is rewritten whole each run, sorted, so it diffs cleanly and a human
can delete a line to force one name back.

    python misscache.py --self-test
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

COLUMNS = ("BloombergCode", "Sym", "Date", "FirstTried")


def load(path) -> dict:
    """{bloomberg code: {date: (sym, first_tried)}}.

    A file that does not exist is an empty cache, not an error - the first
    run of a new deployment has no misses yet.  A malformed line is SKIPPED
    rather than fatal: the worst a lost line can do is cause one wasted
    query, and refusing to start because a cache is scruffy would be a much
    worse trade."""
    out = {}
    p = Path(path)
    if not p.is_file():
        return out
    with p.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            bbg = (r.get("BloombergCode") or "").strip()
            date = _date(r.get("Date"))
            if not bbg or date is None:
                continue
            out.setdefault(bbg, {})[date] = (
                (r.get("Sym") or "").strip(),
                _date(r.get("FirstTried")) or date)
    return out


def _date(text):
    try:
        return dt.date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def tried(cache: dict, bbg: str) -> set:
    """Every date this name has already been asked about and found empty."""
    return set(cache.get(bbg, {}))


def record(cache: dict, bbg: str, sym: str, date, today=None) -> dict:
    """Note that this name had nothing on this date.

    FirstTried is kept from the existing entry when there is one, so the file
    says when we first learned a name was absent rather than when we last
    re-read our own cache.  That is the number that tells you a name has been
    missing for six months."""
    today = today or dt.date.today()
    existing = cache.get(bbg, {}).get(date)
    first = existing[1] if existing else today
    cache.setdefault(bbg, {})[date] = (sym, first)
    return cache


def save(path, cache: dict) -> int:
    """Rewrite the file whole, sorted.  Returns the line count."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for bbg in sorted(cache):
            for date in sorted(cache[bbg]):
                sym, first = cache[bbg][date]
                w.writerow([bbg, sym, date.isoformat(), first.isoformat()])
                n += 1
    return n


def count(cache: dict) -> int:
    return sum(len(v) for v in cache.values())


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    D = dt.date
    d1, d2 = D(2026, 9, 2), D(2026, 9, 3)
    today = D(2026, 9, 4)

    print("misscache --self-test\n\nrecording a miss")
    c = record({}, "ZZZ QQ", "ZZZ.QQ", d1, today)
    check("the name and date are in", tried(c, "ZZZ QQ"), {d1})
    check("with the sym and the day we learned it",
          c["ZZZ QQ"][d1], ("ZZZ.QQ", today))
    c = record(c, "ZZZ QQ", "ZZZ.QQ", d2, today)
    check("a second date joins the first", tried(c, "ZZZ QQ"), {d1, d2})
    check("a name never recorded has been tried on nothing",
          tried(c, "7203 JT"), set())
    check("two names, three dates", count(c), 2)

    print("\nre-recording the same miss")
    later = record(c, "ZZZ QQ", "ZZZ.QQ", d1, D(2026, 12, 25))
    check("FirstTried is NOT bumped - it says when the name went missing, "
          "not when we last re-read our own cache",
          later["ZZZ QQ"][d1][1], today)
    check("and nothing is duplicated", count(later), 2)

    print("\nround trip through the file")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "_no_data.csv"
        check("the line count comes back, and the directory is created",
              save(p, later), 2)
        back = load(p)
        check("what went in comes out", tried(back, "ZZZ QQ"), {d1, d2})
        check("with the sym and the original FirstTried intact",
              back["ZZZ QQ"][d1], ("ZZZ.QQ", today))
        check("the header is the four columns",
              p.read_text(encoding="utf-8").splitlines()[0],
              "BloombergCode,Sym,Date,FirstTried")
        check("and the body is sorted, so the file diffs cleanly",
              p.read_text(encoding="utf-8").splitlines()[1:],
              ["ZZZ QQ,ZZZ.QQ,2026-09-02,2026-09-04",
               "ZZZ QQ,ZZZ.QQ,2026-09-03,2026-09-04"])

        check("no file yet is an empty cache, not an error",
              load(Path(d) / "nothing.csv"), {})

        bad = Path(d) / "bad.csv"
        bad.write_text("BloombergCode,Sym,Date,FirstTried\n"
                       "GOOD X,GOOD.X,2026-09-02,2026-09-02\n"
                       ",,2026-09-02,2026-09-02\n"
                       "NODATE X,NODATE.X,,2026-09-02\n"
                       "BADDATE X,BADDATE.X,not-a-date,2026-09-02\n",
                       encoding="utf-8")
        got = load(bad)
        check("a scruffy line is skipped, not fatal - the worst a lost line "
              "costs is one wasted query, and refusing to start would cost "
              "the whole run",
              sorted(got), ["GOOD X"])

        empty = Path(d) / "empty.csv"
        save(empty, {})
        check("an empty cache still writes its header, so the file exists "
              "and is obviously ours",
              empty.read_text(encoding="utf-8").splitlines(),
              ["BloombergCode,Sym,Date,FirstTried"])
        check("and reads back empty", load(empty), {})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
