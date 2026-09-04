#!/usr/bin/env python3
"""Settings, from local_settings.py beside this file.  Shared by every
script here, so there is one place to fill in and one spelling of each name.

NOTHING IS PASSED ON THE COMMAND LINE.  A host and a port are the two things
worth nothing to anyone but us and something to everyone else, and a server
retyped per invocation is a server eventually typed wrong.  They live in
local_settings.py, which git ignores, and the scripts read them from here.

STRICT.  A name in local_settings.py that no script defines is an ERROR, not
a new setting.  A typo'd OUTPUT_DIRR would otherwise sit there doing nothing
while the run wrote somewhere else - and the run would look like it worked.

    python settings.py --self-test
"""

from __future__ import annotations

#  Every setting, with the default used when local_settings.py is silent.
#  "" means there is no sane default and the script must refuse to start.
DEFAULTS = {
    #  Two different servers.  equity_master sits on the order side in
    #  kdb-queries' layout (:5010); qatt has its own (:5011).
    "EQUITY_MASTER_SERVER": "",
    #  Must be the HDB - the one partitioned by date.  The RDB holds today
    #  only, and every day these jobs ask for has finished.
    "QATT_SERVER": "",

    "CROSSCODE_PATH": "",
    "OUTPUT_DIR": "",
    #  The long-term record of (name, date) pairs kdb had nothing for.
    #  Empty means <OUTPUT_DIR>/_no_data.csv.
    "MISS_CACHE_PATH": "",

    "BACKFILL_DAYS": 60,     # how deep a name with no files goes
    "SYM_CHUNK": 200,        # syms per qatt round trip
    "MASTER_CHUNK": 5000,    # codes per equity_master round trip
}


class SettingError(Exception):
    pass


def merge(module) -> dict:
    """DEFAULTS overlaid with whatever local_settings.py sets."""
    out = dict(DEFAULTS)
    unknown = [n for n in dir(module)
               if not n.startswith("_") and n not in DEFAULTS]
    if unknown:
        raise SettingError(
            f"local_settings.py sets {', '.join(sorted(unknown))}, which no "
            f"script here defines.  Known settings are "
            f"{', '.join(sorted(DEFAULTS))}.")
    for name in DEFAULTS:
        if hasattr(module, name):
            out[name] = getattr(module, name)
    return out


def load() -> dict:
    try:
        import local_settings
    except ImportError:
        raise SettingError(
            "local_settings.py not found.  Copy local_settings.py.example "
            "beside it and fill it in.")
    return merge(local_settings)


def require(cfg: dict, *names) -> None:
    """Refuse before connecting, naming what is missing.

    A script asks only for what it uses: the probe needs QATT_SERVER and
    nothing else, so a half-filled local_settings.py is enough to run it."""
    missing = [n for n in names if not str(cfg.get(n, "")).strip()]
    if missing:
        raise SettingError(
            f"{', '.join(missing)} not set in local_settings.py")


def hostport(value: str, what: str):
    """'kdb1:5011' -> ('kdb1', 5011)."""
    text = str(value or "").strip()
    if not text:
        raise SettingError(f"{what} is not set in local_settings.py")
    host, _, port = text.rpartition(":")
    if not host.strip() or not port.strip().isdigit():
        raise SettingError(
            f"{what} = {text!r} needs a host and a port, as host:port")
    return host.strip(), int(port)


def server(cfg: dict, name: str):
    require(cfg, name)
    return hostport(cfg[name], name)


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except SettingError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                 f"{fragment!r}"))

    print("settings --self-test\n\nreading a server")
    check("host and port", hostport("kdb1:5011", "X"), ("kdb1", 5011))
    check("a host containing colons keeps them - only the last is the port",
          hostport("a:b:5011", "X"), ("a:b", 5011))
    check("whitespace is not part of it",
          hostport("  kdb1:5011  ", "X"), ("kdb1", 5011))
    raises("empty is refused by name", lambda: hostport("", "QATT_SERVER"),
           "QATT_SERVER")
    raises("so is a bare host", lambda: hostport("kdb1", "Q"), "host:port")
    raises("so is a port that is not a number",
           lambda: hostport("kdb1:abc", "Q"), "host:port")
    raises("and a port with no host", lambda: hostport(":5011", "Q"),
           "host:port")

    print("\nmerging local_settings over the defaults")

    class Local:
        QATT_SERVER = "kdb1:5011"
        BACKFILL_DAYS = 5

    class Typo:
        OUTPUT_DIRR = "X"

    cfg = merge(Local())
    check("what it sets is taken", cfg["QATT_SERVER"], "kdb1:5011")
    check("and overrides the default", cfg["BACKFILL_DAYS"], 5)
    check("what it does not set keeps the default", cfg["SYM_CHUNK"], 200)
    check("a setting with no sane default stays empty, for require() to "
          "refuse on", cfg["CROSSCODE_PATH"], "")
    check("the defaults are not mutated by a merge",
          DEFAULTS["BACKFILL_DAYS"], 60)
    raises("a typo is an error naming the typo, not a setting silently "
           "doing nothing", lambda: merge(Typo()), "OUTPUT_DIRR")

    print("\nasking only for what a script uses")
    check("the probe needs one server, and a half-filled file is enough",
          require(cfg, "QATT_SERVER"), None)
    raises("the job needs more, and says which is missing",
           lambda: require(cfg, "QATT_SERVER", "CROSSCODE_PATH", "OUTPUT_DIR"),
           "CROSSCODE_PATH, OUTPUT_DIR")
    check("resolving a server does both at once",
          server(cfg, "QATT_SERVER"), ("kdb1", 5011))
    raises("and refuses an unset one by name",
           lambda: server(cfg, "EQUITY_MASTER_SERVER"),
           "EQUITY_MASTER_SERVER")
    check("whitespace only is not set",
          [n for n in ("X",) if not str({"X": "   "}.get(n, "")).strip()],
          ["X"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
