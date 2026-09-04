#!/usr/bin/env python3
"""Ask B-PIPE for any fields you name, on any securities you name.

The other probes each answer one fixed question.  This one answers whatever
you type, which is what you want when you are hunting for a field rather
than checking a known one:

    python bpipe_get.py "600000 CH Equity" -f NAME,MIN_LIMIT,MAX_LIMIT
    python bpipe_get.py "7203 JT Equity" "005930 KP Equity" -f LAST_PRICE
    python bpipe_get.py --securities-file syms.txt -f SECURITY_NAME
    python bpipe_get.py --self-test          no Bloomberg, no network

THREE OUTCOMES, KEPT APART, because they mean different things and a probe
that blurs them tells you nothing:

    a value            the field answered
    field exception    the FIELD is unknown or not permitted to us - it will
                       fail for every security, so it is reported once with
                       a count, not once per name
    security error     the SECURITY is unknown or not entitled - that is
                       about the name, not the field

WHY THIS EXISTS RIGHT NOW.  Chinese ST names are capped at +/-5% where the
main board gets +/-10%, and nothing in the config knows which they are.  ST
is a NAME prefix - the ticker stays 600xxx CG and only the name gains the
marker - so the question is whether B-PIPE will hand us a name field at all:

    python bpipe_get.py "600000 CH Equity" -f NAME,SECURITY_NAME,SHORT_NAME,LONG_COMP_NAME

Whichever answers is the one to add to the limit job.  Use bpipe_fields.py
--search name first if none of those work; it lists what the real-time
family actually contains.

Connection settings come from bpipe_probe.py, so there is one place to fill
them in.  They ship empty and this refuses to start rather than connect
somewhere you did not mean.
"""

from __future__ import annotations

import argparse
import csv as csvmod
import sys
from pathlib import Path

import bpipe_probe
from bpipe_probe import SettingError, resolve_connection

CHUNK = 100


def parse_fields(values) -> list:
    """--field MIN_LIMIT --field NAME and -f MIN_LIMIT,NAME both work.

    Upper-cased and de-duplicated, order kept: a field asked for twice is a
    typo, not a request for two columns."""
    out = []
    for value in values or []:
        for part in str(value).replace(";", ",").split(","):
            name = part.strip().upper()
            if name and name not in out:
                out.append(name)
    return out


def parse_securities(positional, path) -> list:
    """Names on the command line, or one per line in a file.

    Blank lines and # comments are skipped so a file can be annotated."""
    out = list(positional or [])
    if path:
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    seen, unique = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def render(securities, fields, values, refused, field_problems) -> list:
    """The table, as lines.  Pure, so the self test can read it."""
    lines = []
    if field_problems:
        lines.append("FIELDS THAT DID NOT ANSWER")
        for field, (message, count) in sorted(field_problems.items()):
            lines.append(f"  {field:<24} {message}  ({count} names)")
        lines.append("")
    if refused:
        lines.append("SECURITIES B-PIPE REFUSED")
        for security, message in sorted(refused.items()):
            lines.append(f"  {security:<24} {message}")
        lines.append("")

    answered = [f for f in fields
                if any(f in values.get(s, {}) for s in securities)]
    if not answered:
        lines.append("No field answered for any security.")
        return lines

    width = max([len(s) for s in securities] + [8])
    lines.append("  " + "security".ljust(width)
                 + "  " + "  ".join(f.ljust(18) for f in answered))
    for s in securities:
        got = values.get(s)
        if got is None:
            continue
        cells = []
        for f in answered:
            v = got.get(f)
            cells.append(("" if v is None else str(v)).ljust(18))
        lines.append("  " + s.ljust(width) + "  " + "  ".join(cells).rstrip())
    return lines


def fetch(session, identity, securities, fields):
    """(values, refused, field_problems).  Mirrors v2/bpipe.fetch, kept
    separate so the probes never import the job."""
    blpapi = bpipe_probe._blpapi()
    if not session.openService("//blp/refdata"):
        raise SystemExit("FAIL  could not open //blp/refdata")
    service = session.getService("//blp/refdata")

    values, refused, field_problems = {}, {}, {}
    for batch in chunks(securities, CHUNK):
        request = service.createRequest("ReferenceDataRequest")
        for s in batch:
            request.getElement("securities").appendValue(s)
        for f in fields:
            request.getElement("fields").appendValue(f)
        session.sendRequest(request, identity)

        done = False
        while not done:
            event = session.nextEvent(30_000)
            for msg in event:
                if not msg.hasElement("securityData"):
                    continue
                data = msg.getElement("securityData")
                for i in range(data.numValues()):
                    entry = data.getValueAsElement(i)
                    security = entry.getElementAsString("security")
                    if entry.hasElement("securityError"):
                        error = entry.getElement("securityError")
                        refused[security] = (
                            error.getElementAsString("message")
                            if error.hasElement("message") else str(error))
                        continue
                    if entry.hasElement("fieldExceptions"):
                        fx = entry.getElement("fieldExceptions")
                        for j in range(fx.numValues()):
                            item = fx.getValueAsElement(j)
                            field = item.getElementAsString("fieldId")
                            info = item.getElement("errorInfo")
                            message = (info.getElementAsString("message")
                                       if info.hasElement("message")
                                       else str(info))
                            old = field_problems.get(field, (message, 0))
                            field_problems[field] = (old[0], old[1] + 1)
                    values[security] = bpipe_probe.element_to_dict(
                        entry.getElement("fieldData"))
            if event.eventType() == blpapi.Event.RESPONSE:
                done = True
    return values, refused, field_problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("securities", nargs="*",
                   help='e.g. "600000 CH Equity"')
    p.add_argument("-f", "--field", action="append", default=None,
                   help="field to ask for; repeatable, or comma separated")
    p.add_argument("--securities-file", default="",
                   help="one security per line; # comments allowed")
    p.add_argument("--csv", default="", help="also write the table here")
    p.add_argument("--host", default=bpipe_probe.HOST)
    p.add_argument("--port", default=bpipe_probe.PORT)
    p.add_argument("--app", default=bpipe_probe.APP_NAME)
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()

    fields = parse_fields(args.field)
    if not fields:
        print("FAIL  name at least one field with -f", file=sys.stderr)
        return 2
    try:
        securities = parse_securities(args.securities, args.securities_file)
    except OSError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2
    if not securities:
        print("FAIL  name at least one security", file=sys.stderr)
        return 2

    try:
        host, port, app = resolve_connection(args.host, args.port, args.app,
                                             args.no_auth)
    except SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    session, identity = bpipe_probe.start_session(host, port, app)
    try:
        print(f"{len(securities)} securities x {len(fields)} fields")
        values, refused, field_problems = fetch(session, identity,
                                                securities, fields)
    finally:
        session.stop()

    print("")
    for line in render(securities, fields, values, refused, field_problems):
        print(line)

    if args.csv:
        target = Path(args.csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csvmod.DictWriter(fh, fieldnames=["security"] + fields)
            writer.writeheader()
            for s in securities:
                row = {"security": s}
                row.update({f: values.get(s, {}).get(f, "") for f in fields})
                writer.writerow(row)
        print(f"\nwritten to {target}")
    return 0 if values else 1


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

    print("bpipe_get --self-test")
    print("")
    print("reading the fields off the command line")
    check("repeated -f", parse_fields(["MIN_LIMIT", "MAX_LIMIT"]),
          ["MIN_LIMIT", "MAX_LIMIT"])
    check("one -f, comma separated", parse_fields(["MIN_LIMIT,MAX_LIMIT"]),
          ["MIN_LIMIT", "MAX_LIMIT"])
    check("mixed, with spaces", parse_fields(["min_limit, name"]),
          ["MIN_LIMIT", "NAME"])
    check("a field asked for twice is a typo, not two columns",
          parse_fields(["NAME", "name"]), ["NAME"])
    check("nothing", parse_fields(None), [])

    print("")
    print("reading the securities")
    check("from the command line",
          parse_securities(["7203 JT Equity"], ""), ["7203 JT Equity"])
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "syms.txt"
        f.write_text("# a comment\n7203 JT Equity\n\n600000 CH Equity\n",
                     encoding="utf-8")
        check("from a file, comments and blanks skipped",
              parse_securities([], str(f)),
              ["7203 JT Equity", "600000 CH Equity"])
        check("both together, de-duplicated",
              parse_securities(["7203 JT Equity"], str(f)),
              ["7203 JT Equity", "600000 CH Equity"])

    print("")
    print("the three outcomes stay apart")
    securities = ["600000 CH Equity", "GONE CH Equity"]
    fields = ["NAME", "MIN_LIMIT", "NOSUCH"]
    values = {"600000 CH Equity": {"NAME": "*ST SOMECO",
                                   "MIN_LIMIT": 9.5}}
    refused = {"GONE CH Equity": "Unknown/Invalid Security"}
    problems = {"NOSUCH": ("Field not valid", 2)}
    text = "\n".join(render(securities, fields, values, refused, problems))
    check("a bad FIELD is reported once with a count, not once per name",
          "NOSUCH" in text and "(2 names)" in text, True)
    check("a bad SECURITY is reported under its own heading",
          "SECURITIES B-PIPE REFUSED" in text, True)
    check("a field nothing answered is left out of the table entirely, so "
          "the columns are the ones that worked",
          "NOSUCH" in text.split("security")[-1], False)
    check("and the value that did answer is shown",
          "*ST SOMECO" in text, True)

    nothing = "\n".join(render(["A"], ["X"], {}, {}, {}))
    check("nothing answered at all says so plainly",
          "No field answered" in nothing, True)

    print("")
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
