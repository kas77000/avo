#!/usr/bin/env python3
"""qatt and equity_master out of kdb.  This is the ONLY module that touches
kdb, and the only things it fetches are trade prints and the cross-references
needed to name them.

WHY KDB AND NOT B-PIPE.  This job replaces a HistoricalTickDataRequest.  Our
B-PIPE entitlement is real-time only - `LimitUpDown/other/bpipe_history.py`
is the probe that established it - so the prints have to come from the plant's
own store, which is qatt.

WHAT qatt IS.  One row per print, carrying the quote that stood at the time;
there are no quote-only rows, so `price>0, size>0` is the whole test for "this
row is a trade".  That is liquidity_profile.q's finding and this file relies
on it rather than re-deriving it.

THREE UNCERTAINTIES, ALL REPORTED RATHER THAN ASSUMED.

  TIME_FIELD  qatt has five time columns and the sample CSV is stamped in
              EXCHANGE local time.  qatt`time is the PLANT's clock - HKT -
              so it is two hours out for Australia and one for Tokyo.  Which
              column carries the exchange's own stamp is not settled here:
              run qatt_time_probe.py, read the answer, then set TIME_FIELD.
              It ships as tradeTime because the name says so, NOT because it
              has been checked.

  the sym     equity_master is asked, never guessed.  A crosscode row's
              `7203 JT` is matched on sym_bpipe (`7203.JT`), then on
              sym_mbpipe (`7203 JT EQUITY`) for whatever the first pass
              missed, and the authoritative `sym` (`7203.JP`) is read back.
              The run tallies which pass answered.

  the date    .z.D-1 lands on a Sunday every Monday and on every holiday, so
              a requested date is rolled back to the most recent partition
              that actually exists, and both are reported.

ONE ROUND TRIP PER DATE PER CHUNK, never one per symbol.  A universe is tens
of thousands of names; a per-symbol query would still be running at the open.

HDB FOR EVERY FINISHED DAY, RDB FOR TODAY.  A partition appears when the day
is done, so today is not in the HDB and no dated query will ever find it.
The two are DIFFERENT SERVERS - QATT_SERVER and QATT_RDB_SERVER - and the
RDB has no date column, so the pair is ticks_q (dated) and live_ticks_q
(not).  Today is asked for only when --today says so; see historical_ticks.py
for why a partial session is not written by accident.

pykx is imported inside connect(), so every other module - and --self-test -
runs on a machine with no kdb and no q licence.

    python qattsource.py --self-test
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

#  SET THIS FROM THE PROBE.  See the module docstring: it is a placeholder
#  with a plausible name, not a checked answer.
TIME_FIELD = "tradeTime"

#  Every time column qatt carries, for the probe to lay side by side.
TIME_FIELDS = ("time", "tradeTime", "srcTime", "lineTime", "activityTime")

#  What the output CSV needs, beyond the time.
TICK_FIELDS = ("price", "size", "cond", "ex")

#  What equity_master owes us: the qatt key, the primary code that names the
#  file, the composite, and the MIC the filter runs on.
MASTER_FIELDS = ("sym", "EQY_PRIM_EXCH_SHRT", "COMPOSITE_EXCH_CODE",
                 "ID_MIC_PRIM_EXCH")

#  AN EXPRESSION, NOT A LAMBDA, and it is the only query here sent with no
#  arguments.  conn("{...}") EVALUATES the string, and a lambda literal
#  evaluates to the unapplied function - q never runs it, and there is
#  nothing to apply it to.  The dates that came back were the characters of
#  the function's own text, every one of which _as_date discards, so
#  partitions() returned [] and the run said "qatt holds no partitions at
#  all" with no error anywhere.  Every other query here takes an argument,
#  which is what made pykx apply them and why this was the only one wrong.
PARTITIONS_Q = "exec distinct date from qatt"

#  TWO WAYS TO ASK FOR THE PARTITION, because the type of equity_master's
#  `date` column is NOT established and a LimitUpDown run on 2026-09-04 died
#  on 'type asking the first way.  See ../LimitUpDown/v2/kdbclose.py.
#
#    client   the bound is a python date, converted by pykx on the way out
#    server   the bound is computed IN q from .z.D, so no date crosses the
#             wire at all - only an integer, which cannot be mis-converted
MAXDATE_CLIENT_Q = "{[d] exec max date from equity_master where date<=d}"
MAXDATE_SERVER_Q = ("{[n] exec max date from equity_master "
                    "where date<=.z.D-n}")

#  NO `$ CAST ON THE SYM LIST.  This module used to carry one, with a comment
#  claiming it made a symbol vector out of a list of strings.  It does not:
#  pykx already sends symbols, and `$ applied to a symbol is a type error.
#  LimitUpDown paid for that live on 2026-09-04 against this same server -
#  see the ledger in ../LimitUpDown/v2/kdbclose.py - and TradingData's
#  ../TradingData/equitymaster.py carries the same plain form.  The syms go
#  in as they arrive.
MASTER_BPIPE_Q = (
    "{[d;s] select sym_bpipe," + ",".join(MASTER_FIELDS) +
    " from equity_master where date=d, sym_bpipe in s}")

MASTER_MBPIPE_Q = (
    "{[d;s] select sym_mbpipe," + ",".join(MASTER_FIELDS) +
    " from equity_master where date=d, sym_mbpipe in s}")

#  The third pass, and the one China needs.  A Shanghai line is `600000 C1`
#  in the crosscode, so sym_bpipe would be `600000.C1` - but equity_master
#  and qatt both key it on the COMPOSITE, `600000.CH`.  Matching the `sym`
#  column directly with a ticker-plus-composite candidate finds it where the
#  first two passes cannot.
MASTER_SYM_Q = (
    "{[d;s] select " + ",".join(MASTER_FIELDS) +
    " from equity_master where date=d, sym in s}")


def ticks_q(time_field: str = None) -> str:
    """The tick query: a plain select on the two predicates that matter.

    NO COLUMN LIST AND NO price>0/size>0.  Both named columns this module
    has never confirmed - TIME_FIELD is openly a placeholder and so are
    TICK_FIELDS - and a select that names a column qatt does not have is a
    hard q error, which is the worst way to discover a schema.  Taking every
    column means the answer itself says what qatt holds; the row shaping
    below picks by name and reports what is missing instead of dying on it.

    time_field is kept in the signature because the probe passes one, but
    the query no longer varies with it: every column comes back regardless.
    The date and the sym are the whole question."""
    return "{[d;s] select from qatt where date=d, sym in s}"


def live_ticks_q(time_field: str = None) -> str:
    """The same prints, from the RDB, for today.

    TODAY IS NOT IN THE HDB.  qatt is partitioned by date and a partition
    appears when the day is done, so the only way to see today is the RDB -
    a DIFFERENT SERVER, QATT_RDB_SERVER, not a different query against the
    same one.

    It carries no date column at all, which is why this is a separate query
    rather than ticks_q with the predicate dropped: there is nothing to
    constrain and nothing to pass.  The RDB holds today and only today, so
    the table itself is the filter.  LimitUpDown/v1/kdbsource.py asks qatt
    the same undated way for the same reason."""
    return "{[s] select from qatt where sym in s}"


def probe_q() -> str:
    """Every time column at once, for one name on one day."""
    return ("{[d;s] select " + ",".join(TIME_FIELDS) + "," +
            ",".join(TICK_FIELDS) +
            " from qatt where date=d, sym=s, price>0, size>0}")


#  NARROWING AN EMPTY ANSWER.  ticks_q has three predicates and a silent
#  empty result says nothing about which one emptied it.  These ask the same
#  question with one predicate removed at a time, and `select[n]` caps the
#  read so a diagnostic on a tick table stays cheap.
ANY_ON_DATE_Q = "{[d] count select[1] sym from qatt where date=d}"
SAMPLE_SYMS_Q = "{[d] select[5] sym from qatt where date=d}"
#  One whole row, for its COLUMN NAMES.  TIME_FIELD and TICK_FIELDS are
#  placeholders this module has never confirmed, and now that the query names
#  no column a wrong guess is a silent None rather than a q error.  This is
#  what turns the guess into a fact.
SAMPLE_ROW_Q = "{[d] select[1] from qatt where date=d}"
ANY_SYM_Q = "{[d;s] count select[1] sym from qatt where date=d, sym in s}"


def q_type_of(value) -> str:
    """What pykx turns this into, and what q calls it.

    THIS IS THE WHOLE QUESTION behind a silently empty answer.  `where
    date=d` against a partition column compares two types, and if a python
    date arrives as a timestamp the comparison is simply false for every
    row - no error, no rows.  Printing it beats reasoning about it.  Ported
    from ../TradingData/equitymaster.py, which was written for the 'type
    version of the same question."""
    try:
        import pykx
    except ImportError:
        return f"{type(value).__name__} (pykx not installed, cannot convert)"
    try:
        converted = pykx.toq(value)
    except Exception as e:                                  # noqa: BLE001
        return (f"{type(value).__name__} -> will not convert: "
                f"{type(e).__name__}: {e}")
    kind = getattr(converted, "t", "?")
    inside = ""
    #  A bare "List (q type 0)" hid the sym bug in the sibling: the
    #  container was right and its CONTENTS were the wrong type.
    if kind == 0:
        first = None
        for get in (lambda c: c[0], lambda c: list(c)[0],
                    lambda c: c.py()[0]):
            try:
                first = get(converted)
                break
            except Exception:                               # noqa: BLE001
                continue
        inside = (" of ?" if first is None else
                  f" of {type(first).__name__} "
                  f"(q type {getattr(first, 't', '?')})")
    return (f"{type(value).__name__} -> {type(converted).__name__} "
            f"(q type {kind}){inside}")


def sample_columns(conn, date) -> list:
    """Every column name qatt actually has on that date, in order."""
    rows = _rows(conn(SAMPLE_ROW_Q, date))
    return list(rows[0].keys()) if rows else []


def diagnose(conn, date, syms) -> list:
    """Why the answer was empty, as (label, finding) pairs.

    ONE PREDICATE AT A TIME.  ticks_q asks for date AND sym AND price>0 AND
    size>0; an empty result could be any of them.  Removing them one at a
    time says which, and a sample of the syms qatt actually holds on that
    date says what shape it wants when the answer is the sym.

    Every query here is capped with select[n], so this is safe to run
    against a tick table."""
    out = [("date sent as", q_type_of(date)),
           ("syms sent as", q_type_of(list(syms)))]

    def ask(label, query, *args):
        try:
            return _py(conn(query, *args))
        except Exception as e:                              # noqa: BLE001
            out.append((label, f"the query RAISED: {e}"))
            return None

    on_date = ask("rows on that date", ANY_ON_DATE_Q, date)
    if on_date is None:
        return out
    if not on_date:
        out.append(("rows on that date", "NONE - qatt has nothing at all "
                    "for this date, so the date predicate is what is "
                    "empty, not the syms"))
        return out
    out.append(("rows on that date", "yes, so the date matches"))

    try:
        sample = [text(r.get("sym")) for r in _rows(conn(SAMPLE_SYMS_Q, date))]
    except Exception as e:                                  # noqa: BLE001
        sample = []
        out.append(("syms qatt holds there", f"the query RAISED: {e}"))
    if sample:
        out.append(("syms qatt holds there", ", ".join(sample[:5])))

    matched = ask("those syms on that date", ANY_SYM_Q, date, list(syms))
    if matched is not None:
        out.append(("those syms on that date",
                    "yes - the sym matches, so the emptiness is downstream "
                    "of the query" if matched else
                    "NONE - the sym predicate is what is empty.  Compare "
                    "the shape asked for with the sample above"))
    return out


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


# =============================================================================
# NORMALISING WHAT kdb HANDS BACK  - pure, and the part worth testing
# =============================================================================

def _py(value):
    """pykx atoms carry a .py(); numpy scalars and plain python do not."""
    try:
        return value.py()
    except AttributeError:
        return value


def text(value) -> str:
    """A symbol, a char vector or a python string -> a string.

    kdb's null symbol is the empty one, so a null and a blank are the same
    thing here and both come back as ""."""
    value = _py(value)
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode().strip()
        except UnicodeDecodeError:
            return ""
    return str(value).strip()


def to_decimal(value):
    """Anything that will not become a finite number is not a price.

    Unlike LimitUpDown's reader this one does NOT require a positive: the
    caller has already filtered on price>0 in q, and a size or a price that
    arrives as 0 here means the filter did not run, which is worth seeing
    rather than silently dropping."""
    value = _py(value)
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    return d if d.is_finite() else None


def to_time(value):
    """kdb's `t` -> datetime.time.

    pykx hands this back as a datetime.time on some builds, a timedelta on
    others, and a bare count of milliseconds on a raw connection.  All three
    mean the same thing - milliseconds since midnight - so all three are
    accepted and anything else is None."""
    value = _py(value)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.time()
    if isinstance(value, dt.timedelta):
        ms = int(value.total_seconds() * 1000)
    else:
        try:
            ms = int(value)
        except (TypeError, ValueError):
            return None
    #  kdb's null time is 0Ni, which arrives as a very large negative.
    if ms < 0 or ms >= 86_400_000:
        return None
    s, ms = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return dt.time(h, m, s, ms * 1000)


def clock(value) -> str:
    """A time as the CSV writes it: zero padded, to the second.

    Excel renders 09:31:33 and 9:31:33 identically, and the padded form is
    the one that sorts as text, so it is the one written."""
    t = to_time(value)
    return t.strftime("%H:%M:%S") if t else ""


# =============================================================================
# THE READS
# =============================================================================

def partitions(conn) -> list:
    """Every date qatt actually holds, oldest first.

    This is what caps a backfill.  A name listed three weeks ago cannot have
    sixty days behind it, and the difference between "no history" and "no
    partition" is the difference between a coverage problem and a request
    for a day that was never stored."""
    got = _py(conn(PARTITIONS_Q))
    if got is None:
        return []
    return sorted({d for d in (_as_date(x) for x in got) if d is not None})


def _as_date(value):
    value = _py(value)
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def resolve_master_date(conn, requested, days_back=1, log=None):
    """The most recent equity_master date on or before the one asked for.

    BOTH WAYS OF ASKING ARE TRIED.  Sending the bound as a python date is
    the obvious way and it is the way that died on 'type in LimitUpDown on
    2026-09-04; computing the bound in q from .z.D sends an integer, which
    cannot be mis-converted.  If both fail, `date` is not a date column and
    no client-side change will help - which is what the error says."""
    say = log or (lambda line: None)
    errors = []
    attempts = ((f"client date {requested}", MAXDATE_CLIENT_Q, requested),
                (f"server .z.D-{days_back}", MAXDATE_SERVER_Q,
                 int(days_back)))
    for how, query, arg in attempts:
        try:
            got = _as_date(conn(query, arg))
        except Exception as e:
            errors.append(f"{how}: {str(e).splitlines()[0]}")
            continue
        if got is None:
            errors.append(f"{how}: no partition on or before the bound")
            say(f"      {how}: null - no partition on or before the bound")
            continue
        say(f"      {how}: got {got}")
        return got

    NL = chr(10)
    raise SystemExit(NL.join(
        [f"equity_master has no usable date on or before {requested}."]
        + ["    " + e for e in errors]
        + ["  BOTH ways of asking failed, so this is most likely the SCHEMA",
           "  rather than the client: `date` may not be a q date column at",
           "  all.  Run",
           "      python ../LimitUpDown/other/em_probe.py "
           "--server HOST:PORT --meta",
           "  and check what type `date` actually is."]))


def _rows(result):
    """A pykx table -> a list of dicts, whatever the build hands back."""
    if result is None:
        return []
    try:
        return result.pd().to_dict("records")
    except AttributeError:
        pass
    try:
        return list(result.py())
    except AttributeError:
        return list(result)


def fetch_master(conn, date, candidates: dict) -> dict:
    """Resolve crosscode codes to qatt syms, in three passes.

    `candidates` is {bloomberg code: (dotted, full, composite)}:

        dotted      `600000.C1`      matched on sym_bpipe
        full        `600000 C1 EQUITY`   matched on sym_mbpipe
        composite   `600000.CH`      matched on sym itself

    Returns {bloomberg code: row} plus a tally, and the caller reports what
    is still missing.

    THREE PASSES AND NOT ONE JOIN.  Each runs only on what the last could not
    answer, so the counts say which column actually carries our universe
    rather than burying it in a coalesce.  The third exists for China: a
    Shanghai line's exchange code is C1 and its composite is CH, and both
    equity_master and qatt key it on the composite - so `600000.C1` matches
    nothing while `600000.CH` matches."""
    if not candidates:
        return {}

    out = {}
    hits = {"sym_bpipe": 0, "sym_mbpipe": 0, "sym": 0}

    def pass_(query, column, index):
        wanted = {}
        for bbg, cand in candidates.items():
            if bbg in out:
                continue
            key = cand[index] if len(cand) > index else ""
            if key:
                wanted.setdefault(key, bbg)
        if not wanted:
            return
        for row in _rows(conn(query, date, list(wanted))):
            #  MASTER_SYM_Q selects no separate key column - it matched on
            #  `sym`, which is already in MASTER_FIELDS.
            bbg = wanted.get(text(row.get(column)))
            if bbg and bbg not in out:
                out[bbg] = _master_row(row)
                hits[column] += 1

    pass_(MASTER_BPIPE_Q, "sym_bpipe", 0)
    pass_(MASTER_MBPIPE_Q, "sym_mbpipe", 1)
    pass_(MASTER_SYM_Q, "sym", 2)

    return {"rows": out, "hits": hits}


def _master_row(row) -> dict:
    return {f: text(row.get(f)) for f in MASTER_FIELDS}


def fetch_ticks(conn, date, syms, time_field: str = None) -> dict:
    """Every print for these syms on this date, grouped by sym.

    One round trip for the whole chunk.  The caller decides the chunk size:
    a day of the entire universe is millions of rows and does not want to
    arrive in one object."""
    if not syms:
        return {}
    out = {}
    field = time_field or TIME_FIELD
    for row in _rows(conn(ticks_q(field), date, list(syms))):
        out.setdefault(text(row.get("sym")), []).append({
            "time": clock(row.get(field)),
            "price": to_decimal(row.get("price")),
            "size": to_decimal(row.get("size")),
            "cond": text(row.get("cond")),
            "ex": text(row.get("ex"))})
    return out


def fetch_live_ticks(conn, syms, time_field: str = None) -> dict:
    """Today's prints for these syms, from the RDB.  No date, either sent or
    returned - the table is today."""
    if not syms:
        return {}
    out = {}
    field = time_field or TIME_FIELD
    for row in _rows(conn(live_ticks_q(field), list(syms))):
        out.setdefault(text(row.get("sym")), []).append({
            "time": clock(row.get(field)),
            "price": to_decimal(row.get("price")),
            "size": to_decimal(row.get("size")),
            "cond": text(row.get("cond")),
            "ex": text(row.get("ex"))})
    return out


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    D = Decimal
    T = dt.time

    print("qattsource --self-test\n\nthe queries name the right columns")
    check("the tick query names no column at all - qatt's schema is not "
          "confirmed, and naming a column it lacks is a hard q error",
          "select from qatt" in ticks_q(), True)
    check("so the time column does not reach the query, and asking for "
          "another changes nothing",
          ticks_q("srcTime"), ticks_q("tradeTime"))
    check("the two predicates that matter are both there",
          "where date=d, sym in s" in ticks_q(), True)
    check("it does NOT cast the sym list - pykx sends symbols already, "
          "and `$ on a symbol is the 'type LimitUpDown paid for",
          "sym in s" in ticks_q(), True)
    check("and no query anywhere still carries that cast",
          [q for q in (MASTER_BPIPE_Q, MASTER_MBPIPE_Q, MASTER_SYM_Q,
                       ticks_q(), probe_q()) if "`$" in q], [])
    print("\ntoday comes from the RDB, which has no date at all")
    check("the live query names no date, because there is none to name",
          "date" in live_ticks_q(), False)
    check("it takes only the syms - one argument, not two",
          live_ticks_q().startswith("{[s]"), True)
    check("it names no column either, for the same reason",
          "select from qatt" in live_ticks_q(), True)
    check("and differs from the dated one in exactly one thing: the date",
          live_ticks_q().replace("{[s]", "{[d;s]")
          .replace("where sym in s", "where date=d, sym in s"), ticks_q())
    check("the dated query does name the partition, and still does",
          "date=d" in ticks_q(), True)

    check("the probe asks for all five time columns at once",
          all(f in probe_q() for f in TIME_FIELDS), True)
    check("equity_master is asked for the four cross-references",
          all(f in MASTER_BPIPE_Q for f in MASTER_FIELDS), True)

    print("\nresolving the equity_master date, both ways")

    class FakeConn:
        """Answers by query text, so a test can fail one way and not the
        other - which is the whole point of there being two."""
        def __init__(self, answers):
            self.answers, self.asked = answers, []
        def __call__(self, query, *args):
            self.asked.append(query)
            got = self.answers.get(query)
            if isinstance(got, Exception):
                raise got
            return got

    day = dt.date(2026, 9, 4)
    c = FakeConn({MAXDATE_CLIENT_Q: day})
    check("the client form answers, and is asked first",
          (resolve_master_date(c, day), len(c.asked)), (day, 1))

    c = FakeConn({MAXDATE_CLIENT_Q: RuntimeError("type"),
                  MAXDATE_SERVER_Q: day})
    check("a 'type on the client form falls through to the server form",
          resolve_master_date(c, day), day)

    c = FakeConn({MAXDATE_CLIENT_Q: None, MAXDATE_SERVER_Q: day})
    check("so does a null answer, which is not an error but is not a date",
          resolve_master_date(c, day), day)

    c = FakeConn({MAXDATE_CLIENT_Q: RuntimeError("type"),
                  MAXDATE_SERVER_Q: RuntimeError("type")})
    try:
        resolve_master_date(c, day)
        got = "no exit"
    except SystemExit as e:
        got = "schema" if "not be a q date column" in str(e) else str(e)
    check("both failing says SCHEMA, and names the probe that settles it",
          got, "schema")

    print("\na query sent with no arguments must not be a lambda")
    check("the partitions query is an expression - a lambda literal would "
          "come back unapplied, and its characters are not dates",
          PARTITIONS_Q.startswith("{"), False)
    check("and it is still the only one sent bare; every other query takes "
          "an argument, which is what makes pykx apply it",
          [q for q in (MAXDATE_CLIENT_Q, MAXDATE_SERVER_Q, MASTER_BPIPE_Q,
                       MASTER_MBPIPE_Q, MASTER_SYM_Q, ticks_q(),
                       live_ticks_q(), probe_q(), ANY_ON_DATE_Q,
                       SAMPLE_SYMS_Q, ANY_SYM_Q)
           if not q.startswith("{[")], [])

    print("\nreading qatt's real column names")

    class OneRow:
        def __call__(self, q, *args):
            return [{"sym": "7203.JP", "tradeTime": 1, "price": 2,
                     "size": 3, "cond": "T", "ex": "T"}]

    class NoRow:
        def __call__(self, q, *args):
            return []

    cols = sample_columns(OneRow(), dt.date(2026, 9, 4))
    check("the names come back in order, so a placeholder can be checked "
          "against them", cols,
          ["sym", "tradeTime", "price", "size", "cond", "ex"])
    check("the ones this module assumes are all there, in this sample",
          [f for f in (TIME_FIELD,) + TICK_FIELDS if f not in cols], [])
    check("an empty partition names nothing rather than raising",
          sample_columns(NoRow(), dt.date(2026, 9, 4)), [])

    print("\nnarrowing an empty answer")

    class Narrow:
        """A qatt that answers each diagnostic query differently."""

        def __init__(self, on_date, matched, sample=None):
            self.on_date, self.matched = on_date, matched
            self.sample = sample or []

        def __call__(self, q, *args):
            if q == ANY_ON_DATE_Q:
                return self.on_date
            if q == ANY_SYM_Q:
                return self.matched
            if q == SAMPLE_SYMS_Q:
                return [{"sym": x} for x in self.sample]
            raise AssertionError(f"unexpected query {q}")

    day = dt.date(2026, 9, 4)

    found = dict(diagnose(Narrow(0, 0), day, ["7203.JP"]))
    check("an empty partition blames the date, and stops there",
          "NONE" in found["rows on that date"], True)
    check("and does not go on to accuse the syms",
          "those syms on that date" in found, False)

    found = dict(diagnose(Narrow(1, 0, ["7203.T", "6758.T"]),
                          day, ["7203.JP"]))
    check("a populated partition clears the date",
          found["rows on that date"], "yes, so the date matches")
    check("no sym match blames the sym",
          "NONE" in found["those syms on that date"], True)
    check("and shows what shape qatt actually uses, which is the answer",
          found["syms qatt holds there"], "7203.T, 6758.T")

    found = dict(diagnose(Narrow(1, 1, ["7203.JP"]), day, ["7203.JP"]))
    check("a sym that matches clears the query and points downstream",
          "downstream" in found["those syms on that date"], True)

    class Raises:
        def __call__(self, q, *args):
            raise RuntimeError("type")

    found = dict(diagnose(Raises(), day, ["7203.JP"]))
    check("a query that raises is reported, not swallowed",
          "RAISED" in found["rows on that date"], True)
    check("and the q types are reported first, so they survive a raise",
          "date sent as" in found and "syms sent as" in found, True)

    print("\ntext out of kdb")
    check("a symbol", text("7203.JP"), "7203.JP")
    check("bytes", text(b"7203.JP"), "7203.JP")
    check("kdb's null symbol is a blank, and reads as one", text(""), "")
    check("so does a null", text(None), "")
    check("whitespace is trimmed", text("  XSHG  "), "XSHG")

    print("\nnumbers out of kdb")
    check("a price", to_decimal(0.105), D("0.105"))
    check("a size", to_decimal(1000), D("1000"))
    check("bytes", to_decimal(b"2500"), D("2500"))
    check("a null is None", to_decimal(None), None)
    check("nan is None", to_decimal(float("nan")), None)
    check("a bool is not a number", to_decimal(True), None)
    check("garbage is None", to_decimal("N/A"), None)
    check("zero is KEPT here, unlike LimitUpDown's reader - q already "
          "filtered it, so a zero arriving means the filter did not run",
          to_decimal(0), D("0"))

    print("\ntimes out of kdb, in all three shapes pykx uses")
    check("a datetime.time passes through",
          to_time(T(9, 31, 33)), T(9, 31, 33))
    check("a timedelta becomes one",
          to_time(dt.timedelta(hours=9, minutes=31, seconds=33)),
          T(9, 31, 33))
    check("and a bare count of milliseconds does too",
          to_time((9 * 3600 + 31 * 60 + 33) * 1000), T(9, 31, 33))
    check("milliseconds survive the trip",
          to_time(34_293_250), T(9, 31, 33, 250_000))
    check("a datetime is reduced to its time",
          to_time(dt.datetime(2026, 9, 4, 9, 31, 33)), T(9, 31, 33))
    check("kdb's null time is not a time", to_time(-9223372036854775808), None)
    check("nor is a negative", to_time(-1), None)
    check("nor is anything past midnight", to_time(86_400_000), None)
    check("nor is None", to_time(None), None)
    check("nor a bool", to_time(True), None)

    print("\nthe clock the CSV writes")
    check("zero padded, to the second", clock(T(9, 31, 33)), "09:31:33")
    check("milliseconds are dropped, as the sample file has them",
          clock(T(9, 31, 33, 250_000)), "09:31:33")
    check("midnight", clock(T(0, 0, 0)), "00:00:00")
    check("no time writes an empty cell, not the word None", clock(None), "")

    print("\nresolving syms against equity_master")

    class Conn:
        """One name per pass: Toyota on sym_bpipe, BHP on sym_mbpipe, and a
        Shanghai line that only the composite finds."""

        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            if "sym_bpipe in" in q:
                return [{"sym_bpipe": "7203.JT", "sym": "7203.JP",
                         "EQY_PRIM_EXCH_SHRT": "JT",
                         "COMPOSITE_EXCH_CODE": "JP",
                         "ID_MIC_PRIM_EXCH": "XTKS"}]
            if "sym_mbpipe in" in q:
                return [{"sym_mbpipe": "BHP AU EQUITY", "sym": "BHP.AU",
                         "EQY_PRIM_EXCH_SHRT": "AU",
                         "COMPOSITE_EXCH_CODE": "AU",
                         "ID_MIC_PRIM_EXCH": "XASX"}]
            if "sym in" in q:
                return [{"sym": "600000.CH",
                         "EQY_PRIM_EXCH_SHRT": "C1",
                         "COMPOSITE_EXCH_CODE": "CH",
                         "ID_MIC_PRIM_EXCH": "XSHG"}]
            return []

    c = Conn()
    got = fetch_master(c, dt.date(2026, 9, 4), {
        "7203 JT": ("7203.JT", "7203 JT EQUITY", "7203.JP"),
        "BHP AU": ("BHP.AU", "BHP AU EQUITY", "BHP.AU"),
        "600000 C1": ("600000.C1", "600000 C1 EQUITY", "600000.CH"),
        "ZZZ XX": ("ZZZ.XX", "ZZZ XX EQUITY", "")})

    check("three passes, and only three round trips", len(c.calls), 3)
    check("the first pass answers Toyota",
          got["rows"]["7203 JT"]["sym"], "7203.JP")
    check("the crosscode's JT becomes qatt's JP, which is the whole point",
          got["rows"]["7203 JT"]["COMPOSITE_EXCH_CODE"], "JP")
    check("the second pass picks up what the first missed",
          got["rows"]["BHP AU"]["sym"], "BHP.AU")
    check("the third finds Shanghai, whose C1 matches nothing but whose "
          "600000.CH does - the whole reason the pass exists",
          got["rows"]["600000 C1"]["sym"], "600000.CH")
    check("and it comes back with its MIC",
          got["rows"]["600000 C1"]["ID_MIC_PRIM_EXCH"], "XSHG")
    check("the MIC comes back for Japan too",
          got["rows"]["7203 JT"]["ID_MIC_PRIM_EXCH"], "XTKS")
    check("a name in none of the three is simply absent, for the caller "
          "to report", "ZZZ XX" in got["rows"], False)
    check("and which pass answered is counted",
          got["hits"], {"sym_bpipe": 1, "sym_mbpipe": 1, "sym": 1})
    check("the second pass is asked only about the leftovers, not the "
          "whole universe again - and in sym_mbpipe's shape, not sym_bpipe's",
          sorted(c.calls[1][1][1]),
          ["600000 C1 EQUITY", "BHP AU EQUITY", "ZZZ XX EQUITY"])
    check("and the third only about what is still missing, in composite "
          "shape - a name with no composite candidate is not asked about "
          "at all",
          sorted(c.calls[2][1][1]), ["600000.CH"])
    check("nothing to resolve means no round trip at all",
          fetch_master(c, dt.date(2026, 9, 4), {}), {})
    check("and no extra call", len(c.calls), 3)

    print("\nfetching ticks")

    class TickConn:
        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return [{"sym": "7203.JP", "tradeTime": T(9, 31, 33),
                     "price": 2500.0, "size": 100, "cond": "T", "ex": "T"},
                    {"sym": "7203.JP", "tradeTime": T(9, 31, 34),
                     "price": 2501.0, "size": 200, "cond": "", "ex": "H"},
                    {"sym": "BHP.AU", "tradeTime": T(10, 0, 0),
                     "price": 40.5, "size": 300, "cond": "OA", "ex": "T"}]

    tc = TickConn()
    ticks = fetch_ticks(tc, dt.date(2026, 9, 4), ["7203.JP", "BHP.AU"])
    check("one round trip for the whole chunk", len(tc.calls), 1)
    check("grouped by sym, so one read becomes many files",
          sorted(ticks), ["7203.JP", "BHP.AU"])
    check("both of Toyota's prints", len(ticks["7203.JP"]), 2)
    check("the row carries what the CSV needs and nothing else",
          sorted(ticks["7203.JP"][0]),
          ["cond", "ex", "price", "size", "time"])
    check("the time is already a clock string",
          ticks["7203.JP"][0]["time"], "09:31:33")
    check("an empty condition stays empty rather than becoming None",
          ticks["7203.JP"][1]["cond"], "")
    check("no syms means no round trip",
          fetch_ticks(tc, dt.date(2026, 9, 4), []), {})
    check("and no extra call", len(tc.calls), 1)

    print("\nthe partitions that cap a backfill")

    class PartConn:
        def __call__(self, q, *args):
            return [dt.date(2026, 9, 4), dt.date(2026, 9, 2),
                    dt.date(2026, 9, 3)]

    check("sorted oldest first, so a caller can take the last N",
          partitions(PartConn()),
          [dt.date(2026, 9, 2), dt.date(2026, 9, 3), dt.date(2026, 9, 4)])

    class EmptyConn:
        def __call__(self, q, *args):
            return None

    check("an empty store is an empty list, not a crash",
          partitions(EmptyConn()), [])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
