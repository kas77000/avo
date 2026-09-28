#!/usr/bin/env python3
"""The open-auction aggressivity override: the only source of
OpenAggressivityPct.

Optional, like the msci mapping: no file means the column is blank for every
row, and the run says so rather than failing.

WHAT THE R JOB DOES WITH IT, at :322-329:

    AuctionOverride <- read.csv(job.info$OpenAuctionAggressiveLevel, ...)
    tradingData = left_join(tradingData, AuctionOverride, by = "RicCode")

A left join on RicCode, and that is the whole of it.  There is no rule and no
computed default: a name on the file gets its percentage, every other row
gets NA, and write.csv's na="" turns that into an empty field.  Which is why
the column is blank for almost every row of the R output too.

The file has a header and at least these two columns:

    RicCode              the join key, matched exactly as the crosscode
                         spells it
    OpenAggressivityPct  the value

Both are read by name, so more columns or another order is fine.  The value
is copied VERBATIM rather than parsed as a number: R reads it, joins it and
prints it back unchanged, so 5 must not go out as 5.0.

TWO THINGS THE R JOB WOULD DIE ON, which are reported here instead:

  a file with no OpenAggressivityPct column.  The join adds nothing, then
  :463's select cannot find the name and the job stops.  Here the column
  stays blank and the run says the header was wrong.

  a RicCode listed twice.  dplyr's left_join emits ONE OUTPUT ROW PER
  MATCHING OVERRIDE ROW, so a duplicated code quietly lengthens
  TradingData.csv.  Here the last row for a code wins and the duplicates are
  counted, because a file with the wrong number of rows is worse than a row
  with the wrong percentage.

    python auction.py --self-test
"""

from __future__ import annotations

import csv
from pathlib import Path

RIC_COLUMN = "RicCode"
PCT_COLUMN = "OpenAggressivityPct"


def load(path, log=None) -> dict:
    """{RicCode: OpenAggressivityPct}, as text.

    A missing or unreadable file is an empty dict rather than an error, which
    is what the R job's tryCatch makes of it."""
    p = Path(path)
    if not p.exists():
        return {}

    out = {}
    dupes = 0
    with p.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if PCT_COLUMN not in (reader.fieldnames or []):
            if log:
                log(f"  ! the override has no {PCT_COLUMN} column - the R "
                    f"job stops at :463 on this file; nothing is filled")
            return {}
        for r in reader:
            ric = (r.get(RIC_COLUMN) or "").strip()
            if not ric:
                continue
            if ric in out:
                dupes += 1
            out[ric] = (r.get(PCT_COLUMN) or "").strip()

    if dupes and log:
        log(f"  ! {dupes} duplicate {RIC_COLUMN} in the override - the last "
            f"row for a code wins, where the R job would have DUPLICATED "
            f"those output rows")
    return out


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("auction --self-test\n\nno file at all")
    check("a missing override is an empty dict, not an error",
          load(Path("does-not-exist.csv")), {})
    check("and every row looks it up to nothing",
          load(Path("does-not-exist.csv")).get("BHP.AX", ""), "")

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "override.csv"
        p.write_text("RicCode,OpenAggressivityPct\n"
                     "BHP.AX,50\n"
                     "0700.HK,12.5\n", encoding="utf-8")
        check("the file is read on RicCode, per :325",
              load(p), {"BHP.AX": "50", "0700.HK": "12.5"})
        check("a code that is not on it joins to nothing",
              load(p).get("RELI.NS", ""), "")

        p.write_text("Something,RicCode,OpenAggressivityPct,Else\n"
                     "x,BHP.AX,50,y\n", encoding="utf-8")
        check("the two columns are found by name, in any position",
              load(p), {"BHP.AX": "50"})

        p.write_text("RicCode,OpenAggressivityPct\nBHP.AX,5\n",
                     encoding="utf-8")
        check("the value is text, not a parsed number - 5 stays 5",
              load(p)["BHP.AX"], "5")

        said = []
        p.write_text("RicCode,OpenAggressivityPct\n"
                     "BHP.AX,50\nBHP.AX,60\n", encoding="utf-8")
        check("a duplicated code takes the last row",
              load(p, said.append)["BHP.AX"], "60")
        check("and is counted out loud, because R would have duplicated "
              "the output row", len(said), 1)

        said = []
        p.write_text("RicCode,Level\nBHP.AX,50\n", encoding="utf-8")
        check("a file without the value column fills nothing",
              load(p, said.append), {})
        check("and says so rather than writing blanks in silence",
              len(said), 1)

        p.write_text("RicCode,OpenAggressivityPct\n,50\nBHP.AX,\n",
                     encoding="utf-8")
        check("a blank RicCode is not a key, and a blank value is kept as "
              "the blank it is", load(p), {"BHP.AX": ""})

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
