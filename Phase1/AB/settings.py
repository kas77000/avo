#!/usr/bin/env python3
"""Settings, from local_settings.py beside this file.

AB phase1 modules load their settings here. STRICT, as in Historical: a
name no script defines is an ERROR, so a typo'd name fails loudly instead of
doing nothing while the files go somewhere else.

    python settings.py --self-test
"""

from __future__ import annotations

#  Every setting, with the default used when local_settings.py is silent.
#  "" means there is no sane default and the script that needs it refuses.
DEFAULTS = {
    #  kdb servers that AB contacts.  equity_master (and the tick ladders)
    #  on the order side; QATT_SERVER is the qatt HDB, read with --date;
    #  QATT_RDB_SERVER is the RDB, read for today.
    "EQUITY_MASTER_SERVER": "",
    "QATT_SERVER": "",
    "QATT_RDB_SERVER": "",
    #  The process with the `quote` table, when it is not qatt's: with
    #  --date (HDB) and without (RDB).  Blank means qatt's server.
    "QUOTE_SERVER": "",
    "QUOTE_RDB_SERVER": "",
    #  Where extract.py leaves phase1-YYYYMMDD.zip.
    "EXPORT_DIR": "",

    #  AB modules: NewCrosscode.csv, wherever it lives on THIS machine.
    "CROSSCODE_PATH": "",

    #  The zone kdb stamps its prints in.  Written into the bundle by the
    #  export, so downstream converts from what the export actually saw.
    "KDB_TIMEZONE": "China Standard Time",
    #  The mail at the end of every extract.py run.  Blank: no mail.
    "SMTP_HOST": "",
    "EMAIL_FROM": "",
    "EMAIL_TO": [],          # a list of addresses
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
    """Refuse before doing anything, naming what is missing."""
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

    print("settings --self-test\n")

    class AB:
        QATT_SERVER = "kdb1:5011"
        CROSSCODE_PATH = r"C:\a\NewCrosscode.csv"

    class Typo:
        QATT_RDB_SERVERR = "X"

    ab = merge(AB())
    check("AB sets its own crosscode path",
          ab["CROSSCODE_PATH"],
          r"C:\a\NewCrosscode.csv")
    check("AB requires kdb servers",
          require(ab, "CROSSCODE_PATH", "QATT_SERVER"), None)
    raises("AB refuses without its servers, by name",
           lambda: require(ab, "EQUITY_MASTER_SERVER", "EXPORT_DIR"),
           "EQUITY_MASTER_SERVER, EXPORT_DIR")
    check("host and port", server(ab, "QATT_SERVER"), ("kdb1", 5011))
    raises("a bare host is refused", lambda: hostport("kdb1", "Q"),
           "host:port")
    raises("a typo is an error naming the typo", lambda: merge(Typo()),
           "QATT_RDB_SERVERR")
    check("the defaults are not mutated", DEFAULTS["CROSSCODE_PATH"], "")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
