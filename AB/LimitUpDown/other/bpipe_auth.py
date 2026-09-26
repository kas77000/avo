#!/usr/bin/env python3
"""Which B-PIPE identity do we have, and what is it entitled to?

The limit job authenticates APPLICATION_ONLY, so every request carries the
APPLICATION's entitlements.  When B-PIPE answers

    Security Entitlement Check Failed! EID(s) needed: 64487 or 64488

that is not a bug, and no retry, ticker form or field substitution gets past
it: the identity making the request does not hold that exchange.  Before
anyone buys an entitlement it is worth knowing whether a DIFFERENT identity
already has it, because an application login is usually narrower than a
person's.

Three questions, in order:

  app    does APPLICATION_ONLY authorize, and does it hold the EIDs?
  user   is there a USER account reachable from this machine at all?
         AuthenticationType=OS_LOGON asks for a token naming the logged-in
         Windows user.  If that authorizes, you have a Bloomberg user
         identity here - which is the question worth settling first.
  both   does USER_AND_APPLICATION authorize, and hold the EIDs?

A mode that fails is an ANSWER, not a crash.  Each is caught and reported so
that one run tells you the whole picture rather than stopping at the first
refusal.

    python bpipe_auth.py                       three modes, EIDs 64487/64488
    python bpipe_auth.py --eid 64487 --eid 12345
    python bpipe_auth.py --only user           just one mode
    python bpipe_auth.py --self-test           no Bloomberg, no network

Connection settings come from bpipe_probe.py so there is one place to fill
them in.  They ship empty, and this refuses to start rather than connect
somewhere you did not mean.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

import bpipe_probe
from bpipe_probe import SettingError, resolve_connection

TIMEOUT_MS = 30_000

#  The EIDs the 2026-09-04 run was refused for.  Override with --eid.
DEFAULT_EIDS = ["64487", "64488"]

#  Written without backslash escapes on purpose: this file is maintained
#  through a shell that mangles them.
_EID_AFTER = re.compile(re.escape("EID(s)") + "[^:]*:[ ]*([0-9 ,]*[0-9])")


def eids_in(message) -> list:
    """The EID numbers out of a refusal message.

    'EID(s) needed: 64487 or 64488' -> ['64487', '64488'].  The 'or' means
    either one suffices, which usually marks a real-time and a delayed
    variant of the same feed - worth knowing, because the delayed one is
    cheaper and this job runs pre-open anyway."""
    text = (message or "").replace(" or ", " ").replace(" and ", " ")
    found = _EID_AFTER.search(text)
    if not found:
        return []
    return re.findall("[0-9]+", found.group(1))


#  (key, what it means, template).  A template with no {app} needs no
#  application name at all, which is the whole point of the user-only mode.
MODES = [
    ("app",
     "APPLICATION_ONLY - what the limit job uses today",
     "AuthenticationMode=APPLICATION_ONLY;"
     "ApplicationAuthenticationType=APPNAME_AND_KEY;"
     "ApplicationName={app}"),
    ("user",
     "USER only, by OS logon - do you have a user account here?",
     "AuthenticationType=OS_LOGON"),
    ("both",
     "USER_AND_APPLICATION - the user's entitlements, through the app",
     "AuthenticationMode=USER_AND_APPLICATION;"
     "AuthenticationType=OS_LOGON;"
     "ApplicationAuthenticationType=APPNAME_AND_KEY;"
     "ApplicationName={app}"),
]

MODE_KEYS = [key for key, _, _ in MODES]


def auth_options(key: str, app: str) -> str:
    """The authentication string for one mode."""
    for mode_key, _, template in MODES:
        if mode_key == key:
            if "{app}" in template:
                return template.format(app=app)
            return template
    raise SettingError(f"unknown mode {key!r}, expected one of "
                       + ", ".join(MODE_KEYS))


def describe(key: str) -> str:
    for mode_key, name, _ in MODES:
        if mode_key == key:
            return name
    return key


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def has_entitlements(identity, service, eids):
    """(held, error).  `held` is True, False, or None when we could not ask.

    blpapi has moved this signature between releases, so both argument
    orders are tried rather than pinning one and being wrong on whatever is
    installed on the target machine.  A probe that reports 'no' because it
    called the API wrongly is worse than one that admits it could not tell."""
    numbers = [int(e) for e in eids]
    for args in ((service, numbers), (numbers, service)):
        try:
            return bool(identity.hasEntitlements(*args)), None
        except TypeError:
            continue
        except Exception as e:                              # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
    return None, "hasEntitlements refused both argument orders"


def failed_entitlements(identity, service, eids):
    """Which of `eids` the identity does NOT hold, when blpapi will say."""
    numbers = [int(e) for e in eids]
    for args in ((service, numbers), (numbers, service)):
        try:
            result = identity.getFailedEntitlements(*args)
        except TypeError:
            continue
        except Exception:                                   # noqa: BLE001
            return None
        #  Some releases return (bool, list), others just the list.
        if isinstance(result, tuple) and len(result) == 2:
            return list(result[1])
        return list(result) if result is not None else []
    return None


def try_mode(key: str, host: str, port: int, app: str, eids: list) -> dict:
    """Authorize under one mode and report what it holds.

    Never raises.  A mode that cannot authorize is the finding."""
    blpapi = bpipe_probe._blpapi()
    out = {"mode": key, "name": describe(key), "authorized": False,
           "detail": "", "entitled": None, "missing": None}

    session = None
    try:
        options = blpapi.SessionOptions()
        options.setServerHost(host)
        options.setServerPort(port)
        options.setAuthenticationOptions(auth_options(key, app))

        session = blpapi.Session(options)
        if not session.start():
            out["detail"] = "session would not start"
            return out

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
                    out["detail"] = f"no token: {msg}".strip()
                    return out
            if event.eventType() == blpapi.Event.TIMEOUT:
                out["detail"] = "timed out waiting for a token"
                return out

        if not session.openService("//blp/apiauth"):
            out["detail"] = "could not open //blp/apiauth"
            return out
        auth_service = session.getService("//blp/apiauth")

        request = auth_service.createAuthorizationRequest()
        request.set("token", token)
        identity = session.createIdentity()
        queue = blpapi.EventQueue()
        session.sendAuthorizationRequest(
            request, identity, blpapi.CorrelationId("auth"), queue)

        while not out["authorized"]:
            event = queue.nextEvent(TIMEOUT_MS)
            for msg in event:
                kind = str(msg.messageType())
                if kind == "AuthorizationSuccess":
                    out["authorized"] = True
                elif kind == "AuthorizationFailure":
                    out["detail"] = str(msg).strip()
                    return out
            if event.eventType() == blpapi.Event.TIMEOUT:
                out["detail"] = "timed out waiting for authorization"
                return out

        #  Entitlements are per SERVICE, so ask about the one the limit job
        #  uses.  //blp/refdata is where the refusal came from.
        if not session.openService("//blp/refdata"):
            out["detail"] = "authorized, but //blp/refdata would not open"
            return out
        refdata = session.getService("//blp/refdata")

        held, error = has_entitlements(identity, refdata, eids)
        out["entitled"] = held
        if error:
            out["detail"] = error
        out["missing"] = failed_entitlements(identity, refdata, eids)
        return out
    except Exception as e:                                  # noqa: BLE001
        out["detail"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        if session is not None:
            try:
                session.stop()
            except Exception:                               # noqa: BLE001
                pass


def render(results: list, eids: list) -> list:
    """The summary, as lines.  Pure, so the self test can read it."""
    lines = ["  EIDs asked about: " + ", ".join(eids), ""]
    for r in results:
        if not r["authorized"]:
            said = "did NOT authorize"
        elif r["entitled"] is True:
            said = "authorized, HOLDS the EIDs"
        elif r["entitled"] is False:
            said = "authorized, does NOT hold the EIDs"
        else:
            said = "authorized, entitlement unknown"
        lines.append(f"  {r['mode']:<6} {said}")
        lines.append(f"         {r['name']}")
        if r.get("missing"):
            lines.append("         missing: "
                         + ", ".join(str(m) for m in r["missing"]))
        if r["detail"]:
            lines.append("         " + r["detail"].splitlines()[0][:110])
    return lines


def verdict(results: list) -> str:
    """The one sentence worth reading."""
    holders = [r["mode"] for r in results if r["entitled"] is True]
    users = [r["mode"] for r in results
             if r["authorized"] and r["mode"] in ("user", "both")]
    if holders:
        return ("An identity that HOLDS these EIDs exists here: "
                + ", ".join(holders)
                + ". Point the job at that authentication mode - nothing "
                  "needs buying.")
    if users:
        return ("You DO have a user account on this machine ("
                + ", ".join(users)
                + "), but it does not hold these EIDs either. That makes "
                  "this a contract change, not a code change.")
    return ("No user identity authorized here, so the application login is "
            "all there is. Ask whether a user account can be used from this "
            "machine before treating the EIDs as unavailable.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=bpipe_probe.HOST)
    p.add_argument("--port", default=bpipe_probe.PORT)
    p.add_argument("--app", default=bpipe_probe.APP_NAME)
    p.add_argument("--eid", action="append", default=None,
                   help="an EID to test; repeatable. Default 64487, 64488")
    p.add_argument("--only", choices=MODE_KEYS, default=None,
                   help="test one mode instead of all three")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()

    eids = args.eid or list(DEFAULT_EIDS)
    for e in eids:
        if not str(e).isdigit():
            print(f"FAIL  {e!r} is not an EID number", file=sys.stderr)
            return 2

    try:
        host, port, app = resolve_connection(args.host, args.port, args.app,
                                             False)
    except SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    keys = [args.only] if args.only else MODE_KEYS
    print(f"{_now()}  {host}:{port}  application {app!r}")
    results = []
    for key in keys:
        print(f"{_now()}  trying {key} ...")
        results.append(try_mode(key, host, port, app, eids))

    print("")
    for line in render(results, eids):
        print(line)
    print("")
    print("  " + verdict(results))
    return 0 if any(r["authorized"] for r in results) else 1


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

    print("bpipe_auth --self-test")
    print("")
    print("building the authentication strings")
    check("APPLICATION_ONLY is exactly what the limit job sends today",
          auth_options("app", "an-app"),
          "AuthenticationMode=APPLICATION_ONLY;"
          "ApplicationAuthenticationType=APPNAME_AND_KEY;"
          "ApplicationName=an-app")
    check("the user mode names NO application - that is the point, it asks "
          "whether a PERSON can authenticate from this machine",
          auth_options("user", "an-app"), "AuthenticationType=OS_LOGON")
    check("and 'both' carries the user AND the application",
          auth_options("both", "an-app"),
          "AuthenticationMode=USER_AND_APPLICATION;"
          "AuthenticationType=OS_LOGON;"
          "ApplicationAuthenticationType=APPNAME_AND_KEY;"
          "ApplicationName=an-app")
    try:
        auth_options("nope", "a")
        got = "no error"
    except SettingError as e:
        got = str(e)
    check("an unknown mode is refused by name",
          "unknown mode 'nope'" in got, True)

    print("")
    print("reading the EIDs out of a refusal")
    check("the message the 2026-09-04 run actually produced",
          eids_in("Security Entitlement Check Failed! EID(s) needed: "
                  "64487 or 64488"), ["64487", "64488"])
    check("a single EID", eids_in("EID(s) needed: 12345"), ["12345"])
    check("comma separated", eids_in("EID(s) needed: 1, 2, 3"),
          ["1", "2", "3"])
    check("a refusal that is not about entitlements at all",
          eids_in("Unknown/Invalid Security"), [])
    check("nothing in, nothing out", eids_in(None), [])

    print("")
    print("the summary and the verdict")
    held = {"mode": "both", "name": describe("both"), "authorized": True,
            "detail": "", "entitled": True, "missing": []}
    denied = {"mode": "app", "name": describe("app"), "authorized": True,
              "detail": "", "entitled": False, "missing": [64487, 64488]}
    refused = {"mode": "user", "name": describe("user"), "authorized": False,
               "detail": "no token: reason", "entitled": None,
               "missing": None}
    text = chr(10).join(render([held, denied, refused], ["64487"]))
    check("a holder is called out", "HOLDS the EIDs" in text, True)
    check("a denial names which EIDs are missing",
          "missing: 64487, 64488" in text, True)
    check("a mode that never authorized says so, rather than looking like a "
          "mode with no entitlements", "did NOT authorize" in text, True)

    check("when some identity holds them, the verdict is to switch to it, "
          "because that costs nothing",
          "nothing needs buying" in verdict([held, denied]), True)
    user_but_short = dict(refused, authorized=True, entitled=False)
    check("when a user exists but is equally short, it is a contract change",
          "contract change" in verdict([denied, user_but_short]), True)
    check("and when no user authorizes at all, THAT is the thing to ask "
          "about first",
          "No user identity authorized" in verdict([denied]), True)

    print("")
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
