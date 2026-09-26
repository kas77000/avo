#!/usr/bin/env python3
"""The cutover instrument: two output folders, compared in ONE direction.

Every file the new process generated must exist in the old process's folder,
and its content must agree.  A file the old folder has and the new one does
not is NOT a finding and is never looked for - a run fetches only the days it
has not already tried, so the old tree is always the larger of the two and
the difference says nothing about correctness.

THE NEW TREE IS WALKED, NEVER THE OLD ONE.  Everything else follows from
that.  Work is proportional to what a run generated - a few hundred files
after a daily top-up - rather than to the size of the archive it is measured
against.  The two layouts are identical, so the counterpart is found by
reusing the relative path verbatim: one stat() per generated file, no index,
flat memory.  The README's own figure for the store is 59,013 names over 60
days, upwards of a million files, and indexing that to compare two hundred
is the wrong trade.

    THE CHINA CASE IS WHAT THAT TRADE GIVES UP, and it is the first thing to
    suspect if Chinese names ever report `missing` in bulk.  A folder is
    named for the CROSSCODE code and the file inside it for the MIC code -
    "600000 C1/raw-600000 CG-20260817.csv" - so the two strings differ for
    Shanghai and Shenzhen and nowhere else.  If the old job foldered those
    by the file's own code, every one of them reads as missing here.  The
    report keeps `folder` and `code` in separate columns so that is visible
    at a glance rather than buried.

BYTE EQUALITY IS NOT THE BAR.  The old files came from the Bloomberg add-in
and the new ones from qatt, so the two agree on values and have no reason to
agree on spelling or on the order of prints inside one second.  See
compare_rows().

THE OLD PROCESS COMPRESSES FILES ONCE THEY ARE OLD ENOUGH.  Folders keep
their names; inside one, a file may be raw-7203 JT-20260612.csv.gz instead of
.csv.  So an old file missing as .csv is read from .csv.gz, decompressed in
memory - nothing is extracted to disk - and only a file that is neither is
missing.  A .gz that will not decompress is UNREADABLE, reported like any
other finding rather than stopping the run.

    python compare.py OLD_DIR NEW_DIR
    python compare.py OLD_DIR NEW_DIR --report somewhere/else.csv
    python compare.py --self-test
"""

from __future__ import annotations

import csv
import gzip
import os
import zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ticksfile

#  Worst first.  A file reports the FIRST that applies: a missing file is
#  never also a header difference, because there is no header to compare it
#  against.
MISSING = "missing"
UNREADABLE = "unreadable"
HEADER = "header"
DIFFERS = "differs"
OK = "ok"

REPORT_COLUMNS = ["folder", "code", "date", "status",
                  "rows_old", "rows_new", "detail"]

DEFAULT_REPORT = "compare-report.csv"

#  How many problem files print their full breakdown to the terminal.  The
#  rest are in the report; a small run needs no second file, a large one
#  would be unreadable either way.
INLINE = 5

#  Per file, inside one column's breakdown.
EXAMPLES = 3


# =============================================================================
# PURE - no disk
# =============================================================================

def _number(text):
    """Last and Volume as a value rather than as a spelling.

    3833 and 3833.0 are the same print.  ticksfile._num() deliberately keeps
    whatever scale the source carried - 0.10 and 0.1 are different ticks -
    so the two sources agree on the number and differ on how it is written,
    and without this, formatting alone would report a difference on most
    rows of most files.  Decimal is what LimitUpDown.compare uses for the
    same reason.

    A cell that is not a number keeps its text, so a corrupt value still
    compares and still shows up, rather than being silently read as empty."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return t


def normalise(cells) -> tuple:
    """One row, comparable.  Short rows pad and long ones truncate, so a
    ragged line is a difference in its cells rather than an exception."""
    c = (list(cells) + [""] * len(ticksfile.COLUMNS))[:len(ticksfile.COLUMNS)]
    return (c[0].strip(), _number(c[1]), _number(c[2]),
            c[3].strip(), c[4].strip(), c[5].strip())


def bucket(rows) -> dict:
    """Rows grouped by timestamp, each group a multiset.

    A TIMESTAMP IS NOT UNIQUE - a liquid name prints many times in the same
    second - so the group is a count per distinct row, not a row.  Comparing
    groups as multisets is what makes order within a second a non-difference,
    which is right: the two sources have no reason to sequence a second the
    same way.

    Positional alignment was rejected for the reason it is always rejected:
    one extra print early in the day shifts every row after it, and a single
    insertion turns the rest of the file into noise."""
    out = {}
    for r in rows:
        out.setdefault(r[0], {})
        out[r[0]][r] = out[r[0]].get(r, 0) + 1
    return out


def _take(group: dict) -> list:
    """A multiset back to a flat list of rows."""
    return [r for r, n in group.items() for _ in range(n)]


def _leftovers(old_group: dict, new_group: dict):
    """What does not cancel between two multisets, and how much did."""
    matched = 0
    left_old, left_new = {}, {}
    for r, n in old_group.items():
        m = new_group.get(r, 0)
        matched += min(n, m)
        if n > m:
            left_old[r] = n - m
    for r, n in new_group.items():
        m = old_group.get(r, 0)
        if n > m:
            left_new[r] = n - m
    return matched, _take(left_old), _take(left_new)


def compare_rows(old_rows, new_rows) -> dict:
    """The body of two files.

    Rows bucket by timestamp and the buckets compare as multisets.  When a
    bucket is left with EXACTLY ONE unmatched row on each side they pair,
    and the difference is attributed to the column that actually moved -
    which is what turns an opaque "row differs" into "Last differ 2".  With
    more than one left on each side no pairing is guessed; they are reported
    as unpaired counts, because picking one of several possible pairings
    would invent a finding."""
    old_b, new_b = bucket(old_rows), bucket(new_rows)

    only_new_times = sorted(set(new_b) - set(old_b))
    only_old_times = sorted(set(old_b) - set(new_b))

    matched = 0
    unpaired_old = unpaired_new = 0
    columns, examples = {}, {}

    for t in sorted(set(old_b) & set(new_b)):
        m, lo, ln = _leftovers(old_b[t], new_b[t])
        matched += m
        if len(lo) == 1 and len(ln) == 1:
            for i in range(1, len(ticksfile.COLUMNS)):
                if lo[0][i] != ln[0][i]:
                    col = ticksfile.COLUMNS[i]
                    columns[col] = columns.get(col, 0) + 1
                    ex = examples.setdefault(col, [])
                    if len(ex) < EXAMPLES:
                        ex.append((t, _show(lo[0][i]), _show(ln[0][i])))
        else:
            unpaired_old += len(lo)
            unpaired_new += len(ln)

    #  A row at a timestamp the other side does not have at all is unpaired
    #  by definition - there is nothing to pair it with.
    unpaired_new += sum(sum(new_b[t].values()) for t in only_new_times)
    unpaired_old += sum(sum(old_b[t].values()) for t in only_old_times)

    return {"matched": matched,
            "rows_old": len(old_rows), "rows_new": len(new_rows),
            "only_new_times": len(only_new_times),
            "only_old_times": len(only_old_times),
            "unpaired_old": unpaired_old, "unpaired_new": unpaired_new,
            "columns": columns, "examples": examples}


def _show(value) -> str:
    """A normalised cell, back to something readable in a report."""
    if value is None:
        return ""
    return str(value)


def agrees(d: dict) -> bool:
    return not (d["columns"] or d["unpaired_old"] or d["unpaired_new"])


def detail(d: dict) -> str:
    """The one-line summary the report carries."""
    parts = [f"{c} differ {n}" for c, n in sorted(d["columns"].items())]
    if d["only_new_times"]:
        parts.append(f"times only in new {d['only_new_times']}")
    if d["only_old_times"]:
        parts.append(f"times only in old {d['only_old_times']}")
    if d["unpaired_new"]:
        parts.append(f"rows only in new {d['unpaired_new']}")
    if d["unpaired_old"]:
        parts.append(f"rows only in old {d['unpaired_old']}")
    return "; ".join(parts)


# =============================================================================
# DISK
# =============================================================================

def read_ticks(path):
    """One file: its header cells, and its rows normalised.

    utf-8-sig because the old files came from the Bloomberg add-in and may
    carry a BOM; without it the first header cell reads as '\\ufeff#Time' and
    every file in the tree reports a header difference.

    Wholly blank lines are dropped.  ticksfile.write() takes care not to
    produce them - newline="" is why - but a file written before that, or by
    the old job, can carry them, and a blank line is not a print."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], []
    header = [c.strip() for c in rows[0]]
    return header, [normalise(r) for r in rows[1:]
                    if any(c.strip() for c in r)]


def compare_file(old_path, new_path) -> dict:
    """One generated file against its counterpart."""
    old_header, old_rows = read_ticks(old_path)
    new_header, new_rows = read_ticks(new_path)

    #  The trailing timezone label is part of this.  write() appends it as a
    #  seventh header cell, no data row has a seventh field, and whatever
    #  reads these files downstream was written against it.
    if old_header != new_header:
        return {"status": HEADER,
                "rows_old": len(old_rows), "rows_new": len(new_rows),
                "detail": f"old {','.join(old_header)} | "
                          f"new {','.join(new_header)}",
                "body": None}

    d = compare_rows(old_rows, new_rows)
    return {"status": OK if agrees(d) else DIFFERS,
            "rows_old": d["rows_old"], "rows_new": d["rows_new"],
            "detail": detail(d), "body": d}


def counterpart(old_root, rel):
    """The old process's copy of one file, or None.  The plain .csv first,
    then the same name with .gz on the end."""
    plain = Path(old_root) / rel
    if plain.is_file():
        return plain
    packed = plain.with_name(plain.name + ".gz")
    return packed if packed.is_file() else None


def generated(new_dir):
    """Every file the new process wrote, as (relative path, folder, code,
    date).  Anything else is yielded as a skip.

    ticksfile.parse_filename() is reused rather than a second regex written
    here.  Its docstring says why: a Bloomberg code can contain the
    separator, so 'SCB-R TB' only survives because the date is matched first
    and the code is whatever precedes it.  A second regex would be a second
    chance to get that wrong.

    os.walk, not rglob: at a million files the difference is measurable and
    neither builds a list."""
    root = Path(new_dir)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root)
            parsed = ticksfile.parse_filename(name)
            if not parsed:
                #  _no_data.csv at the root is the expected case - the miss
                #  cache is a new-process invention with no old counterpart.
                #  It is still counted, so a file that landed somewhere
                #  unintended is visible rather than silently dropped.
                yield rel, None, None, None
                continue
            code, date = parsed
            folder = rel.parent.as_posix()
            yield rel, ("" if folder == "." else folder), code, date


# =============================================================================
# THE RUN
# =============================================================================

def run(old_dir, new_dir, report_path=DEFAULT_REPORT) -> int:
    old_root, new_root = Path(old_dir), Path(new_dir)
    if not new_root.is_dir():
        raise SystemExit(f"not a directory: {new_root}")
    if not old_root.is_dir():
        raise SystemExit(f"not a directory: {old_root}")

    tally = {MISSING: 0, UNREADABLE: 0, HEADER: 0, DIFFERS: 0, OK: 0,
             "skipped": 0, "gz": 0}
    findings = []
    shown = 0

    print(f"comparing\n  new  {new_root}\n  old  {old_root}\n")

    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(report_path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(REPORT_COLUMNS)

        for rel, folder, code, date in generated(new_root):
            if code is None:
                tally["skipped"] += 1
                continue

            old_file = counterpart(old_root, rel)
            if old_file is None:
                r = {"status": MISSING, "rows_old": "", "rows_new": "",
                     "detail": "", "body": None}
            else:
                packed = old_file.suffix == ".gz"
                tally["gz"] += packed
                try:
                    r = compare_file(old_file, new_root / rel)
                except (OSError, EOFError, zlib.error, UnicodeDecodeError) as e:
                    #  A truncated or corrupt archive is a finding about
                    #  that one file, not a reason to stop comparing.
                    r = {"status": UNREADABLE, "rows_old": "",
                         "rows_new": "", "body": None,
                         "detail": f"{old_file.name}: {e}"}
                else:
                    if packed and r["status"] != OK:
                        r["detail"] = f"old read from .gz; {r['detail']}"

            tally[r["status"]] += 1
            if r["status"] == OK:
                continue

            w.writerow([folder, code, f"{date:%Y%m%d}", r["status"],
                        r["rows_old"], r["rows_new"], r["detail"]])
            if shown < INLINE:
                shown += 1
                findings.append((rel, r))

    for rel, r in findings:
        describe(rel, r)

    total = sum(tally[k] for k in (MISSING, UNREADABLE, HEADER, DIFFERS, OK))
    print(f"\n  files generated     {total:8d}")
    print(f"  missing from old    {tally[MISSING]:8d}")
    if tally[UNREADABLE]:
        print(f"  old .gz unreadable  {tally[UNREADABLE]:8d}")
    print(f"  header differs      {tally[HEADER]:8d}")
    print(f"  content differs     {tally[DIFFERS]:8d}")
    print(f"  identical           {tally[OK]:8d}")
    print(f"  skipped (not ours)  {tally['skipped']:8d}")
    print(f"  old read from .gz   {tally['gz']:8d}")

    bad = tally[MISSING] + tally[UNREADABLE] + tally[HEADER] + tally[DIFFERS]
    if bad:
        print(f"\n  {bad} to look at -> {report_path}")
    else:
        print(f"\n  nothing to look at.  {report_path} has its header "
              f"and no rows.")
    #  Non-zero so this can gate a cutover step.  Deliberately unlike
    #  LimitUpDown's --compare, which returns 0 whatever it finds: a check
    #  that cannot fail cannot gate anything.
    return 1 if bad else 0


def describe(rel, r):
    """One problem file, in full, for the terminal."""
    print(f"{rel.as_posix()}  {r['status'].upper()}")
    if r["status"] == MISSING:
        print("  the new process generated it; the old folder has no "
              "such file")
        return
    if r["status"] in (HEADER, UNREADABLE):
        print(f"  {r['detail']}")
        return
    d = r["body"]
    print(f"  rows      old {d['rows_old']:,}   new {d['rows_new']:,}"
          f"   matched {d['matched']:,}")
    for c, n in sorted(d["columns"].items()):
        print(f"  {c:<12} differ {n}")
        for t, a, b in d["examples"].get(c, []):
            print(f"      {t}  old={a!r}  new={b!r}")
    for label, key in (("times only in new", "only_new_times"),
                       ("times only in old", "only_old_times"),
                       ("rows only in new", "unpaired_new"),
                       ("rows only in old", "unpaired_old")):
        if d[key]:
            print(f"  {label} {d[key]}")
    print()


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import datetime as dt
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def rows(*text):
        return [normalise(line.split(",")) for line in text]

    print("compare --self-test\n\na number is a value, not a spelling")
    check("the same price written two ways is one price",
          _number("3833") == _number("3833.0"), True)
    check("and hashes the same, which is what lets the multiset cancel",
          hash(_number("3833")) == hash(_number("3833.0")), True)
    check("a trailing zero the source had is still not a different value",
          _number("0.10") == _number("0.1"), True)
    check("an empty cell is nothing", _number(""), None)
    check("something that is not a number keeps its text, so it still "
          "compares rather than reading as empty",
          _number("n/a"), "n/a")

    print("\nrows bucket by timestamp")
    b = bucket(rows("09:31:33,3833,1000,T,T,XASX",
                    "09:31:33,3834,200,T,T,XASX",
                    "09:58:58,3840,100,T,T,XASX"))
    check("one bucket per distinct second", sorted(b), ["09:31:33", "09:58:58"])
    check("a second holding two prints counts two",
          sum(b["09:31:33"].values()), 2)
    check("and the same print twice counts twice, because two prints of the "
          "same size at the same price are two prints",
          sum(bucket(rows("09:31:33,3833,1000,T,T,XASX",
                          "09:31:33,3833,1000,T,T,XASX"))["09:31:33"]
              .values()), 2)

    print("\ncomparing two bodies")
    base = rows("09:31:33,3833,1000,T,T,XASX",
                "09:31:33,3833,500,T,T,XASX",
                "09:31:33,3834,200,T,T,XASX",
                "09:58:58,3840,100,T,T,XASX")
    check("a file against itself agrees", agrees(compare_rows(base, base)),
          True)
    check("and every row is accounted for",
          compare_rows(base, base)["matched"], 4)

    reordered = rows("09:31:33,3834,200,T,T,XASX",
                     "09:31:33,3833,1000,T,T,XASX",
                     "09:31:33,3833,500,T,T,XASX",
                     "09:58:58,3840,100,T,T,XASX")
    check("ORDER INSIDE ONE SECOND IS NOT A DIFFERENCE - the two sources "
          "have no reason to sequence a second the same way",
          agrees(compare_rows(base, reordered)), True)

    rescaled = rows("09:31:33,3833.0,1000,T,T,XASX",
                    "09:31:33,3833,500.0,T,T,XASX",
                    "09:31:33,3834,200,T,T,XASX",
                    "09:58:58,3840,100,T,T,XASX")
    check("nor is the scale the source happened to write",
          agrees(compare_rows(base, rescaled)), True)

    moved = rows("09:31:33,3900,1000,T,T,XASX",
                 "09:31:33,3833,500,T,T,XASX",
                 "09:31:33,3834,200,T,T,XASX",
                 "09:58:58,3840,100,T,T,XASX")
    d = compare_rows(base, moved)
    check("A PRICE THAT MOVED IS ATTRIBUTED TO ITS COLUMN, which is the "
          "whole point of pairing the leftovers",
          d["columns"], {"Last": 1})
    check("and it shows which print",
          d["examples"]["Last"][0], ("09:31:33", "3833", "3900"))
    check("nothing else is dragged in", d["unpaired_old"], 0)
    check("the rest of the second still matched", d["matched"], 3)

    two_moved = rows("09:31:33,3900,1000,T,T,XASX",
                     "09:31:33,3901,500,T,T,XASX",
                     "09:31:33,3834,200,T,T,XASX",
                     "09:58:58,3840,100,T,T,XASX")
    d = compare_rows(base, two_moved)
    check("TWO LEFTOVERS A SIDE ARE NOT PAIRED - picking one of several "
          "possible pairings would invent a finding",
          (d["columns"], d["unpaired_old"], d["unpaired_new"]), ({}, 2, 2))

    extra = base + rows("09:58:58,3840,300,T,T,XASX")
    d = compare_rows(base, extra)
    check("a print the old file does not carry is one row only in new",
          (d["unpaired_new"], d["unpaired_old"]), (1, 0))
    check("reported as a difference", agrees(d), False)

    later = base + rows("10:05:00,3850,100,T,T,XASX")
    d = compare_rows(base, later)
    check("a whole second the old file does not have is counted as a time, "
          "and its prints as rows",
          (d["only_new_times"], d["unpaired_new"]), (1, 1))

    d = compare_rows(base + rows("10:05:00,3850,100,T,T,XASX"), base)
    check("the old file having a second the new one does not is still a "
          "content difference - the one-direction rule is about FILES",
          (d["only_old_times"], d["unpaired_old"]), (1, 1))

    print("\nthe detail line")
    check("names the column and the count",
          detail(compare_rows(base, moved)), "Last differ 1")
    check("an agreeing file has nothing to say",
          detail(compare_rows(base, base)), "")
    check("and a file with several things wrong says all of them",
          detail(compare_rows(base, later + rows("09:58:58,3840,300,T,T,XASX"))),
          "times only in new 1; rows only in new 2")

    print("\ntwo trees on disk")
    HEAD = "#Time,Last,Volume,Condition,Exchange,MicCode,AUS Eastern "\
           "Standard Time\n"
    BODY = "09:31:33,0.105,1000,T,T,XASX\n09:58:58,0.105,1000,T,T,XASX\n"

    def tree(root, files):
        for rel, text in files.items():
            p = Path(root) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    with tempfile.TemporaryDirectory() as d1, \
            tempfile.TemporaryDirectory() as d2, \
            tempfile.TemporaryDirectory() as d3:
        old, new = Path(d1), Path(d2)
        report = Path(d3) / "r.csv"

        same = {"EAU AU/raw-EAU AU-20260817.csv": HEAD + BODY,
                "SCB-R TB/raw-SCB-R TB-20260817.csv": HEAD + BODY}
        tree(old, same)
        tree(new, same)
        check("identical trees pass, and say so with a zero exit",
              run(old, new, report), 0)
        check("a clean report is a header and no rows",
              report.read_text(encoding="utf-8").splitlines(),
              [",".join(REPORT_COLUMNS)])
        check("A CODE CONTAINING THE SEPARATOR SURVIVES, because "
              "parse_filename is reused rather than rewritten here",
              ticksfile.parse_filename("raw-SCB-R TB-20260817.csv")[0],
              "SCB-R TB")

        #  the miss cache, which has no old counterpart and is not a finding
        (new / "_no_data.csv").write_text(
            "BloombergCode,Sym,Date,FirstTried\n", encoding="utf-8")
        check("the miss cache is skipped, not reported missing",
              run(old, new, report), 0)

        #  a file the new process generated and the old folder never had
        tree(new, {"ZZZ SP/raw-ZZZ SP-20260817.csv": HEAD + BODY})
        check("a file only the new tree has fails the run",
              run(old, new, report), 1)
        lines = report.read_text(encoding="utf-8").splitlines()
        check("and is the one row in the report",
              lines[1:], ["ZZZ SP,ZZZ SP,20260817,missing,,,"])

        #  put it in both, then move a price
        tree(old, {"ZZZ SP/raw-ZZZ SP-20260817.csv": HEAD + BODY})
        tree(new, {"ZZZ SP/raw-ZZZ SP-20260817.csv":
                   HEAD + "09:31:33,0.105,1000,T,T,XASX\n"
                          "09:58:58,0.110,1000,T,T,XASX\n"})
        check("a moved price fails the run", run(old, new, report), 1)
        check("named, with its column and its row counts",
              report.read_text(encoding="utf-8").splitlines()[1],
              "ZZZ SP,ZZZ SP,20260817,differs,2,2,Last differ 1")

        #  scale only
        tree(new, {"ZZZ SP/raw-ZZZ SP-20260817.csv":
                   HEAD + "09:31:33,0.1050,1000,T,T,XASX\n"
                          "09:58:58,0.105,1000.0,T,T,XASX\n"})
        check("the same prints written to a different scale pass",
              run(old, new, report), 0)

        #  the timezone label
        tree(new, {"ZZZ SP/raw-ZZZ SP-20260817.csv":
                   "#Time,Last,Volume,Condition,Exchange,MicCode,"
                   "Hong Kong Standard Time\n" + BODY})
        check("a different timezone label is a header difference, not a "
              "body one",
              run(old, new, report), 1)
        check("and says both headers",
              report.read_text(encoding="utf-8")
              .splitlines()[1].startswith("ZZZ SP,ZZZ SP,20260817,header,"),
              True)

        #  a BOM on the old side, which the Bloomberg add-in may well have
        #  written, must not turn every file in the tree into a finding
        tree(new, {"ZZZ SP/raw-ZZZ SP-20260817.csv": HEAD + BODY})
        (old / "ZZZ SP" / "raw-ZZZ SP-20260817.csv").write_text(
            HEAD + BODY, encoding="utf-8-sig")
        check("A BOM ON THE OLD FILE IS NOT A HEADER DIFFERENCE",
              run(old, new, report), 0)

    print("\nthe old process compresses older files to .csv.gz")

    def pack(root, rel, text, bom=False):
        """The old file as the old process leaves it: .csv gone, .csv.gz
        in its place, in the same folder."""
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        with gzip.open(p.with_name(p.name + ".gz"), "wt", newline="",
                       encoding="utf-8-sig" if bom else "utf-8") as fh:
            fh.write(text)

    with tempfile.TemporaryDirectory() as d1, \
            tempfile.TemporaryDirectory() as d2, \
            tempfile.TemporaryDirectory() as d3:
        old, new = Path(d1), Path(d2)
        report = Path(d3) / "r.csv"
        recent = "7203 JT/raw-7203 JT-20260903.csv"
        older = "7203 JT/raw-7203 JT-20260612.csv"
        tree(new, {recent: HEAD + BODY, older: HEAD + BODY})
        tree(old, {recent: HEAD + BODY})
        pack(old, older, HEAD + BODY)
        check("one stock folder holding a plain file and a .gz one: both "
              "are found, and both agree", run(old, new, report), 0)
        check("the .gz is decompressed in memory - nothing is extracted "
              "beside it", sorted(f.name for f in (old / "7203 JT").iterdir()),
              ["raw-7203 JT-20260612.csv.gz", "raw-7203 JT-20260903.csv"])

        pack(old, older, HEAD + "09:31:33,0.105,1000,T,T,XASX\n"
                                "09:58:58,0.110,1000,T,T,XASX\n")
        check("a price that differs inside a .gz fails the run",
              run(old, new, report), 1)
        check("and the report says the old side came from the .gz",
              report.read_text(encoding="utf-8").splitlines()[1],
              "7203 JT,7203 JT,20260612,differs,2,2,"
              "old read from .gz; Last differ 1")

        pack(old, older, HEAD + BODY, bom=True)
        check("a BOM inside the .gz is no difference either",
              run(old, new, report), 0)

        (old / "7203 JT" / "raw-7203 JT-20260612.csv.gz").write_bytes(
            b"\x1f\x8b\x08\x00 not really gzip")
        check("a corrupt .gz fails the run instead of crashing it",
              run(old, new, report), 1)
        check("reported as unreadable, naming the archive",
              report.read_text(encoding="utf-8").splitlines()[1]
              .startswith("7203 JT,7203 JT,20260612,unreadable,,,"
                          "raw-7203 JT-20260612.csv.gz: "), True)

        (old / "7203 JT" / "raw-7203 JT-20260612.csv.gz").unlink()
        check("neither .csv nor .csv.gz is still missing",
              report.read_text(encoding="utf-8").splitlines()[1:] if
              run(old, new, report) else None,
              ["7203 JT,7203 JT,20260612,missing,,,"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Compare the files a run generated against the old "
                    "process's folder.  One direction: the new tree is the "
                    "subject and the old tree is never walked.")
    p.add_argument("old_dir", nargs="?", help="the old process's output")
    p.add_argument("new_dir", nargs="?", help="this process's OUTPUT_DIR")
    p.add_argument("--report", default=DEFAULT_REPORT,
                   help=f"where the CSV goes (default {DEFAULT_REPORT})")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()

    if a.self_test:
        sys.exit(self_test())
    if not a.old_dir or not a.new_dir:
        p.error("both OLD_DIR and NEW_DIR are required")
    sys.exit(run(a.old_dir, a.new_dir, a.report))
