#!/usr/bin/env python3
r"""The list of files to fetch, as a CSV the desk can edit in Excel.

One row per file.  The only column that needs thought is Route, and it is a
property of the FILE, not of the job: some of these shares answer from the
corporate machine directly and some only answer from the server.

    Name          a short label.  Only used in the run's output, and to pick
                  a single file with --only.
    SourcePath    the full UNC path of the file on the share, including the
                  filename.  Not a directory, not a wildcard.
    Route         direct  - robocopy straight from SourcePath
                  server  - the server copies it to the staging folder first,
                            and robocopy pulls it from there

A Route you guessed wrong is the failure to expect on day one, so --probe
resolves every SourcePath from here and says which rows disagree with the
route they were given.

    python filelist.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

DIRECT = "direct"
SERVER = "server"
ROUTES = (DIRECT, SERVER)


class ConfigError(Exception):
    """files.csv says something that cannot be acted on."""


@dataclass(frozen=True)
class Entry:
    name: str
    source: str
    route: str

    #  PureWindowsPath, not Path: on a non-Windows box Path would treat the
    #  whole UNC string as one filename and the self-test would pass while
    #  lying about it.
    @property
    def filename(self) -> str:
        return PureWindowsPath(self.source).name

    #  The rstrip is not cosmetic.  For a file at a share ROOT - the common
    #  case - .parent is '\\host\share\' with a trailing backslash, because
    #  that is the UNC anchor.  Passed to robocopy as "\\host\share\" the
    #  final backslash escapes the closing quote and the whole command line
    #  shifts by one argument, which fails with a message about the
    #  destination that has nothing to do with the real cause.
    @property
    def directory(self) -> str:
        return str(PureWindowsPath(self.source).parent).rstrip("\\")


def parse(rows) -> list[Entry]:
    """rows is anything csv.DictReader yields.  Raises on the first problem.

    Loud beats lenient here.  A row silently dropped for a typo'd Route is a
    file that quietly does not arrive, and you find out about it when
    whatever reads it downstream is still on yesterday's copy.
    """
    out: list[Entry] = []
    seen: set[str] = set()

    for n, r in enumerate(rows, start=2):        # 2: row 1 is the header
        name = (r.get("Name") or "").strip()
        source = (r.get("SourcePath") or "").strip()
        route = (r.get("Route") or "").strip().lower()

        if not name and not source and not route:
            continue                             # a blank line Excel left

        if not name:
            raise ConfigError(f"row {n}: no Name")
        if not source:
            raise ConfigError(f"row {n}: {name} has no SourcePath")
        if route not in ROUTES:
            raise ConfigError(
                f"row {n}: {name} has Route={route!r}, "
                f"which is not one of {' '.join(ROUTES)}")
        if not source.startswith("\\\\"):
            raise ConfigError(
                f"row {n}: {name} has SourcePath={source!r}, which is not a "
                "UNC path.  It has to start with two backslashes - a mapped "
                "drive letter means something different on the server than "
                "it does here, and S: may not even exist there.")
        if source.endswith("\\") or "*" in source or "?" in source:
            raise ConfigError(
                f"row {n}: {name} has SourcePath={source!r}.  It has to name "
                "one file, not a directory or a wildcard.")
        if name in seen:
            raise ConfigError(f"row {n}: {name} appears twice")

        seen.add(name)
        out.append(Entry(name=name, source=source, route=route))

    if not out:
        raise ConfigError("no rows")
    return out


def load(path) -> list[Entry]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"{p} not found")
    #  utf-8-sig: Excel writes a BOM, and without it the first header becomes
    #  '\ufeffName' and every row looks like it has no Name.
    with p.open(newline="", encoding="utf-8-sig") as fh:
        return parse(csv.DictReader(fh))


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def rejects(name, rows):
        nonlocal ok
        try:
            parse(rows)
        except ConfigError:
            print(f"  ok    {name}")
            return
        ok = False
        print(f"  FAIL  {name}   was accepted")

    print("filelist --self-test\n\nthe good case")
    e = parse([{"Name": "positions",
                "SourcePath": r"\\finance-nas\risk\Positions.csv",
                "Route": "direct"}])
    check("one row", len(e), 1)
    check("name", e[0].name, "positions")
    check("route", e[0].route, DIRECT)
    check("filename splits off the tail", e[0].filename, "Positions.csv")
    check("directory is the rest", e[0].directory, r"\\finance-nas\risk")

    print("\nRoute is case and space insensitive")
    e = parse([{"Name": "p", "SourcePath": r"\\a\b\c.csv",
                "Route": "  Server "}])
    check("Server -> server", e[0].route, SERVER)

    print("\nblank lines are skipped, not errors")
    e = parse([{"Name": "", "SourcePath": "", "Route": ""},
               {"Name": "p", "SourcePath": r"\\a\b\c.csv",
                "Route": "direct"}])
    check("one row survives", len(e), 1)

    print("\nwhat it refuses")
    rejects("no rows at all", [])
    rejects("a missing Name",
            [{"Name": "", "SourcePath": r"\\a\b\c.csv", "Route": "direct"}])
    rejects("a missing SourcePath",
            [{"Name": "p", "SourcePath": "", "Route": "direct"}])
    rejects("a route that is neither",
            [{"Name": "p", "SourcePath": r"\\a\b\c.csv", "Route": "remote"}])
    rejects("a drive letter instead of a UNC path",
            [{"Name": "p", "SourcePath": r"S:\risk\c.csv", "Route": "direct"}])
    rejects("a directory",
            [{"Name": "p", "SourcePath": "\\\\a\\b\\", "Route": "direct"}])
    rejects("a wildcard",
            [{"Name": "p", "SourcePath": r"\\a\b\*.csv", "Route": "direct"}])
    rejects("the same Name twice",
            [{"Name": "p", "SourcePath": r"\\a\b\c.csv", "Route": "direct"},
             {"Name": "p", "SourcePath": r"\\a\b\d.csv", "Route": "direct"}])

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
