#!/usr/bin/env python3
"""What does equity_master actually carry, and can it identify an ST name?

THE PROBLEM THIS EXISTS FOR.  Chinese ST and *ST names are capped at +/-5%
where the main board gets +/-10%, and v2's computed tiers give them +/-10% -
twice what the exchange allows, on exactly the names most likely to hit a
limit.  Bloomberg knew which names those were.  The tiers do not.

WHY IT IS HARD.  ST is a NAME prefix, not a ticker pattern.  An ST'd company
keeps its ticker - it stays `600xxx CG` - and only its exchange name gains
the marker:

    600xxx CG   name reads  "*ST <company>"    delisting risk warning
    600xxx CG   name reads  "ST <company>"     special treatment

So SymPrefix, which matches the TICKER and is how 688 and 300 work, cannot
see them.  Something has to carry the name.

WHAT THIS ASKS, in order, and none of it is assumed:

  --columns   what columns does equity_master have?  The repo documents ten;
              the table may carry more, and a name column is what we need.
  --st        for whichever name-ish columns exist, how many rows on a
              Chinese venue look ST, and what do they look like?
  --sample    the raw row for one sym, every column, so an unfamiliar
              column can be read rather than guessed at.

NOTHING IS HARDCODED.  If equity_master has no name column, that is the
finding, and the answer is that the ST flag must come from somewhere else -
the crosscode, a separate list, or keeping China on Source=bloomberg.

    python em_probe.py --server host:5010 --columns
    python em_probe.py --server host:5010 --st
    python em_probe.py --server host:5010 --sample 600000.CH
    python em_probe.py --self-test          no kdb, no network
"""

from __future__ import annotations

import argparse
import re
import sys

#  Column names worth trying, in the order they are most likely to carry a
#  human-readable name.  The probe reports which of these EXIST rather than
#  assuming any of them do.
NAME_CANDIDATES = ("NAME", "SECURITY_NAME", "SHORT_NAME", "LONG_COMP_NAME",
                   "NAME_CHINESE", "EQY_SH_NAME", "SECURITY_DES",
                   "TICKER_AND_EXCH")

#  Composites that identify a mainland Chinese listing.  ST applies there;
#  it has no meaning in Tokyo or Kuala Lumpur.
CHINA_SUFFIXES = ("CH", "C1", "C2", "CG", "CS")

COLUMNS_Q = "{cols equity_master}"
#  meta gives the TYPE of every column, which is the decisive evidence when a
#  date comparison dies on 'type.
META_Q = "{meta equity_master}"
#  Two ways to bound the partition, for the same reason kdbclose has two: the
#  python date may not convert to whatever `date` actually is.
MAXDATE_CLIENT_Q = "{[d] exec max date from equity_master where date<=d}"
MAXDATE_SERVER_Q = ("{[n] exec max date from equity_master "
                    "where date<=.z.D-n}")
LATEST_Q = "{exec max date from equity_master}"


def _pykx():
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "--self-test runs without it.")
    return pykx


def parse_server(value: str):
    text = (value or "").strip()
    if text.count(":") != 1:
        raise SystemExit(f"--server {value!r} is not host:port")
    host, port = (p.strip() for p in text.split(":"))
    if not host or not port.isdigit():
        raise SystemExit(f"--server {value!r} is not host:port")
    return host, int(port)


#  Written without backslash escapes: this file is maintained through a shell
#  that mangles them.  [*]? is a literal asterisk, optional.
_ST = re.compile("^[ ]*[*]?[ ]*S[*]?ST[ ]|^[ ]*[*]?[ ]*ST[ ]", re.IGNORECASE)


def looks_st(name) -> bool:
    """Does this exchange name carry an ST marker?

    Covers the forms the mainland exchanges have used: 'ST X', '*ST X', and
    the older 'SST X' / 'S*ST X' from the split-share era.  The trailing
    space matters - a company legitimately called 'STAR SOMETHING' is not
    under special treatment, and matching a bare prefix would flag it."""
    text = (name or "")
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    return bool(_ST.match(str(text)))


def is_china(sym) -> bool:
    """A sym like '600000.CH' on a mainland composite."""
    text = str(sym or "")
    suffix = text.rsplit(".", 1)[-1].upper() if "." in text else ""
    return suffix in CHINA_SUFFIXES


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace").strip()
    return str(value).strip()


def columns(conn) -> list:
    got = conn(COLUMNS_Q)
    try:
        got = got.py()
    except AttributeError:
        pass
    return [_text(c) for c in (got or [])]


def report_columns(found: list) -> list:
    """Which columns exist, and which of them might carry a name."""
    lines = [f"  {len(found)} columns in equity_master", ""]
    named = [c for c in found
             if c.upper() in {n.upper() for n in NAME_CANDIDATES}]
    for c in sorted(found):
        mark = "  <- could carry the ST marker" if c in named else ""
        lines.append(f"    {c}{mark}")
    lines.append("")
    if named:
        lines.append("  name-ish columns present: " + ", ".join(named))
        lines.append("  -> run --st to see whether any of them shows ST")
    else:
        lines.append("  NO name-ish column. That is the finding: equity_master")
        lines.append("  cannot identify an ST name, so the flag has to come")
        lines.append("  from elsewhere - the crosscode, a separate list, or")
        lines.append("  China stays on Source=bloomberg.")
    return lines


def summarise_st(rows, column: str) -> list:
    """rows is (sym, name) pairs.  Counts and shows what matched."""
    china = [(s, n) for s, n in rows if is_china(s)]
    hits = [(s, n) for s, n in china if looks_st(n)]
    lines = [f"  {column}: {len(china)} Chinese rows, {len(hits)} look ST"]
    for sym, name in hits[:15]:
        lines.append(f"      {sym:<16} {name}")
    if len(hits) > 15:
        lines.append(f"      ... and {len(hits) - 15} more")
    if china and not hits:
        lines.append("      nothing matched - either this column is not the "
                     "name, or it is not the marker")
    return lines


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="", help="kdb as host:port")
    p.add_argument("--date", default="", help="YYYY.MM.DD; default today")
    p.add_argument("--columns", action="store_true")
    p.add_argument("--meta", action="store_true",
                   help="every column AND its q type - run this first when a "
                        "date comparison dies on 'type")
    p.add_argument("--st", action="store_true")
    p.add_argument("--sample", default="", help="one sym, every column")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    if not (args.columns or args.meta or args.st or args.sample):
        p.print_help()
        return 2

    host, port = parse_server(args.server)
    conn = _pykx().SyncQConnection(host=host, port=port)

    #  SCHEMA QUESTIONS ARE ANSWERED WITHOUT A DATE, deliberately.  This
    #  probe exists partly because a date comparison died on 'type, and a
    #  probe that needs the date to work before it can tell you about the
    #  date is no use at all.
    if args.meta:
        print(conn(META_Q))
        return 0
    if args.columns:
        for line in report_columns(columns(conn)):
            print(line)
        return 0

    found = columns(conn)
    date_used = None
    for how, query, arg in (("client date", MAXDATE_CLIENT_Q,
                             args.date or "2026.01.01"),
                            ("server .z.D-1", MAXDATE_SERVER_Q, 1),
                            ("latest partition", LATEST_Q, None)):
        try:
            date_used = conn(query) if arg is None else conn(query, arg)
        except Exception as e:                              # noqa: BLE001
            print(f"  {how}: {type(e).__name__}: {e}")
            continue
        print(f"equity_master partition {date_used} (via {how})")
        break
    if date_used is None:
        print("  could not resolve any partition date - run --meta")
        return 1
    print()

    if args.sample:
        got = conn("{[d;s] select from equity_master where date=d, "
                   "sym=`$s}", date_used, args.sample)
        print(got)
        return 0

    named = [c for c in found
             if c.upper() in {n.upper() for n in NAME_CANDIDATES}]
    if not named:
        print("  no name-ish column exists, so there is nothing to test.")
        print("  run --columns to see what IS there.")
        return 1
    for column in named:
        rows = conn("{[d;c] select sym, name:c#0 from equity_master "
                    "where date=d}", date_used, column)
        try:
            pairs = [(_text(r["sym"]), _text(r["name"])) for r in rows.py()]
        except Exception:                                   # noqa: BLE001
            pairs = []
        for line in summarise_st(pairs, column):
            print(line)
        print()
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("em_probe --self-test")
    print()
    print("spotting an ST marker in an exchange name")
    check("the plain form", looks_st("ST SOMECOMPANY"), True)
    check("the delisting-risk form", looks_st("*ST SOMECOMPANY"), True)
    check("the split-share era form", looks_st("S*ST SOMECOMPANY"), True)
    check("and its sibling", looks_st("SST SOMECOMPANY"), True)
    check("leading whitespace does not hide it",
          looks_st("  *ST SOMECOMPANY"), True)
    check("case does not either", looks_st("*st somecompany"), True)
    check("bytes, as kdb hands symbols back", looks_st(b"*ST X CO"), True)

    print()
    print("and NOT flagging a name that merely starts with those letters")
    check("a company called STAR is not under special treatment",
          looks_st("STAR SEMICONDUCTOR"), False)
    check("nor is STANLEY", looks_st("STANLEY WORKS"), False)
    check("nor STATE STREET", looks_st("STATE STREET CORP"), False)
    check("an ordinary name", looks_st("PING AN INSURANCE"), False)
    check("empty", looks_st(""), False)
    check("None", looks_st(None), False)

    print()
    print("keeping the question to mainland listings")
    check("a Shanghai composite", is_china("600000.CH"), True)
    check("a Shenzhen venue code", is_china("000001.CS"), True)
    check("Tokyo is not in scope - ST has no meaning there",
          is_china("7203.JP"), False)
    check("Kuala Lumpur", is_china("MAYBANK.MK"), False)
    check("a sym with no suffix at all", is_china("600000"), False)

    print()
    print("reporting what the table has")
    lines = "\n".join(report_columns(["sym", "PX_LAST", "NAME", "CRNCY"]))
    check("a name column is pointed at", "could carry the ST marker" in lines,
          True)
    check("and the next step is named", "run --st" in lines, True)
    bare = "\n".join(report_columns(["sym", "PX_LAST", "CRNCY"]))
    check("no name column is stated as the FINDING, not as an error - it "
          "means the flag must come from somewhere else",
          "NO name-ish column" in bare, True)
    check("and it says where to look instead",
          "Source=bloomberg" in bare, True)

    print()
    print("counting what matched")
    rows = [("600001.CH", "SOME CO"), ("600002.CH", "*ST OTHER CO"),
            ("600003.CH", "ST THIRD CO"), ("7203.JP", "*ST NOT CHINA")]
    text = "\n".join(summarise_st(rows, "NAME"))
    check("only the Chinese rows are counted, so Tokyo cannot inflate it",
          "3 Chinese rows, 2 look ST" in text, True)
    check("and the matches are shown, so they can be checked by hand",
          "600002.CH" in text, True)
    none = "\n".join(summarise_st([("600001.CH", "SOME CO")], "CRNCY"))
    check("a column that matches nothing says so rather than looking clean",
          "nothing matched" in none, True)

    print()
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
