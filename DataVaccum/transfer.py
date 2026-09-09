#!/usr/bin/env python3
r"""The copy itself, which is robocopy and not shutil.

shutil.copyfile over a UNC path on a corporate link gives up the moment the
link blinks, and hundreds of MB is long enough for that to happen.  robocopy
retries, and it is already on every Windows box.

ROBOCOPY EXIT CODES ARE NOT UNIX EXIT CODES.  They are a bitmask:

    0   nothing needed copying
    1   files were copied            <- the normal success for us
    2   extra files in the destination
    4   MISMATCHED files or dirs
    8   some files did NOT copy      <- the first genuine failure bit
    16  a fatal error

So `if returncode:` reads a perfectly good copy as a failure, and
`check=True` on subprocess.run raises on it.  Anything under 8 is fine.

WHY THESE FLAGS

    /IS /IT   copy the file even when it looks identical.  These files are
              regenerated daily and the brief is to always overwrite, so a
              same-size same-timestamp file still gets pulled.  Without /IS
              robocopy skips it and the run silently does nothing.
    /J        unbuffered I/O.  This is the flag that matters at hundreds of
              MB; it bypasses the cache manager and is markedly faster on
              large files over SMB.
    /R:2 /W:5 two retries, five seconds apart.  robocopy's default is a
              MILLION retries thirty seconds apart, which is how an
              unattended job appears to hang forever.
    /NP       no per-file percentage.  It emits a carriage return per
              update and turns a log file into megabytes of noise.

There is deliberately no /MIR and no /PURGE.  Both delete things in the
destination that are not in the source, and the destination here is a folder
you may keep other things in.

    python transfer.py --self-test
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

#  8 is the first bit that means "did not copy".
ROBOCOPY_FIRST_FAILURE = 8

FLAGS = ["/IS", "/IT", "/J", "/R:2", "/W:5", "/NP", "/NDL", "/NJH", "/NJS"]


@dataclass
class Result:
    name: str
    ok: bool
    code: int
    seconds: float
    size: int
    detail: str = ""

    @property
    def rate_mb_s(self) -> float:
        if self.seconds <= 0 or not self.size:
            return 0.0
        return self.size / self.seconds / (1024 * 1024)


def succeeded(code: int) -> bool:
    return code < ROBOCOPY_FIRST_FAILURE


def command(src_dir: str, dest_dir: str, filename: str) -> list[str]:
    """The argv.  Split out so the self-test can assert on it without
    touching a network."""
    return ["robocopy", src_dir, dest_dir, filename, *FLAGS]


def copy(name: str, src_dir: str, dest_dir: str, filename: str,
         timeout: int = 3600) -> Result:
    argv = command(src_dir, dest_dir, filename)
    started = time.monotonic()

    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return Result(name, False, -1, 0.0, 0,
                      "robocopy not found - this has to run on Windows")
    except subprocess.TimeoutExpired:
        return Result(name, False, -1, time.monotonic() - started, 0,
                      f"gave up after {timeout}s")

    elapsed = time.monotonic() - started
    landed = Path(dest_dir) / filename
    size = landed.stat().st_size if landed.exists() else 0

    if not succeeded(p.returncode):
        #  robocopy puts the real reason on stdout, not stderr.
        detail = (p.stdout or p.stderr or "").strip().splitlines()
        return Result(name, False, p.returncode, elapsed, size,
                      detail[-1].strip() if detail else "no output")

    #  Exit code says success but nothing is there: the source file did not
    #  exist under that name.  robocopy calls a copy of zero files a success,
    #  so without this the run reports OK and you get yesterday's file.
    if not landed.exists():
        return Result(name, False, p.returncode, elapsed, 0,
                      f"robocopy reported {p.returncode} but {filename} is "
                      "not in the destination - check the source filename's "
                      "spelling")

    return Result(name, True, p.returncode, elapsed, size)


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("transfer --self-test\n\nthe exit code bitmask")
    check("0, nothing to do, is success", succeeded(0), True)
    check("1, files copied, is success", succeeded(1), True)
    check("3, copied plus extras, is success", succeeded(3), True)
    check("7 is still success", succeeded(7), True)
    check("8 is the first failure", succeeded(8), False)
    check("16, fatal, is failure", succeeded(16), False)

    print("\nthe command line")
    argv = command(r"\\host\share", r"C:\dest", "Positions.csv")
    check("source, destination, then the one filename",
          argv[:4], ["robocopy", r"\\host\share", r"C:\dest",
                     "Positions.csv"])
    check("/IS is present, or an unchanged file is skipped",
          "/IS" in argv, True)
    check("/J is present, for the large ones", "/J" in argv, True)
    check("retries are bounded", "/R:2" in argv, True)
    check("nothing that deletes",
          [f for f in argv if f.upper() in ("/MIR", "/PURGE")], [])

    print("\nrate is safe when nothing was copied")
    check("no divide by zero", Result("x", True, 1, 0.0, 0).rate_mb_s, 0.0)

    print("\na real local copy")
    import shutil
    if not shutil.which("robocopy"):
        print("  skip  robocopy is not on this machine (not Windows)")
    else:
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src", Path(d) / "dst"
            src.mkdir()
            dst.mkdir()
            (src / "Positions.csv").write_bytes(b"a,b\n1,2\n" * 1000)

            r = copy("positions", str(src), str(dst), "Positions.csv")
            check("it copied", r.ok, True)
            check("the size is the source's", r.size, 8000)

            #  The point of /IS: the same file again still copies.
            r2 = copy("positions", str(src), str(dst), "Positions.csv")
            check("an identical file copies again, not skipped",
                  r2.ok and r2.code >= 1, True)

            missing = copy("nope", str(src), str(dst), "NotThere.csv")
            check("a missing source is a failure, not a silent pass",
                  missing.ok, False)

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
