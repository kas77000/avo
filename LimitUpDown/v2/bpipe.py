#!/usr/bin/env python3
"""The limits, from B-PIPE.  The only module here that talks to Bloomberg.

THE REAL-TIME FIELD FAMILY, NOT THE STATIC ONE.  Bloomberg carries daily
price limits under two sets of names, and our B-PIPE entitlement serves only
one of them.  A probe on 2026-09-03 got "Field not permitted to datafeed
users" for PX_MIN_LIMIT, PX_MAX_LIMIT and PX_LAST against a Japanese name,
while MIN_LIMIT and MAX_LIMIT answered on the same request:

    barred (static)      served (real-time)
    PX_MAX_LIMIT    ->   MAX_LIMIT
    PX_MIN_LIMIT    ->   MIN_LIMIT
    PX_LAST         ->   LAST_PRICE

These are not two spellings of one field.  Anything this job needs must be
found in the real-time family, and sometimes there is no equivalent.

ONE REQUEST PER BATCH, not per name.  A universe is thousands of
instruments and //blp/refdata takes a list.  Real-time FIELDS do not oblige
us to hold a real-time SUBSCRIPTION: this file is written once a day and a
request is the honest fit.  other/bpipe_probe.py holds a streaming version
of the same question if a live feed is ever wanted.

blpapi is imported inside _blpapi(), so --self-test and --demo run on a
machine with no Bloomberg at all.

    python bpipe.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#  MIN_LIMIT and MAX_LIMIT are the file.  LAST_PRICE is the sanity check: a
#  limit that does not bracket the last trade is a limit we should not
#  publish.
#
#  STATUS_FIELD is a CROSS-CHECK, no longer the filter.  The real ACTV test
#  runs in crosscode.py against CrossCode's own BloombergStatus column - a
#  status we already have, in a file we already read.  MARKET_STATUS is a
#  STATIC field and may be barred or absent for us; when it does not answer
#  the check does not fire, and the run report says so on its own line.
#  Nothing depends on it, which is the point.
#
#  DO NOT SUBSTITUTE RT_EXCH_MARKET_STATUS FOR IT.  Bloomberg's own
#  real-time model has two different status axes, visible as two different
#  MKTDATA_EVENT_SUBTYPE values in the ai3 B-PIPE code:
#
#      MARKETSTATUS     the exchange's SESSION phase - open, closed,
#                       auction, halt.  RT_EXCH_MARKET_STATUS lives here.
#      SECURITYSTATUS   the INSTRUMENT's own state.  RT_SIMP_SEC_STATUS
#                       lives here.
#
#  The ACTV test is a lifecycle question - is this listing alive - not a
#  "is it trading right now" question, despite the field's name.  This job
#  runs 07:30-09:03 Hong Kong, which is pre-open or closed for every market
#  in scope, so filtering on a SESSION field would drop the whole universe
#  every single day.
#
#  The diagnostics below are requested and TALLIED but never filtered on,
#  so one real run shows what values they actually carry.  When that is
#  known, point STATUS_FIELD at the right one and set STATUS_ACTIVE - or
#  leave it, now that the published file no longer hangs on the answer.
STATUS_FIELD = "MARKET_STATUS"
STATUS_ACTIVE = ("ACTV",)
STATUS_DIAGNOSTIC = ["RT_SIMP_SEC_STATUS", "RT_EXCH_MARKET_STATUS"]

FIELDS = (["MIN_LIMIT", "MAX_LIMIT", "LAST_PRICE", STATUS_FIELD]
          + STATUS_DIAGNOSTIC)

#  The computed side needs yesterday's close.  The obvious name for it,
#  PX_YEST_CLOSE, is STATIC and so almost certainly barred to us - PX_LAST
#  already was.  These are the real-time candidates, in the order they are
#  preferred, taken from field lists this desk already subscribes to in the
#  ai3 B-PIPE code.
#
#  WHICH ONE ANSWERS IS NOT YET KNOWN.  Rather than guess a single mnemonic
#  and get an empty Indonesia, ask for all of them, use the first that
#  answers per name, and REPORT the tally - the first real run then tells us
#  which to keep.  PX_YEST_CLOSE rides along purely as a diagnostic: if it
#  turns out to be permitted after all, that is worth knowing too.
PREV_CLOSE_FIELDS = ["PREV_CLOSE_VALUE_REALTIME",
                     "PRICE_PREVIOUS_CLOSE_RT",
                     "ADJUSTED_PREV_LAST_PRICE_RT"]

PREV_CLOSE_DIAGNOSTIC = ["PX_YEST_CLOSE"]

#  Securities per //blp/refdata request.  Bloomberg accepts more; 100 keeps
#  a single failure small and the responses readable in a log.
CHUNK = 100

AUTH_TEMPLATE = ("AuthenticationMode=APPLICATION_ONLY;"
                 "ApplicationAuthenticationType=APPNAME_AND_KEY;"
                 "ApplicationName={app}")

TIMEOUT_MS = 60_000


class BpipeError(Exception):
    pass


def _blpapi():
    try:
        import blpapi
    except ImportError:
        raise SystemExit(
            "blpapi is not installed.\n"
            "    pip install --index-url="
            "https://blpapi.bloomberg.com/repository/releases/python/simple/"
            " blpapi")
    return blpapi


# =============================================================================
# PURE - no Bloomberg, no clock
# =============================================================================

def chunks(items, size: int):
    """Split a universe into request sized pieces."""
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def to_decimal(value):
    """Anything that is not a positive finite number is not a price.  A limit
    of 0 is not a limit; it is a missing value wearing a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not (d.is_finite() and d > 0):
        return None
    #  Drop a trailing .0 that came from a float: Decimal(str(10.0)) is
    #  Decimal('10.0').  Value-preserving, and it matters because exclusion
    #  REASONS carry the price - "no band tier for price 10.0" and "...10"
    #  are the same cause, and the run report groups by that string.  Not
    #  .normalize(), which would turn 8000 into 8E+3.
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return d


def _text(value):
    """A status field as a clean string, or None if it was not served."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode(errors="replace")
    value = str(value).strip()
    return value or None


def status_tally(values, fields=None):
    """{field: {value: how many securities}} across a whole fetch.

    The point of this is to answer, from one real run, what a candidate
    status field actually carries - so STATUS_FIELD can be pointed at the
    right one on evidence rather than on the strength of its name."""
    fields = fields or ([STATUS_FIELD] + STATUS_DIAGNOSTIC)
    out = {}
    for fields_of_one in values.values():
        for field in fields:
            text = _text(fields_of_one.get(field))
            if text is not None:
                seen = out.setdefault(field, {})
                seen[text] = seen.get(text, 0) + 1
    return out


def band_from(values: dict, status_field=None, active=None):
    """One security's fields -> (down, up) or a reason we will not publish it.

    Returns (band, reason); exactly one of them is None.

    A last price outside the band means one of the three numbers is wrong,
    so the name is excluded with a reason rather than published.

    A MISSING last price is NOT a veto.  It is counted and reported instead,
    because this job runs pre-open and a real-time field may simply not have
    ticked yet - vetoing on absence would empty the file.  If LAST_PRICE
    turns out to be always populated at run time, tighten this."""
    #  Checked FIRST: there is no point reading the limits of a name that
    #  is not a live listing.  A status we were not served is not a status
    #  of 'not active' - absent means the filter does not fire, and the run
    #  report carries the field-level reason why.
    status = _text(values.get(status_field or STATUS_FIELD))
    if status is not None and status.upper() not in {
            s.upper() for s in (active or STATUS_ACTIVE)}:
        return None, (f"{status_field or STATUS_FIELD} is {status}, not "
                      f"{'/'.join(active or STATUS_ACTIVE)}")

    low = to_decimal(values.get("MIN_LIMIT"))
    high = to_decimal(values.get("MAX_LIMIT"))
    last = to_decimal(values.get("LAST_PRICE"))

    if low is None and high is None:
        return None, "no limits from Bloomberg"
    if low is None:
        return None, "no MIN_LIMIT"
    if high is None:
        return None, "no MAX_LIMIT"
    if high <= low:
        return None, f"limits inverted: MAX {high} <= MIN {low}"
    if last is not None and not (low <= last <= high):
        return None, "last price outside the limits"
    return (low, high), None


def prev_close_from(values: dict, candidates=None):
    """The reference price for a computed venue: (price, which field gave
    it), or (None, None).

    First candidate that answers wins, and the caller is told WHICH so the
    run report can say so.  A silent fallback chain would hide the day one
    of them stops answering."""
    for field in (candidates or PREV_CLOSE_FIELDS):
        price = to_decimal(values.get(field))
        if price is not None:
            return price, field
    return None, None


# =============================================================================
# CONNECTION
# =============================================================================

def connect(host: str, port: int, app_name: str):
    """Connect and authorize as the application.  Returns (session, identity).

    The Bloomberg API Demo Tool's "Simplify Authentication" tick is these two
    steps: generate a token, then send an AuthorizationRequest and keep the
    Identity it fills in.  Every request afterwards is made AS that identity;
    without it B-PIPE answers with an entitlement failure, not a price."""
    blpapi = _blpapi()

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(int(port))
    if app_name:
        opts.setAuthenticationOptions(AUTH_TEMPLATE.format(app=app_name))

    session = blpapi.Session(opts)
    if not session.start():
        raise BpipeError(f"cannot reach B-PIPE at {host}:{port}")

    identity = None
    if app_name:
        identity = _authorize(session, app_name)

    if not session.openService("//blp/refdata"):
        raise BpipeError("could not open //blp/refdata")
    return session, identity


def _authorize(session, app_name: str):
    blpapi = _blpapi()

    queue = blpapi.EventQueue()
    session.generateToken(eventQueue=queue)
    token = None
    while token is None:
        event = queue.nextEvent(TIMEOUT_MS)
        for msg in event:
            kind = str(msg.messageType())
            if kind == "TokenGenerationSuccess":
                token = msg.getElementAsString("token")
            elif kind == "TokenGenerationFailure":
                raise BpipeError(f"token generation refused: {msg}")
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise BpipeError("timed out waiting for a token")

    if not session.openService("//blp/apiauth"):
        raise BpipeError("could not open //blp/apiauth")

    request = session.getService("//blp/apiauth").createAuthorizationRequest()
    request.set("token", token)
    identity = session.createIdentity()
    queue = blpapi.EventQueue()
    session.sendAuthorizationRequest(
        request, identity, blpapi.CorrelationId("auth"), queue)

    while True:
        event = queue.nextEvent(TIMEOUT_MS)
        for msg in event:
            kind = str(msg.messageType())
            if kind == "AuthorizationSuccess":
                return identity
            if kind == "AuthorizationFailure":
                raise BpipeError(
                    f"B-PIPE refused the application {app_name!r}: {msg}")
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise BpipeError("timed out waiting for authorization")


def _element_to_dict(element) -> dict:
    out = {}
    for i in range(element.numElements()):
        child = element.getElement(i)
        try:
            out[str(child.name())] = child.getValue()
        except Exception:                            # noqa: BLE001
            out[str(child.name())] = None
    return out


def _one_request(session, identity, securities, fields):
    """One //blp/refdata batch -> {security: {field: value}}, plus the
    securities Bloomberg refused by name."""
    blpapi = _blpapi()
    service = session.getService("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")
    for security in securities:
        request.getElement("securities").appendValue(security)
    for field in fields:
        request.getElement("fields").appendValue(field)

    session.sendRequest(request, identity)

    values, refused, field_problems = {}, {}, {}
    while True:
        event = session.nextEvent(TIMEOUT_MS)
        for msg in event:
            if msg.hasElement("responseError"):
                raise BpipeError(
                    f"responseError: {msg.getElement('responseError')}")
            if not msg.hasElement("securityData"):
                continue
            data = msg.getElement("securityData")
            for i in range(data.numValues()):
                entry = data.getValueAsElement(i)
                security = entry.getElementAsString("security")
                if entry.hasElement("securityError"):
                    error = entry.getElement("securityError")
                    message = (error.getElementAsString("message")
                               if error.hasElement("message") else str(error))
                    refused[security] = message
                    continue
                values[security] = _element_to_dict(
                    entry.getElement("fieldData"))
                #  A field that is real but not served to us says so here,
                #  once per security.  Collapsed to one line per field by
                #  the caller: "PX_YEST_CLOSE: not permitted (2143 names)"
                #  is the sentence that explains an empty column.
                if entry.hasElement("fieldExceptions"):
                    exceptions = entry.getElement("fieldExceptions")
                    for j in range(exceptions.numValues()):
                        ex = exceptions.getValueAsElement(j)
                        field = ex.getElementAsString("fieldId")
                        info = ex.getElement("errorInfo")
                        message = (info.getElementAsString("message")
                                   if info.hasElement("message")
                                   else str(info))
                        seen = field_problems.setdefault(field,
                                                         [message, 0])
                        seen[1] += 1
        if event.eventType() == blpapi.Event.RESPONSE:
            break
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise BpipeError("timed out waiting for a response")
    return values, refused, field_problems


def fetch(session, identity, securities, fields=None, chunk_size=CHUNK,
          progress=None):
    """Every security's fields, in batches.

    Returns (values, refused, field_problems), where field_problems is
    {field: (message, how many securities)}."""
    fields = fields or FIELDS
    values, refused, problems = {}, {}, {}
    batches = chunks(sorted(set(securities)), chunk_size)
    for n, batch in enumerate(batches, 1):
        got, bad, bad_fields = _one_request(session, identity, batch, fields)
        values.update(got)
        refused.update(bad)
        for field, (message, count) in bad_fields.items():
            seen = problems.setdefault(field, [message, 0])
            seen[1] += count
        if progress:
            progress(n, len(batches), len(values))
    return values, refused, {k: tuple(v) for k, v in problems.items()}


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

    D = Decimal
    print("bpipe --self-test\n\nbatching the universe")
    check("an exact fit", chunks([1, 2, 3, 4], 2), [[1, 2], [3, 4]])
    check("a remainder rides in the last batch", chunks([1, 2, 3], 2),
          [[1, 2], [3]])
    check("fewer than one batch", chunks([1], 100), [[1]])
    check("an empty universe asks nothing", chunks([], 100), [])

    print("\nreading a value")
    check("a float", to_decimal(2433.0), D("2433"))
    check("a string", to_decimal("2433"), D("2433"))
    check("a whole number keeps no trailing .0 - two spellings of one price "
          "would split an exclusion reason across two report lines",
          str(to_decimal(10.0)), "10")
    check("and a big one does not go exponential either",
          str(to_decimal(8000.0)), "8000")
    check("real decimals are untouched", str(to_decimal(12.34)), "12.34")
    check("no value", to_decimal(None), None)
    check("zero is not a limit", to_decimal(0), None)
    check("nor is a negative", to_decimal(-5), None)
    check("nor is a nan", to_decimal(float("nan")), None)
    check("nor is text", to_decimal("N.A."), None)

    print("\nturning fields into a band")
    check("Toyota, as the probe returned it",
          band_from({"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                     "LAST_PRICE": 3130.0}),
          ((D("2433.0"), D("3833.0")), None))
    check("no last price is not a veto - it is reported and the band stands",
          band_from({"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0}),
          ((D("2433.0"), D("3833.0")), None))
    check("a last price ON the limit is fine - that is a limit up day",
          band_from({"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                     "LAST_PRICE": 3833.0}),
          ((D("2433.0"), D("3833.0")), None))
    check("nothing came back at all",
          band_from({}), (None, "no limits from Bloomberg"))
    check("half a band is not a band",
          band_from({"MAX_LIMIT": 3833.0}), (None, "no MIN_LIMIT"))
    check("the other half", band_from({"MIN_LIMIT": 2433.0}),
          (None, "no MAX_LIMIT"))
    check("an inverted pair is refused, not silently swapped",
          band_from({"MIN_LIMIT": 3833.0, "MAX_LIMIT": 2433.0}),
          (None, "limits inverted: MAX 2433 <= MIN 3833"))
    check("a last price outside the band means one of the three numbers "
          "is wrong",
          band_from({"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0,
                     "LAST_PRICE": 9999.0}),
          (None, "last price outside the limits"))
    check("a zero limit is missing data, not a floor of zero",
          band_from({"MIN_LIMIT": 0.0, "MAX_LIMIT": 3833.0}),
          (None, "no MIN_LIMIT"))

    print("\nthe market status filter")
    live = {"MIN_LIMIT": 2433.0, "MAX_LIMIT": 3833.0, "LAST_PRICE": 3130.0}
    check("an ACTV name passes",
          band_from({**live, "MARKET_STATUS": "ACTV"}),
          ((D("2433"), D("3833")), None))
    check("anything else is dropped, even with a perfectly good band",
          band_from({**live, "MARKET_STATUS": "DLST"}),
          (None, "MARKET_STATUS is DLST, not ACTV"))
    check("checked BEFORE the limits - no point pricing a dead listing",
          band_from({"MARKET_STATUS": "DLST"}),
          (None, "MARKET_STATUS is DLST, not ACTV"))
    check("bytes, as a status column can arrive",
          band_from({**live, "MARKET_STATUS": b"ACTV"}),
          ((D("2433"), D("3833")), None))
    check("case and padding do not make a live name dead",
          band_from({**live, "MARKET_STATUS": " actv "}),
          ((D("2433"), D("3833")), None))
    check("a status we were NOT SERVED is not a status of 'not active' - "
          "the filter simply does not fire",
          band_from(live), ((D("2433"), D("3833")), None))
    check("an empty string is 'not served', not a status of ''",
          band_from({**live, "MARKET_STATUS": "  "}),
          ((D("2433"), D("3833")), None))

    print("\npointing the filter at a different field")
    check("a session field would be a DISASTER as the filter - pre-open, "
          "every name reads CLOSED and the whole file empties",
          band_from({**live, "RT_EXCH_MARKET_STATUS": "CLOSED"},
                    status_field="RT_EXCH_MARKET_STATUS", active=("OPEN",)),
          (None, "RT_EXCH_MARKET_STATUS is CLOSED, not OPEN"))
    check("and by default that field is NOT the one filtered on",
          band_from({**live, "RT_EXCH_MARKET_STATUS": "CLOSED"}),
          ((D("2433"), D("3833")), None))
    check("several values can be acceptable",
          band_from({**live, "RT_SIMP_SEC_STATUS": "TRADING"},
                    status_field="RT_SIMP_SEC_STATUS",
                    active=("TRADING", "ACTIVE")),
          ((D("2433"), D("3833")), None))

    print("\ntallying what the status fields actually carry")
    fetched = {"A": {"MARKET_STATUS": "ACTV",
                     "RT_SIMP_SEC_STATUS": "TRADING",
                     "RT_EXCH_MARKET_STATUS": "CLOSED"},
               "B": {"RT_SIMP_SEC_STATUS": "TRADING",
                     "RT_EXCH_MARKET_STATUS": "CLOSED"},
               "C": {"RT_SIMP_SEC_STATUS": "HALTED"}}
    check("counted per field per value, which is how one real run tells us "
          "which field to filter on",
          status_tally(fetched),
          {"MARKET_STATUS": {"ACTV": 1},
           "RT_SIMP_SEC_STATUS": {"TRADING": 2, "HALTED": 1},
           "RT_EXCH_MARKET_STATUS": {"CLOSED": 2}})
    check("a field nobody was served does not appear at all",
          status_tally({"A": {"MIN_LIMIT": 1.0}}), {})

    print("\nfinding a previous close for a computed venue")
    check("the first candidate that answers wins, and names itself",
          prev_close_from({"PREV_CLOSE_VALUE_REALTIME": 8000.0}),
          (D("8000.0"), "PREV_CLOSE_VALUE_REALTIME"))
    check("the second, when the first is absent",
          prev_close_from({"PRICE_PREVIOUS_CLOSE_RT": 8000.0}),
          (D("8000.0"), "PRICE_PREVIOUS_CLOSE_RT"))
    check("preference order holds when several answer",
          prev_close_from({"ADJUSTED_PREV_LAST_PRICE_RT": 1.0,
                           "PREV_CLOSE_VALUE_REALTIME": 8000.0}),
          (D("8000.0"), "PREV_CLOSE_VALUE_REALTIME"))
    check("a field present but empty is skipped, not taken as zero",
          prev_close_from({"PREV_CLOSE_VALUE_REALTIME": None,
                           "PRICE_PREVIOUS_CLOSE_RT": 8000.0}),
          (D("8000.0"), "PRICE_PREVIOUS_CLOSE_RT"))
    check("nothing answered", prev_close_from({}), (None, None))
    check("and a zero close is nothing, not a price",
          prev_close_from({"PREV_CLOSE_VALUE_REALTIME": 0.0}), (None, None))

    print("\nthe authorization string")
    check("application only, app name and key",
          AUTH_TEMPLATE.format(app="APPNAME"),
          "AuthenticationMode=APPLICATION_ONLY;"
          "ApplicationAuthenticationType=APPNAME_AND_KEY;"
          "ApplicationName=APPNAME")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
