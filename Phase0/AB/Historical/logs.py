#!/usr/bin/env python3
"""One place that decides what a line of this job's output looks like.

A scheduled job's log is read twice: once by a person watching the first run,
and once at 3am by whoever is working out why today's file is wrong.  Those
want different things, so every line carries a stamp and a level and the
important ones say a number as well as a word.

    12:04:41  ..  crosscode          41,208 rows
    12:04:41  ..  equity_master      matched 40,933 of 41,208
    12:04:52  !!  CHINA, NOT RENAMED 3    files land under C1/C2

FOUR LEVELS, and they mean different things to a reader in a hurry:

    ..   what happened.  the ordinary running commentary
    ok   a stage finished and the number was what it should be
    !!   a fact worth a human's attention.  NOT an error - the run
         continues - but the sort of thing that is a bug somewhere else
    XX   a failure.  the run is stopping

`--log FILE` tees everything to a file as well, so a scheduled run leaves a
record without anyone having to capture stdout.

    python logs.py --self-test
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

DOT, OK, WARN, FAIL = "..", "ok", "!!", "XX"


class Log:
    """Writes to stdout, and to a file when given one.

    `quiet` silences the running commentary but never a warning or a
    failure - a --quiet that could hide "CHINA, NOT RENAMED" would be worse
    than no --quiet at all."""

    def __init__(self, path=None, quiet=False, stamps=True, clock=None):
        self.quiet = quiet
        self.stamps = stamps
        self.clock = clock or (lambda: dt.datetime.now())
        self.counts = {DOT: 0, OK: 0, WARN: 0, FAIL: 0}
        self.fh = None
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            #  A blank line BETWEEN runs, never before the first one, so the
            #  file does not open on an empty line that offsets every reader
            #  counting from the top.
            gap = "\n" if p.is_file() and p.stat().st_size else ""
            self.fh = p.open("a", encoding="utf-8")
            self.fh.write(f"{gap}=== {self.clock():%Y-%m-%d %H:%M:%S} ===\n")

    # -- the one place a line is shaped -----------------------------------

    def line(self, level: str, text: str = "") -> str:
        stamp = f"{self.clock():%H:%M:%S}  " if self.stamps else ""
        return f"{stamp}{level}  {text}".rstrip()

    def emit(self, level: str, text: str = "") -> None:
        self.counts[level] = self.counts.get(level, 0) + 1
        rendered = self.line(level, text)
        if not (self.quiet and level == DOT):
            print(rendered, file=sys.stderr if level == FAIL else sys.stdout)
        if self.fh:
            self.fh.write(rendered + "\n")
            self.fh.flush()

    # -- what callers actually use ----------------------------------------

    def info(self, text=""):
        self.emit(DOT, text)

    def ok(self, text):
        self.emit(OK, text)

    def warn(self, text):
        self.emit(WARN, text)

    def fail(self, text):
        self.emit(FAIL, text)

    def kv(self, key, value, note="") -> None:
        """A named number or string, in a column, with room for a why."""
        self.info(f"{key:<24}{value}" + (f"   {note}" if note else ""))

    def step(self, n, title) -> None:
        self.info()
        self.info(f"--- {n}. {title} " + "-" * max(0, 52 - len(str(title))))

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def thousands(n) -> str:
    """41208 -> '41,208'.  A universe is tens of thousands of rows and an
    unseparated five digit number is one a reader has to count."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    frozen = dt.datetime(2026, 9, 4, 12, 4, 41)

    print("logs --self-test\n\nshaping a line")
    log = Log(stamps=True, clock=lambda: frozen)
    check("a stamp, a level, then the text",
          log.line(DOT, "crosscode  41,208 rows"),
          "12:04:41  ..  crosscode  41,208 rows")
    check("without stamps, for a self-test or a diff",
          Log(stamps=False).line(OK, "done"), "ok  done")
    check("an empty line does not leave trailing spaces",
          Log(stamps=False).line(DOT, ""), "..")

    print("\ncounting what was said")
    log = Log(stamps=False)
    log.info("a")
    log.ok("b")
    log.warn("c")
    log.warn("d")
    check("every level is tallied, so a run can end by saying how many "
          "warnings it produced",
          log.counts, {DOT: 1, OK: 1, WARN: 2, FAIL: 0})

    print("\nquiet")
    quiet = Log(stamps=False, quiet=True)
    check("quiet still counts the commentary it does not print",
          (quiet.info("hidden"), quiet.counts[DOT])[1], 1)
    check("and a warning is NOT suppressed - a --quiet that could hide a "
          "wrong file would be worse than none",
          (quiet.warn("seen"), quiet.counts[WARN])[1], 1)

    print("\nthe file")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "deep" / "run.log"
        log = Log(path=p, stamps=False, clock=lambda: frozen)
        log.info("first")
        log.warn("second")
        log.close()
        lines = p.read_text(encoding="utf-8").splitlines()
        check("the directory is created on the way",
              lines[0], "=== 2026-09-04 12:04:41 ===")
        check("and the lines land in it",
              lines[1:], ["..  first", "!!  second"])

        log2 = Log(path=p, stamps=False, clock=lambda: frozen)
        log2.info("third")
        log2.close()
        check("a second run APPENDS rather than truncating - a log that "
              "loses yesterday is not a log",
              p.read_text(encoding="utf-8").splitlines()[-4:],
              ["!!  second", "", "=== 2026-09-04 12:04:41 ===",
               "..  third"])
        check("with a blank line between the runs, but none before the "
              "first", p.read_text(encoding="utf-8")[0], "=")

    print("\nformatting a number")
    check("thousands are separated", thousands(41208), "41,208")
    check("small numbers are unchanged", thousands(7), "7")
    check("a string passes through", thousands("n/a"), "n/a")
    check("so does None, rather than raising", thousands(None), "None")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
