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
import os
import datetime as dt
import re
import shutil
import zipfile
from pathlib import Path

COLUMNS = ["#Time", "Last", "Volume", "Condition", "Exchange", "MicCode"]

PREFIX = "raw-"
SUFFIX = ".csv"

#  raw-<code>-<8 digits>.csv, where <code> may itself contain '-' and ' '
#  (SCB-R TB).  Anchored on the date, so the code is whatever is left.
NAME_RE = re.compile(r"^raw-(?P<bbg>.+)-(?P<date>\d{8})\.csv$")


def safe(code: str) -> str:
    """A code as it may appear in a path: 'LPN/F TB' -> 'LPN_F TB'.

    THAILAND'S FOREIGN BOARD is spelt with a slash - LPN/F TB - and a slash
    in a path is a folder.  Written as it was, LPN/F TB's day landed at
    LPN/F TB/raw-LPN/F TB-20260903.csv: three folders deep, under a name
    existing_dates never matched, so every run fetched it again.  The desk
    names these LPN_F TB, folder and file alike.

    THE OTHER CHARACTERS WINDOWS REFUSES ARE DROPPED.  The crosscode carries
    `HPHT* SP`, and on 2026-09-22 mkdir refused it and stopped a run at
    chunk 1,043 of 37,530.  `* ? " < > | : \\` can never be in a Windows
    name, so they are removed - HPHT* SP is HPHT SP on disk."""
    out = (code or "").strip().replace("/", "_")
    for ch in _REFUSED:
        out = out.replace(ch, "")
    #  Windows also refuses a name ending in a space or a dot.
    return " ".join(out.split()).rstrip(". ")


_REFUSED = '*?"<>|:\\'


def filename(bbg: str, date) -> str:
    """('7203 JT', 2026-09-03) -> 'raw-7203 JT-20260903.csv'."""
    return f"{PREFIX}{safe(bbg)}-{date:%Y%m%d}{SUFFIX}"


def folder(crosscode_bbg: str) -> str:
    """The directory one name's files live in: its crosscode BloombergCode.

    The same string as the file's own code today: config/composites.csv
    renames folder and file together.  They are still passed separately
    because the store has held both shapes - a folder "600000 C1" holding
    "raw-600000 CG-...csv", from when the MIC renamed the file alone - and
    existing_dates must still find those.

    ONE FOLDER PER NAME, and it is what makes a real backfill possible.
    existing_dates is called once per name, and when every file shared one
    directory each of those calls listed the WHOLE store: 59,013 names over
    60 days is upwards of a million files, listed 59,013 times.  Measured at
    170ms per name over only 8,000 files, which extrapolates to days of
    work before the first query is sent.  Per name, the listing is the
    handful of days that name actually has.

    """
    return safe(crosscode_bbg)


def path(directory, crosscode_bbg: str, bbg: str, date) -> Path:
    """Where one name's file for one day goes.  Two codes, deliberately:
    the folder's and the file's.  See folder()."""
    return Path(directory) / folder(crosscode_bbg) / filename(bbg, date)


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


def existing_dates(directory, crosscode_bbg: str, bbg: str) -> set:
    """Every date already on disk for one name.

    Only that name's own folder is listed.  The FILE code is still checked
    against each filename, so a file that somehow landed in the wrong
    folder is ignored rather than counted as a day already fetched."""
    d = Path(directory) / folder(crosscode_bbg)
    if not d.is_dir():
        return set()
    out = set()
    for entry in d.iterdir():
        parsed = parse_filename(entry.name)
        if parsed and parsed[0] == safe(bbg):
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


def migrate_flat(directory, folder_of=None, dry_run=False) -> dict:
    """Move files written before there were folders.  Returns a tally.

    A DAY ALREADY FETCHED MUST NOT BE FETCHED AGAIN, and that is the whole
    point of this.  When every file sat loose in the store, existing_dates
    found them; once it looks in one folder per name, the same file is
    invisible, the day reads as untried, and the run refetches and rewrites
    something it already had.

    The filename carries the FILE code and the folder is named for the
    CROSSCODE code - the two differ for China - so `folder_of` maps one to
    the other.  A code it does not know keeps its own name as the folder,
    which is right everywhere except China and is the best that can be done
    for a name the crosscode no longer carries.

    ONE PASS OVER THE ROOT, not one per name.  Only loose files are looked
    at; anything already in a folder is left alone, and so is any file that
    is not one of ours - the miss cache lives at the root and stays there.

    A destination that already exists is NOT overwritten.  The file in the
    folder is the one existing_dates found, so it is the one that counts,
    and the loose copy is left behind to be looked at rather than deleted
    silently."""
    #  Keyed as the FILENAME spells the code, which is safe()'s spelling.
    folder_of = {safe(k): v for k, v in (folder_of or {}).items()}
    d = Path(directory)
    tally = {"moved": 0, "already there": 0, "unknown code": 0}
    if not d.is_dir():
        return tally
    for entry in sorted(d.iterdir()):
        if not entry.is_file():
            continue
        parsed = parse_filename(entry.name)
        if not parsed:
            continue
        code = parsed[0]
        if code in folder_of:
            target = folder_of[code]
        else:
            target = code
            tally["unknown code"] += 1
        dest = d / folder(target) / entry.name
        if dest.exists():
            tally["already there"] += 1
            continue
        tally["moved"] += 1
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            entry.replace(dest)
    return tally


def migrate_slashed(directory, names, dry_run=False) -> int:
    """Move files written before safe() existed to where they belong now.
    `names` is [(crosscode code, file code)].  Returns how many moved.

    Only codes with a slash are looked at, and only at the exact place the
    old path put them - so nothing else in the store is touched.  The
    folders left empty are removed; anything else in them is left alone."""
    root, moved = Path(directory), 0
    for cc, bbg in names:
        if "/" not in (cc or "") + (bbg or ""):
            continue
        probe = root / cc.strip() / f"{PREFIX}{bbg.strip()}-00000000{SUFFIX}"
        old_dir, head = probe.parent, probe.name[:-len("00000000" + SUFFIX)]
        if not old_dir.is_dir():
            continue
        for f in sorted(old_dir.iterdir()):
            stamp = f.name[len(head):-len(SUFFIX)]
            if not (f.is_file() and f.name.startswith(head)
                    and f.name.endswith(SUFFIX) and len(stamp) == 8
                    and stamp.isdigit()):
                continue
            try:
                day = dt.datetime.strptime(stamp, "%Y%m%d").date()
            except ValueError:
                continue
            dest = path(root, cc, bbg, day)
            if dest.exists():
                continue
            moved += 1
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                f.replace(dest)
        if not dry_run:
            gone = old_dir
            while gone != root and gone.is_dir() and not any(gone.iterdir()):
                gone.rmdir()
                gone = gone.parent
    return moved


#  =============================================================================
#  VENUE ZIPS (--compress_venues).  One archive per FidessaMarket at the root
#  of the store - SET-MAIN.zip - holding the same layout the folders do:
#  "7203 JT/raw-7203 JT-20260903.csv".
#  =============================================================================

def venue_zip(directory, venue: str) -> Path:
    return Path(directory) / f"{safe(venue)}.zip"


def zipped_dates(directory) -> dict:
    """{(folder, file code): {date, ...}} for every day inside every zip at
    the root of the store.

    A DAY IN A ZIP IS A DAY ON DISK.  Once --compress_venues has zipped a
    file and deleted it, existing_dates no longer sees it; without this the
    next run would fetch the whole window again.  Read from the zip's
    directory alone, so it costs nothing however big the archive is.  A zip
    that cannot be read stops the run: skipping it would refetch everything
    it holds, and then fail to add to it."""
    out = {}
    root = Path(directory)
    if not root.is_dir():
        return out
    for z in sorted(root.glob("*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile as e:
            raise ValueError(f"{z} is not a readable zip ({e}); move it "
                             f"aside or repair it before running") from e
        for n in names:
            parts = n.split("/")
            parsed = parse_filename(parts[-1]) if len(parts) == 2 else None
            if parsed:
                out.setdefault((parts[0], parsed[0]), set()).add(parsed[1])
    return out


def compress_venue(directory, venue: str, folders, dry_run=False) -> dict:
    """Move every day file in `folders` into the venue's zip.

    ONLY WHAT IS MISSING IS ADDED.  An existing SET-MAIN.zip keeps what it
    holds and gains this run's files - a new day, or a new stock.  A file it
    already holds under the same name (today, refetched with --today) REPLACES
    that entry rather than sitting beside it as a duplicate.

    NOTHING IS DELETED UNTIL THE NEW ZIP IS KNOWN GOOD.  It is written as
    SET-MAIN.zip.part - a copy of the old one with the new files appended,
    or rebuilt without the entries being replaced - every added file is read
    back and compared byte for byte, and only then does it replace the old
    zip and the loose files go.  A crash anywhere before that leaves the old
    zip and every file exactly as they were.

    Every day file in those folders is swept, not only this run's: a run
    that died before its zip step left files behind, and they belong in the
    zip too.  Returns {"added", "replaced", "zip"}."""
    root = Path(directory)
    target = venue_zip(root, venue)
    loose = []
    for f in sorted(set(folders)):
        d = root / f
        if d.is_dir():
            loose += [(e, f"{f}/{e.name}") for e in sorted(d.iterdir())
                      if e.is_file() and parse_filename(e.name)]
    held = set()
    if target.exists():
        with zipfile.ZipFile(target) as zf:
            held = set(zf.namelist())
    replaced = held & {a for _p, a in loose}
    tally = {"added": len(loose) - len(replaced), "replaced": len(replaced),
             "zip": target}
    if not loose or dry_run:
        return tally

    part = target.with_name(target.name + ".part")
    if held and not replaced:
        #  The common case: a byte copy, then an append.
        shutil.copyfile(target, part)
        mode = "a"
    else:
        mode = "w"
    with zipfile.ZipFile(part, mode, compression=zipfile.ZIP_DEFLATED) as z:
        if mode == "w" and held:
            with zipfile.ZipFile(target) as old:
                for info in old.infolist():
                    if info.filename not in replaced:
                        z.writestr(info, old.read(info.filename))
        for p, arc in loose:
            z.write(p, arc)
    with zipfile.ZipFile(part) as z:
        for p, arc in loose:
            if z.read(arc) != p.read_bytes():
                part.unlink()
                raise OSError(f"{arc} did not read back from {part.name} as "
                              f"it was written; nothing deleted")
    os.replace(part, target)
    for p, _arc in loose:
        p.unlink()
    for f in set(folders):
        try:
            (root / f).rmdir()              # only if now empty
        except OSError:
            pass
    return tally


def write(path, rows, mic: str, tz_label: str = "") -> int:
    """One name, one day.  Returns the row count written.

    newline="" is required, not cosmetic: without it csv writes \\r\\r\\n on
    Windows and every other line of the file reads as blank."""
    return write_rows(path, [(r["time"], _num(r["price"]), _num(r["size"]),
                              condition(r["cond"]), r["ex"], mic)
                             for r in rows], tz_label)


#  A PRINT WITH NO CONDITION CODE IS WRITTEN "#N/A N.A.", not blank.  That is
#  what the Bloomberg files carry, and whatever reads these was written
#  against them.  qatt leaves the column empty instead.
NO_CONDITION = "#N/A N.A."


def condition(cond) -> str:
    return (cond or "").strip() or NO_CONDITION


def write_rows(path, rows, tz_label: str = "") -> int:
    """The same file from rows already formatted - six strings each, in
    COLUMNS order.  write() goes through here too, so there is one header
    and one quoting rule whichever path made the rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    #  A FILE UNDER ITS REAL NAME IS A FINISHED FILE.  The name is what
    #  marks a day as done - existing_dates never looks inside - so a run
    #  killed mid-write must not leave a half file under it, or that day is
    #  skipped forever.  Written as .part, which NAME_RE does not match, and
    #  renamed into place in one step once it is complete.
    part = path.with_name(path.name + ".part")
    with part.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS + ([tz_label] if tz_label else []))
        w.writerows(rows)
    os.replace(part, path)
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
        p = path(d, "EAU AU", "EAU AU", D(2026, 8, 17))
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
        check("an empty condition is Bloomberg's marker, never blank and "
              "never the word None",
              lines[3], "10:00:15,0.105,4998,#N/A N.A.,H,XASX")
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
              existing_dates(d, "EAU AU", "EAU AU"), {D(2026, 8, 17)})
        check("and says nothing about a name it does not have",
              existing_dates(d, "7203 JT", "7203 JT"), set())
        check("a directory that does not exist is empty, not an error",
              existing_dates(Path(d) / "nope", "EAU AU", "EAU AU"), set())

        check("the file sits in a folder named for the code, not loose in "
              "the store", p.parent.name, "EAU AU")
        check("and the store itself holds folders, not files",
              [x.name for x in sorted(Path(d).iterdir()) if x.is_file()],
              ["no_label.csv"])

        #  CHINA IS THE ONE PLACE THE TWO CODES DIFFER.  The folder is the
        #  crosscode's line; the file is what the consumer asks for.
        cn = path(d, "600000 C1", "600000 CG", D(2026, 9, 3))
        write(cn, rows, "XSHG", "China Standard Time")
        check("the folder keeps the crosscode's C1", cn.parent.name,
              "600000 C1")
        check("while the file inside takes the MIC's CG", cn.name,
              "raw-600000 CG-20260903.csv")
        check("and the pair is found again by the same two codes",
              existing_dates(d, "600000 C1", "600000 CG"), {D(2026, 9, 3)})
        check("asking with the file code as the folder finds nothing, "
              "which is why both are passed",
              existing_dates(d, "600000 CG", "600000 CG"), set())

    print("\nfiles written before there were folders")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        flat = [filename("7203 JT", D(2026, 9, 3)),
                filename("7203 JT", D(2026, 9, 4)),
                filename("600000 CG", D(2026, 9, 3)),
                filename("SCB-R TB", D(2026, 9, 3))]
        for f in flat:
            (root / f).write_text("x", encoding="utf-8")
        (root / "_no_data.csv").write_text("x", encoding="utf-8")

        check("a loose file is invisible to the folder lookup, which is "
              "why it has to move at all",
              existing_dates(root, "7203 JT", "7203 JT"), set())

        seen = migrate_flat(root, {"600000 CG": "600000 C1"}, dry_run=True)
        check("a dry run counts them and moves nothing",
              (seen["moved"], sorted(x.name for x in root.iterdir()
                                     if x.is_file())),
              (4, sorted(flat + ["_no_data.csv"])))

        tally = migrate_flat(root, {"600000 CG": "600000 C1"})
        check("all four move", tally["moved"], 4)
        check("and the days are found again, which is the point",
              existing_dates(root, "7203 JT", "7203 JT"),
              {D(2026, 9, 3), D(2026, 9, 4)})
        check("China's file goes to the CROSSCODE folder, not its own code",
              existing_dates(root, "600000 C1", "600000 CG"),
              {D(2026, 9, 3)})
        check("a code with a hyphen is not split by the move either",
              existing_dates(root, "SCB-R TB", "SCB-R TB"), {D(2026, 9, 3)})
        check("a code the map does not know keeps its own name, and is "
              "counted rather than lost", tally["unknown code"], 3)
        check("the miss cache is not one of ours and stays at the root",
              (root / "_no_data.csv").is_file(), True)
        check("nothing else is left loose",
              [x.name for x in root.iterdir() if x.is_file()],
              ["_no_data.csv"])

        check("a second run has nothing to do", migrate_flat(root),
              {"moved": 0, "already there": 0, "unknown code": 0})

        #  the same day, loose AND in its folder
        (root / filename("7203 JT", D(2026, 9, 3))).write_text("x",
                                                               encoding="utf-8")
        again = migrate_flat(root)
        check("a day that is already in the folder is not overwritten by "
              "the loose copy", (again["moved"], again["already there"]),
              (0, 1))

    print("\nThailand's foreign board: LPN/F TB is LPN_F TB on disk")
    lpn = D(2026, 9, 3)
    check("the slash becomes an underscore in the file name",
          filename("LPN/F TB", lpn), "raw-LPN_F TB-20260903.csv")
    check("and in the folder", folder("LPN/F TB"), "LPN_F TB")
    check("so the whole path is one folder and one file, not four levels",
          path("out", "LPN/F TB", "LPN/F TB", lpn).parts[-3:],
          ("out", "LPN_F TB", "raw-LPN_F TB-20260903.csv"))
    check("a code with no slash is unchanged", filename("7203 JT", lpn),
          "raw-7203 JT-20260903.csv")

    print("\ncharacters Windows refuses in a name")
    check("HPHT* SP is HPHT SP on disk - folder", folder("HPHT* SP"),
          "HPHT SP")
    check("and file", filename("HPHT* SP", lpn), "raw-HPHT SP-20260903.csv")
    check("every refused character goes, and no double space is left",
          safe('A*?"<>|:\\B *C SP'), "AB C SP")
    check("nor a trailing dot or space, which Windows also refuses",
          safe("ABC SP. "), "ABC SP")
    with tempfile.TemporaryDirectory() as d:
        p = path(d, "HPHT* SP", "HPHT* SP", lpn)
        write_rows(p, [])
        check("so the folder and file can actually be made",
              (p.parent.name, p.name), ("HPHT SP", "raw-HPHT SP-20260903.csv"))
        check("and the day is found again on the next run",
              existing_dates(d, "HPHT* SP", "HPHT* SP"), {lpn})
    with tempfile.TemporaryDirectory() as d:
        write_rows(path(d, "LPN/F TB", "LPN/F TB", lpn), [])
        check("a day on disk is found under the crosscode's own spelling - "
              "without this, every run fetched it again",
              existing_dates(d, "LPN/F TB", "LPN/F TB"), {lpn})

    with tempfile.TemporaryDirectory() as d:
        #  EXACTLY where the old code put them: Path / "LPN/F TB" /
        #  "raw-LPN/F TB-<date>.csv", four levels deep.
        old = [Path(d) / "LPN/F TB" / f"raw-LPN/F TB-2026090{k}.csv"
               for k in (3, 4)]
        for f in old:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        keep = Path(d) / "LPN TB" / "raw-LPN TB-20260903.csv"
        keep.parent.mkdir(parents=True)
        keep.write_text("y", encoding="utf-8")
        pairs = [("LPN/F TB", "LPN/F TB"), ("LPN TB", "LPN TB")]

        check("--dry-run counts them and moves nothing",
              (migrate_slashed(d, pairs, dry_run=True), old[0].exists()),
              (2, True))
        check("a real run moves both days", migrate_slashed(d, pairs), 2)
        check("to LPN_F TB, under the new name",
              existing_dates(d, "LPN/F TB", "LPN/F TB"),
              {D(2026, 9, 3), D(2026, 9, 4)})
        check("the contents come with them",
              path(d, "LPN/F TB", "LPN/F TB", lpn).read_text(), "x")
        check("the nested folders left empty are gone",
              sorted(p.name for p in Path(d).iterdir()),
              ["LPN TB", "LPN_F TB"])
        check("the ordinary LPN TB next door is not touched",
              keep.read_text(), "y")
        check("and a second run finds nothing left to move",
              migrate_slashed(d, pairs), 0)

    with tempfile.TemporaryDirectory() as d:
        loose = Path(d) / filename("LPN/F TB", lpn)
        loose.write_text("z", encoding="utf-8")
        migrate_flat(d, {"LPN/F TB": "LPN/F TB"})
        check("a loose file of a slash code goes to its underscore folder",
              existing_dates(d, "LPN/F TB", "LPN/F TB"), {lpn})

    print("\na print with no condition code")
    check("blank is written as Bloomberg writes it", condition(""),
          "#N/A N.A.")
    check("and so is whitespace, which is the same absence",
          condition("  "), "#N/A N.A.")
    check("nothing at all is too", condition(None), "#N/A N.A.")
    check("a code the exchange did send is untouched", condition("T"), "T")
    check("with its surrounding space trimmed", condition(" XT "), "XT")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.csv"
        write(p, [{"time": "09:00:00", "price": Decimal("1"),
                   "size": Decimal("100"), "cond": "", "ex": ""}], "XBKK")
        check("so the cell is never empty in the file",
              p.read_text(encoding="utf-8").splitlines()[1],
              "09:00:00,1,100,#N/A N.A.,,XBKK")

    print("\n--compress_venues: one zip per venue")
    import zipfile as zf_

    def day_file(root, cc, code, day, text):
        p = path(root, cc, code, day)
        write_rows(p, [(text, "1", "100", "", "T", "XBKK")])
        return p

    with tempfile.TemporaryDirectory() as d:
        a, b = D(2026, 9, 3), D(2026, 9, 4)
        day_file(d, "PTT TB", "PTT TB", a, "09:00:00")
        day_file(d, "LPN/F TB", "LPN/F TB", a, "09:00:01")
        folders = [folder("PTT TB"), folder("LPN/F TB")]

        t = compress_venue(d, "SET-MAIN", folders, dry_run=True)
        check("--dry-run counts and touches nothing",
              (t["added"], venue_zip(d, "SET-MAIN").exists(),
               existing_dates(d, "PTT TB", "PTT TB")), (2, False, {a}))

        t = compress_venue(d, "SET-MAIN", folders)
        z = venue_zip(d, "SET-MAIN")
        check("SET-MAIN.zip is made, next to the folders", z.name,
              "SET-MAIN.zip")
        with zf_.ZipFile(z) as f:
            check("holding the same layout as the folders",
                  sorted(f.namelist()),
                  ["LPN_F TB/raw-LPN_F TB-20260903.csv",
                   "PTT TB/raw-PTT TB-20260903.csv"])
        check("the files and their emptied folders are gone",
              sorted(e.name for e in Path(d).iterdir()), ["SET-MAIN.zip"])
        check("and nothing is left as .part", list(Path(d).glob("*.part")),
              [])
        check("a zipped day still counts as done for the next run",
              zipped_dates(d)[("PTT TB", "PTT TB")], {a})

        day_file(d, "PTT TB", "PTT TB", b, "09:00:02")
        day_file(d, "AOT TB", "AOT TB", a, "09:00:03")
        t = compress_venue(d, "SET-MAIN", [folder("PTT TB"),
                                           folder("AOT TB")])
        with zf_.ZipFile(z) as f:
            names = sorted(f.namelist())
            check("a second run ADDS the missing day and the new stock, and "
                  "keeps what was there", names,
                  ["AOT TB/raw-AOT TB-20260903.csv",
                   "LPN_F TB/raw-LPN_F TB-20260903.csv",
                   "PTT TB/raw-PTT TB-20260903.csv",
                   "PTT TB/raw-PTT TB-20260904.csv"])
        check("counted as added, none replaced", (t["added"], t["replaced"]),
              (2, 0))

        day_file(d, "PTT TB", "PTT TB", b, "15:00:00")
        t = compress_venue(d, "SET-MAIN", [folder("PTT TB")])
        with zf_.ZipFile(z) as f:
            names = f.namelist()
            body = f.read("PTT TB/raw-PTT TB-20260904.csv").decode()
        check("a day fetched again REPLACES its entry - no duplicate",
              (t["replaced"], names.count("PTT TB/raw-PTT TB-20260904.csv")),
              (1, 1))
        check("with the new content", "15:00:00" in body, True)
        check("and the other entries survive the rebuild", len(names), 4)

        z.write_bytes(b"not a zip")
        try:
            zipped_dates(d)
            check("a broken zip raised", False, True)
        except ValueError as e:
            check("a broken zip stops the run rather than refetch all it "
                  "held", "SET-MAIN.zip" in str(e), True)

    print("\na file under its real name is a finished file")
    with tempfile.TemporaryDirectory() as d:
        day = D(2026, 9, 3)
        p = path(d, "7203 JT", "7203 JT", day)
        good = ("09:00:00", "1", "100", "", "T", "XTKS")
        try:
            #  the second row is not a row: the write dies half way, as a
            #  killed run's would
            write_rows(p, [good, 5])
        except Exception:                                   # noqa: BLE001
            pass
        check("a write that dies half way leaves nothing under the real "
              "name", p.exists(), False)
        check("so the day still reads as missing, and the next run fetches "
              "it", existing_dates(d, "7203 JT", "7203 JT"), set())
        write_rows(p, [good])
        check("the next write puts the whole file in place",
              p.read_text(encoding="utf-8").splitlines()[1],
              "09:00:00,1,100,,T,XTKS")
        check("and the day counts as done",
              existing_dates(d, "7203 JT", "7203 JT"), {day})
        check("with no .part left behind", sorted(
            f.name for f in p.parent.iterdir()), [p.name])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
