#!/usr/bin/env python3
"""Settings, from local_settings.py beside this file.

TWO MACHINES, ONE FILE EACH.  qatt_export.py runs where kdb is and
qatt_dispatch.py runs where the tick store is, so each machine has its own
local_settings.py and fills in only what its script uses.  CROSSCODE_PATH is
the same name on both and a different path on each.

STRICT, as in Historical: a name no script defines is an ERROR, so a typo'd
OUTPUT_DIRR fails loudly instead of doing nothing while the files go
somewhere else.

    python settings.py --self-test
"""

from __future__ import annotations

#  Every setting, with the default used when local_settings.py is silent.
#  "" means there is no sane default and the script that needs it refuses.
DEFAULTS = {
    #  qatt_export.py only.  equity_master is on the order side (:5010),
    #  qatt on its own (:5011), and QATT_SERVER must be the HDB.
    "EQUITY_MASTER_SERVER": "",
    "QATT_SERVER": "",
    #  Where qatt_export.py leaves qatt-YYYYMMDD.zip.
    "EXPORT_DIR": "",

    #  Both scripts: NewCrosscode.csv, wherever it lives on THIS machine.
    "CROSSCODE_PATH": "",

    #  qatt_dispatch.py only: the tick store, one folder per name.
    "OUTPUT_DIR": "",

    #  The zone kdb stamps its prints in.  Written into the bundle by the
    #  export, so the dispatch converts from what the export actually saw.
    "KDB_TIMEZONE": "China Standard Time",
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

    class Export:
        QATT_SERVER = "kdb1:5011"
        CROSSCODE_PATH = r"C:\a\NewCrosscode.csv"

    class Dispatch:
        CROSSCODE_PATH = r"D:\b\NewCrosscode.csv"
        OUTPUT_DIR = r"D:\ticks"

    class Typo:
        OUTPUT_DIRR = "X"

    e, d = merge(Export()), merge(Dispatch())
    check("each machine sets its own crosscode path",
          (e["CROSSCODE_PATH"], d["CROSSCODE_PATH"]),
          (r"C:\a\NewCrosscode.csv", r"D:\b\NewCrosscode.csv"))
    check("the dispatch machine needs no kdb server",
          require(d, "CROSSCODE_PATH", "OUTPUT_DIR"), None)
    raises("the export refuses without its servers, by name",
           lambda: require(e, "EQUITY_MASTER_SERVER", "EXPORT_DIR"),
           "EQUITY_MASTER_SERVER, EXPORT_DIR")
    check("host and port", server(e, "QATT_SERVER"), ("kdb1", 5011))
    raises("a bare host is refused", lambda: hostport("kdb1", "Q"),
           "host:port")
    raises("a typo is an error naming the typo", lambda: merge(Typo()),
           "OUTPUT_DIRR")
    check("the defaults are not mutated", DEFAULTS["CROSSCODE_PATH"], "")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
