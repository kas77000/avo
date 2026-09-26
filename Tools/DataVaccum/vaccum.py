#!/usr/bin/env python3
r"""Pull the configured files off the shares onto this machine.

Replaces the RDP session: log in, hunt through several shared drives, drag
the day's files back by hand.  config/files.csv is that list, written down.

HOW A FILE GETS HERE

Some of those shares answer from the corporate machine directly and some
only answer from the server, so the route is a property of the FILE and
lives in its row:

    direct   robocopy straight from the UNC path.  Nothing else involved.
    server   WinRM tells the server to copy it into the staging folder,
             then the same robocopy pulls it from \\server\staging.

WinRM never carries the file itself.  It has no file transfer - everything
that claims to have one is base64 through the shell at a couple of MB/s -
and these files are hundreds of MB.  So it carries a Copy-Item command and
SMB carries the data.

EVERY FILE IS OVERWRITTEN, EVERY RUN.  They are regenerated daily, so there
is nothing to compare and no skip logic to get wrong.  That is what /IS is
doing in transfer.py.

    python vaccum.py                 fetch everything in files.csv
    python vaccum.py --only prices   fetch one row
    python vaccum.py --probe         transfer nothing, report what is
                                     reachable and whether each Route is right
    python vaccum.py --dry-run       print what would be fetched
    python vaccum.py --self-test
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import filelist
import staging
import transfer

HERE = Path(__file__).resolve().parent
DEFAULT_FILES = HERE / "config" / "files.csv"

#  STRICT: a name local_settings.py sets that is not here is an ERROR, not a
#  new setting.  A typo'd STAGING_UNK would otherwise sit there while the
#  run used a default that quietly fetched nothing.
KNOWN_SETTINGS = {
    "SERVER", "WINRM_USER", "WINRM_PASSWORD", "WINRM_TRANSPORT",
    "WINRM_PORT", "WINRM_USE_SSL", "STAGING_DIR", "STAGING_UNC", "DEST_DIR",
}
REQUIRED_IF_SERVER = ("SERVER", "WINRM_USER", "WINRM_PASSWORD",
                      "STAGING_DIR", "STAGING_UNC")


def say(msg=""):
    print(msg, flush=True)


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):,.1f} MB"


def settings():
    try:
        import local_settings
    except ImportError:
        raise SystemExit(
            "local_settings.py not found.  Copy local_settings.py.example "
            "and fill it in.")

    unknown = sorted(n for n in vars(local_settings)
                     if n.isupper() and n not in KNOWN_SETTINGS)
    if unknown:
        raise SystemExit(
            "local_settings.py sets names vaccum.py does not know: "
            + ", ".join(unknown)
            + "\nA setting that is never read is a setting that is not doing "
              "what you think.  Fix the spelling or delete the line.")
    return local_settings


def need(s, names):
    missing = [n for n in names
               if not str(getattr(s, n, "") or "").strip()
               or str(getattr(s, n, "")).strip() == "CHANGEME"]
    if missing:
        raise SystemExit(
            "these rows need Route=server, which needs local_settings.py to "
            "set: " + ", ".join(missing))


def connect(s):
    return staging.connect(
        host=s.SERVER, user=s.WINRM_USER, password=s.WINRM_PASSWORD,
        transport=getattr(s, "WINRM_TRANSPORT", "ntlm"),
        port=int(getattr(s, "WINRM_PORT", 5985)),
        use_ssl=bool(getattr(s, "WINRM_USE_SSL", False)))


def fetch(entries, s) -> int:
    dest = str(s.DEST_DIR).rstrip("\\")
    Path(dest).mkdir(parents=True, exist_ok=True)

    server_rows = [e for e in entries if e.route == filelist.SERVER]
    session = None

    if server_rows:
        need(s, REQUIRED_IF_SERVER)
        say(f"  connecting to {s.SERVER} "
            f"({getattr(s, 'WINRM_TRANSPORT', 'ntlm')})")
        try:
            session = connect(s)
        except staging.RemoteError as e:
            say(f"  FAILED: {e}")
            return 1

    results = []
    for e in entries:
        say(f"\n  {e.name}  ({e.route})")
        say(f"    from {e.source}")

        src_dir = e.directory
        if e.route == filelist.SERVER:
            #  Server-local copy: share -> the server's own disk.  Its size
            #  is worth printing because it is the first honest number about
            #  what is coming, and it arrives before the slow part starts.
            started = time.monotonic()
            try:
                size = staging.stage(session, e.source, s.STAGING_DIR)
            except staging.RemoteError as err:
                say(f"    FAILED staging: {err}")
                results.append(transfer.Result(e.name, False, -1, 0.0, 0,
                                               str(err)))
                continue
            say(f"    staged {mb(size)} on the server in "
                f"{time.monotonic() - started:.1f}s")
            src_dir = str(s.STAGING_UNC).rstrip("\\")

        r = transfer.copy(e.name, src_dir, dest, e.filename)
        results.append(r)
        if r.ok:
            say(f"    got {mb(r.size)} in {r.seconds:.1f}s "
                f"({r.rate_mb_s:.1f} MB/s)")
        else:
            say(f"    FAILED: {r.detail}")

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]

    say(f"\n  {len(good)}/{len(results)} files, "
        f"{mb(sum(r.size for r in good))} into {dest}")
    for r in bad:
        say(f"  FAILED  {r.name}: {r.detail}")
    return 1 if bad else 0


def reachable_here(path: str) -> bool:
    """Can this machine see that path?  A dead UNC host blocks for about
    twenty seconds before it answers, which is why --probe warns first."""
    try:
        return os.path.exists(path)
    except OSError:
        return False


def probe(entries, s) -> int:
    say("  --probe transfers nothing.\n"
        "  An unreachable UNC host takes ~20s to give up, so a wrong path "
        "is slow to report.\n")

    dest = str(getattr(s, "DEST_DIR", "") or "").rstrip("\\")
    say(f"  destination  {dest or 'NOT SET'}"
        + ("" if dest and Path(dest).is_dir() else "   (will be created)"))

    say("\n  what THIS machine can see")
    here = {}
    for e in entries:
        here[e.name] = reachable_here(e.source)
        say(f"    {'yes' if here[e.name] else 'no '}  {e.name:<16} "
            f"{e.source}")

    there = {}
    server_rows = [e for e in entries if e.route == filelist.SERVER]
    if not server_rows:
        say("\n  no Route=server rows, so the server was not contacted")
    else:
        need(s, REQUIRED_IF_SERVER)
        say(f"\n  what {s.SERVER} can see")
        try:
            session = connect(s)
            looks = staging.look(session, [e.source for e in server_rows])
        except staging.RemoteError as err:
            say(f"    FAILED: {err}")
            say("\n    WinRM itself did not answer.  Check the port is open"
                "\n    (Test-NetConnection " + str(s.SERVER) +
                " -Port 5985) and that"
                "\n    WINRM_USER is DOMAIN\\user.")
            return 1

        for e, lk in zip(server_rows, looks):
            there[e.name] = lk
            if lk.exists:
                say(f"    yes  {e.name:<16} {mb(lk.size)}")
            elif lk.denied:
                say(f"    DENIED  {e.name:<16} {lk.detail}")
            else:
                say(f"    no   {e.name:<16} {lk.detail}")

        st = staging.look(session, [s.STAGING_DIR])[0]
        say(f"\n  staging folder on the server  {s.STAGING_DIR}  "
            + ("exists" if st.exists else "will be created"))
        say(f"  the same folder from here     {s.STAGING_UNC}  "
            + ("readable" if reachable_here(str(s.STAGING_UNC))
               else "NOT READABLE - Route=server cannot work until it is"))

    say("\n  verdict")
    problems = 0
    for e in entries:
        lk = there.get(e.name)
        if e.route == filelist.DIRECT:
            if here[e.name]:
                say(f"    ok       {e.name:<16} direct, and it resolves")
            else:
                problems += 1
                say(f"    PROBLEM  {e.name:<16} Route=direct but this "
                    "machine cannot see it - try Route=server")
        else:
            if lk and lk.exists and here[e.name]:
                say(f"    ok       {e.name:<16} server works, and direct "
                    "would too - direct is faster, one hop instead of two")
            elif lk and lk.exists:
                say(f"    ok       {e.name:<16} server can read it")
            elif lk and lk.denied:
                problems += 1
                say(f"    PROBLEM  {e.name:<16} the server was reached but "
                    "denied - this is the double hop.")
                say(f"             Try WINRM_TRANSPORT = \"credssp\""
                    + (", or Route=direct, which resolves from here"
                       if here[e.name] else "."))
            else:
                problems += 1
                say(f"    PROBLEM  {e.name:<16} neither this machine nor "
                    "the server can see it - check the path")

    say(f"\n  {problems} problem(s)")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch the day's files")
    ap.add_argument("--probe", action="store_true",
                    help="report what is reachable, transfer nothing")
    ap.add_argument("--only", metavar="NAME", action="append",
                    help="just this row, repeatable")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--files", metavar="PATH", default=str(DEFAULT_FILES))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        entries = filelist.load(args.files)
    except filelist.ConfigError as e:
        say(f"  {args.files}: {e}")
        return 1

    if args.only:
        wanted = set(args.only)
        unknown = wanted - {e.name for e in entries}
        if unknown:
            say(f"  no such row: {', '.join(sorted(unknown))}")
            say(f"  files.csv has: "
                f"{', '.join(e.name for e in entries)}")
            return 1
        entries = [e for e in entries if e.name in wanted]

    say(f"\n  {len(entries)} file(s) from {args.files}\n")

    if args.dry_run:
        for e in entries:
            say(f"    {e.name:<16} {e.route:<7} {e.source}")
        return 0

    s = settings()
    return probe(entries, s) if args.probe else fetch(entries, s)


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("vaccum --self-test\n\nthe shipped config parses")
    try:
        entries = filelist.load(DEFAULT_FILES)
        check("config/files.csv loads", len(entries) > 0, True)
        check("every route is known",
              {e.route for e in entries} <= set(filelist.ROUTES), True)
    except filelist.ConfigError as e:
        check(f"config/files.csv loads ({e})", False, True)

    print("\nsizes read as sizes")
    check("bytes to MB", mb(1024 * 1024 * 3), "3.0 MB")

    print("\nthe settings whitelist")
    check("every required name is a known name",
          set(REQUIRED_IF_SERVER) <= KNOWN_SETTINGS, True)

    example = HERE / "local_settings.py.example"
    if example.exists():
        names = {ln.split("=")[0].strip()
                 for ln in example.read_text(encoding="utf-8").splitlines()
                 if "=" in ln and not ln.strip().startswith("#")}
        names = {n for n in names if n.isupper()}
        check("the example sets nothing STRICT would reject",
              sorted(names - KNOWN_SETTINGS), [])
        check("the example covers every known setting",
              sorted(KNOWN_SETTINGS - names), [])

    print("\nthe modules agree with each other")
    e = filelist.Entry("prices", r"\\host\share\sub\Prices.dat", "server")
    argv = transfer.command(e.directory, r"C:\dest", e.filename)
    check("a source directory reaches robocopy whole",
          argv[1], r"\\host\share\sub")
    check("and the filename separately", argv[3], "Prices.dat")

    root = filelist.Entry("p", r"\\host\share\P.csv", "direct")
    check("a file at the share root has no trailing backslash",
          transfer.command(root.directory, "d", root.filename)[1],
          r"\\host\share")

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
