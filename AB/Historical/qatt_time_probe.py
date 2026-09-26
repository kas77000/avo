#!/usr/bin/env python3
"""Which of qatt's five time columns is the exchange's own clock?

A probe, not a pipeline.  It answers one question - which column should
become the CSV's `#Time` - before the job is built on a guess.

WHY IT MATTERS MORE THAN IT SOUNDS.  The sample file is stamped in exchange
local time; its header says so in words ("AUS Eastern Standard Time").  But
liquidity_profile.q records that `qatt`time` is the PLANT's clock, running
eight hours ahead of UTC - Hong Kong - for every name in the store.  So for
an Australian name that column is two hours out, and for a Japanese one it is
an hour out.  Nothing about a wrong answer here looks wrong: the file is the
right length, the prices are right, the volumes are right, and every
timestamp is shifted.  A volume curve built on it would put the open auction
in the wrong bucket for every market except Hong Kong.

HOW IT DECIDES.  It asks for one name on one day, pulls all five columns
together, and compares each column's first and last print against the
session hours YOU give it.  The exchange's own clock is the column whose
span lands on the session; the others are offset by whole hours.  The
session is an argument rather than a table because it is a published fact
about the venue, known to whoever runs this, and a timezone database would
be a dependency bought to avoid typing six characters.

    python qatt_time_probe.py 7203.JP --session 09:00-15:00
    python qatt_time_probe.py BHP.AU  --session 10:00-16:00 --date 2026-09-02
    python qatt_time_probe.py 0700.HK --rows 20        show more prints
    python qatt_time_probe.py --self-test              no kdb at all

The qatt server comes from local_settings.py via settings.py - the same file
historical_ticks.py reads, and the only setting this needs is QATT_SERVER.
There is no --host or --port: a server retyped per invocation is a server
eventually typed wrong.

PICK A LIQUID NAME.  A name that traded twice tells you nothing about where
the session started.  0700.HK, 7203.JP and BHP.AU all work.

Exit status is 0 only if exactly one column lands on the session, because
that is the only outcome that settles anything.  When it does, set
`qattsource.TIME_FIELD` to that column and this probe has done its job.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import qattsource
import settings

#  Anything further from the session than this is a different clock, not
#  jitter.  Half an hour is wider than any real gap between an exchange's
#  published open and its first print, and narrower than the smallest
#  timezone step that separates two of these columns.
TOLERANCE_HOURS = 0.5


# =============================================================================
# PURE  - everything the verdict rests on, testable with no kdb
# =============================================================================

def parse_session(text: str):
    """'09:30-16:00' -> (time(9,30), time(16,0))."""
    parts = (text or "").split("-")
    if len(parts) != 2:
        raise ValueError(
            f"--session {text!r} is not a range; write it as 09:30-16:00")
    out = []
    for half in parts:
        bits = half.strip().split(":")
        if len(bits) not in (2, 3) or not all(b.isdigit() for b in bits):
            raise ValueError(
                f"--session {text!r} is not a range; write it as 09:30-16:00")
        nums = [int(b) for b in bits] + [0]
        try:
            out.append(dt.time(nums[0], nums[1], nums[2]))
        except ValueError:
            raise ValueError(f"--session {text!r} has an impossible time")
    return out[0], out[1]


def hours(t) -> float:
    """A time as hours since midnight."""
    return t.hour + t.minute / 60 + t.second / 3600


def offset_hours(first, session_start) -> float:
    """How far this column's first print sits from the session's open.

    Wrapped into (-12, +12]: a Tokyo name read in UTC starts at 00:00 against
    a 09:00 session, which is -9 rather than +15.  Without the wrap a column
    nine hours behind and one fifteen hours ahead would be indistinguishable,
    and one of the two is the answer."""
    if first is None or session_start is None:
        return None
    d = hours(first) - hours(session_start)
    while d <= -12:
        d += 24
    while d > 12:
        d -= 24
    return round(d, 2)


def summarise(values) -> dict:
    """One column's span, and how much of it is missing.

    `nulls` is the disqualifier that matters most: a column that is entirely
    null is not a candidate however plausible its name, and that is a thing
    you can only learn by asking."""
    real = [v for v in values if v is not None]
    return {"n": len(values), "nulls": len(values) - len(real),
            "first": min(real) if real else None,
            "last": max(real) if real else None}


def verdict(summaries: dict, session_start) -> dict:
    """Each column's offset from the session, and which ones land on it.

    Returns {field: (offset, note)} plus a "landed" list, so the caller
    prints every column - a reader who disagrees with the tolerance can see
    the numbers it was applied to."""
    out, landed = {}, []
    for field, s in summaries.items():
        if s["n"] == 0:
            out[field] = (None, "no rows at all")
            continue
        if s["first"] is None:
            out[field] = (None, "every value null - not a candidate")
            continue
        off = offset_hours(s["first"], session_start)
        if abs(off) <= TOLERANCE_HOURS:
            out[field] = (off, "lands on the session")
            landed.append(field)
        else:
            out[field] = (off, f"{off:+g}h off the session")
    return {"columns": out, "landed": landed}


def conclusion(v: dict) -> str:
    landed = v["landed"]
    if len(landed) == 1:
        return (f"  {landed[0]} is the exchange's clock. Set "
                f"qattsource.TIME_FIELD to it.")
    if not landed:
        return ("  No column lands on the session. Either --session is wrong\n"
                "  for this name, or the day is a holiday and what came back\n"
                "  is not a normal session. Check both before concluding the\n"
                "  store has no exchange clock.")
    return (f"  {len(landed)} columns land on the session: "
            f"{', '.join(landed)}.\n"
            f"  They agree to within {TOLERANCE_HOURS}h, so this name cannot "
            f"separate them.\n"
            f"  Re-run on a name in a market whose offset from Hong Kong is "
            f"not zero.")


# =============================================================================
# THE READ
# =============================================================================

def probe(conn, sym: str, date, session, rows_to_show: int) -> int:
    print(f"\n--- qatt : {sym} on {date} ---")
    raw = qattsource._rows(conn(qattsource.probe_q(), date, sym))
    if not raw:
        print("  no prints came back.\n"
              "  Either the sym is wrong - it is the qatt key (7203.JP), not\n"
              "  the Bloomberg code (7203 JT) - or that date is not a\n"
              "  partition, or the name did not trade.")
        return 1

    columns = {f: [qattsource.to_time(r.get(f)) for r in raw]
               for f in qattsource.TIME_FIELDS}
    summaries = {f: summarise(v) for f, v in columns.items()}

    print(f"  {len(raw)} prints\n")
    head = "  " + "  ".join(f"{f:>13}" for f in qattsource.TIME_FIELDS)
    print(head + "     price      size  cond  ex")
    for r in raw[:rows_to_show]:
        line = "  " + "  ".join(
            f"{qattsource.clock(r.get(f)) or '-':>13}"
            for f in qattsource.TIME_FIELDS)
        print(f"{line}  {str(qattsource.to_decimal(r.get('price'))):>9}"
              f"  {str(qattsource.to_decimal(r.get('size'))):>8}"
              f"  {qattsource.text(r.get('cond')):<4}"
              f"  {qattsource.text(r.get('ex'))}")
    if len(raw) > rows_to_show:
        print(f"  ... {len(raw) - rows_to_show} more")

    print(f"\n  session given: {session[0]:%H:%M} - {session[1]:%H:%M} "
          f"exchange local\n")
    v = verdict(summaries, session[0])
    for field in qattsource.TIME_FIELDS:
        s, (off, note) = summaries[field], v["columns"][field]
        span = (f"{s['first']:%H:%M:%S} .. {s['last']:%H:%M:%S}"
                if s["first"] else "-")
        nulls = f"{s['nulls']} null" if s["nulls"] else ""
        print(f"  {field:<14} {span:<22} {nulls:<10} {note}")

    print()
    print(conclusion(v))
    return 0 if len(v["landed"]) == 1 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Which qatt time column is the exchange's own clock?")
    p.add_argument("sym", nargs="?", default="0700.HK",
                   help="a qatt sym - 7203.JP, not 7203 JT (default 0700.HK)")
    p.add_argument("--date", default="",
                   help="the session to read, YYYY-MM-DD; default is the "
                        "most recent partition qatt holds")
    p.add_argument("--session", default="09:30-16:00",
                   help="the exchange's published hours, local "
                        "(default 09:30-16:00, Hong Kong)")
    p.add_argument("--rows", type=int, default=10,
                   help="how many prints to print")
    p.add_argument("--self-test", action="store_true",
                   help="check the reasoning, with no kdb at all")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    try:
        session = parse_session(a.session)
    except ValueError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    try:
        cfg = settings.load()
        host, port = settings.server(cfg, "QATT_SERVER")
    except settings.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    print(f"qatt server  {host}:{port}   from local_settings.py")
    conn = qattsource.connect(host, port)
    dates = qattsource.partitions(conn)
    if not dates:
        print("FAIL  qatt holds no partitions at all.", file=sys.stderr)
        return 1

    if a.date:
        try:
            date = dt.date.fromisoformat(a.date)
        except ValueError:
            print(f"FAIL  --date {a.date!r} is not a date; write it as "
                  f"YYYY-MM-DD", file=sys.stderr)
            return 2
        if date not in dates:
            print(f"FAIL  qatt has no partition for {date}.  "
                  f"It holds {dates[0]} .. {dates[-1]}.", file=sys.stderr)
            return 1
    else:
        date = dates[-1]
        print(f"no --date, so the most recent partition: {date}")

    return probe(conn, a.sym, date, session, a.rows)


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

    T = dt.time

    print("qatt_time_probe --self-test\n\nreading a session")
    check("hours and minutes", parse_session("09:30-16:00"), (T(9, 30), T(16)))
    check("seconds are allowed", parse_session("09:30:15-16:00:00"),
          (T(9, 30, 15), T(16)))
    check("spaces around the halves are not part of them",
          parse_session(" 09:30 - 16:00 "), (T(9, 30), T(16)))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except ValueError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                 f"{fragment!r}"))

    raises("one time is not a range", lambda: parse_session("09:30"),
           "09:30-16:00")
    raises("nor is nothing", lambda: parse_session(""), "09:30-16:00")
    raises("words are not times", lambda: parse_session("open-close"),
           "09:30-16:00")
    raises("nor is an impossible clock", lambda: parse_session("25:00-26:00"),
           "impossible")

    print("\nhow far a column sits from the open")
    check("a column on the session", offset_hours(T(9, 30), T(9, 30)), 0.0)
    check("a few minutes late is a few minutes, not an hour",
          offset_hours(T(9, 32), T(9, 30)), 0.03)
    check("the plant's Hong Kong clock against an Australian session",
          offset_hours(T(8, 0), T(10, 0)), -2.0)
    check("and against a Tokyo one", offset_hours(T(8, 0), T(9, 0)), -1.0)
    check("UTC against Tokyo wraps to -9, not +15 - without the wrap the "
          "answer is unreadable",
          offset_hours(T(0, 0), T(9, 0)), -9.0)
    check("UTC against a Hong Kong session", offset_hours(T(1, 30), T(9, 30)),
          -8.0)
    check("no first print, no offset", offset_hours(None, T(9, 30)), None)

    print("\nsummarising a column")
    vals = [T(9, 31), T(10, 0), T(9, 30), None]
    check("the span is the earliest and the latest, not the first and last",
          (summarise(vals)["first"], summarise(vals)["last"]),
          (T(9, 30), T(10, 0)))
    check("nulls are counted, not dropped silently",
          summarise(vals)["nulls"], 1)
    check("a column that is entirely null has no span",
          summarise([None, None]),
          {"n": 2, "nulls": 2, "first": None, "last": None})
    check("no rows at all", summarise([]),
          {"n": 0, "nulls": 0, "first": None, "last": None})

    print("\nthe verdict")
    #  An Australian name: the exchange's clock reads 10:00, the plant's
    #  Hong Kong clock reads 08:00, and UTC reads 00:00.
    s = {"time": summarise([T(8, 0), T(16, 0)]),
         "tradeTime": summarise([T(10, 0), T(16, 0)]),
         "srcTime": summarise([T(0, 0), T(6, 0)]),
         "lineTime": summarise([None, None]),
         "activityTime": summarise([])}
    v = verdict(s, T(10, 0))
    check("exactly one column lands on the session",
          v["landed"], ["tradeTime"])
    check("the plant's clock is reported as two hours off, with the number",
          v["columns"]["time"], (-2.0, "-2h off the session"))
    check("UTC is ten hours off", v["columns"]["srcTime"][0], -10.0)
    check("an all-null column is disqualified by name, not by offset",
          v["columns"]["lineTime"], (None, "every value null - not a "
                                           "candidate"))
    check("and so is one with no rows",
          v["columns"]["activityTime"], (None, "no rows at all"))
    check("one landing column settles it",
          "Set qattsource.TIME_FIELD" in conclusion(v), True)

    print("\nthe two outcomes that settle nothing")
    none_landed = verdict({"time": summarise([T(8, 0)])}, T(15, 0))
    check("nothing lands: say so, and name the two innocent explanations "
          "before blaming the store",
          "holiday" in conclusion(none_landed), True)
    #  A Hong Kong name cannot tell the plant's clock from the exchange's,
    #  because for Hong Kong they are the same clock.
    two_landed = verdict({"time": summarise([T(9, 30)]),
                          "tradeTime": summarise([T(9, 30)])}, T(9, 30))
    check("two land: the name cannot separate them, and the fix is a "
          "different market rather than a tighter tolerance",
          "not zero" in conclusion(two_landed), True)
    check("and both are named", two_landed["landed"], ["time", "tradeTime"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
