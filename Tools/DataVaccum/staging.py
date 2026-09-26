#!/usr/bin/env python3
r"""The server side: WinRM tells the server to copy a file to its staging
folder, and robocopy then pulls it from there.

WHY THE BYTES DO NOT COME THROUGH WINRM

WinRM has no file transfer.  Every "copy a file with WinRM" recipe - pywinrm
base64 loops, PowerShell's own Copy-Item -FromSession - encodes the file and
streams it through the shell's stdout in small chunks.  That runs at a couple
of MB/s and starts hitting operation timeouts on a file this size.  So WinRM
carries a Copy-Item command and nothing else, and robocopy over SMB carries
the data at line speed.

THE DOUBLE HOP, WHICH IS THE FAILURE TO EXPECT

You authenticate to the server.  The server then has to authenticate to the
share on your behalf, and with NTLM or plain Kerberos it CANNOT: your
credentials reached the server but may go no further.  So a share you can see
perfectly well yourself comes back "Access is denied" from a WinRM session,
and nothing about the message says why.

Three ways out, in the order worth trying:

  1. WINRM_TRANSPORT = "credssp".  CredSSP delegates the credentials the
     second hop needs.  It needs `pip install requests-credssp`, and
     `Enable-WSManCredSSP -Role Server` run once on the server.  Some
     estates disable CredSSP by policy, which is a real answer and not a
     misconfiguration - it exists because a compromised server gets your
     password.
  2. WINRM_TRANSPORT = "kerberos" plus resource-based constrained
     delegation configured for the server against the file server.  This is
     the right answer in a well-run domain and needs someone with AD rights.
  3. Route = direct in files.csv, if that share turns out to answer from
     your machine after all.

--probe distinguishes these.  It asks the server, in one round trip, whether
it can see each file, and reports a denial as a denial rather than as a
missing file.

    python staging.py --self-test
"""

from __future__ import annotations

from dataclasses import dataclass

#  The token the PowerShell snippets print, so a message that happens to
#  start with "OK" cannot be mistaken for a result line.
MARK = "<<DV>>"


class RemoteError(Exception):
    """The server could not be reached, or refused the command."""


@dataclass
class Look:
    """What the server sees at one path."""
    exists: bool
    size: int = 0
    denied: bool = False
    detail: str = ""


def ps_quote(s: str) -> str:
    r"""A PowerShell single-quoted literal.

    Single quotes, not double: inside double quotes PowerShell expands $ and
    backticks, and a Windows path is full of neither but a filename with a $
    in it - which share names have, C$ - would silently become empty.  The
    only escape a single-quoted string needs is a doubled single quote.
    """
    return "'" + s.replace("'", "''") + "'"


def connect(host: str, user: str, password: str, transport: str = "ntlm",
            port: int = 5985, use_ssl: bool = False, read_timeout: int = 120):
    """A pywinrm Session.  Imported here, not at module scope, so the rest of
    this file - and its self-test - work on a machine without pywinrm."""
    try:
        import winrm
    except ImportError:
        raise RemoteError(
            "pywinrm is not installed.  pip install pywinrm, and "
            "pip install requests-credssp too if WINRM_TRANSPORT is credssp.")

    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{port}/wsman"

    #  operation_timeout has to be under read_timeout or pywinrm raises
    #  before the server has finished answering.
    return winrm.Session(
        endpoint, auth=(user, password), transport=transport,
        read_timeout_sec=read_timeout,
        operation_timeout_sec=max(read_timeout - 20, 20),
        server_cert_validation="ignore")


def run_ps(session, script: str) -> str:
    """Run PowerShell on the server, return stdout.  Raises on failure."""
    try:
        r = session.run_ps(script)
    except Exception as e:                       # pywinrm raises broadly
        raise RemoteError(f"{type(e).__name__}: {e}")

    out = (r.std_out or b"").decode("utf-8", "replace").strip()
    err = (r.std_err or b"").decode("utf-8", "replace").strip()

    if r.status_code != 0:
        raise RemoteError(err or out or f"exit {r.status_code}")
    return out


def _parse(out: str) -> list[str]:
    """Pull the marked result lines out, ignoring anything else the profile
    or a module load may have printed."""
    return [ln[len(MARK):].strip()
            for ln in out.splitlines() if ln.strip().startswith(MARK)]


def _look_script(paths) -> str:
    """One round trip that reports on every path, rather than one call each.

    ErrorAction Stop turns an access denial into a catchable exception -
    without it Test-Path swallows the denial and returns False, which reads
    as 'the file is not there' and sends you looking for the wrong bug.
    """
    lines = ["$ErrorActionPreference = 'Continue'"]
    for p in paths:
        q = ps_quote(p)
        lines.append(
            "try {"
            f"  $i = Get-Item -LiteralPath {q} -Force -ErrorAction Stop;"
            f"  Write-Output ('{MARK} OK ' + $i.Length)"
            "} catch {"
            "  $m = $_.Exception.Message;"
            "  if ($_.Exception -is [System.UnauthorizedAccessException] -or"
            "      $m -match 'denied') {"
            f"    Write-Output ('{MARK} DENIED ' + $m)"
            "  } elseif ($m -match 'does not exist|cannot find|not found') {"
            f"    Write-Output ('{MARK} MISSING ' + $m)"
            "  } else {"
            f"    Write-Output ('{MARK} ERR ' + $m)"
            "  }"
            "}")
    return "\n".join(lines)


def parse_looks(out: str, count: int) -> list[Look]:
    got = _parse(out)
    if len(got) != count:
        raise RemoteError(
            f"asked about {count} paths, got {len(got)} answers.  The raw "
            f"output was: {out[:400]!r}")

    looks = []
    for line in got:
        verb, _, rest = line.partition(" ")
        if verb == "OK":
            looks.append(Look(True, size=int(rest.strip() or 0)))
        elif verb == "DENIED":
            looks.append(Look(False, denied=True, detail=rest.strip()))
        else:
            looks.append(Look(False, detail=rest.strip()))
    return looks


def look(session, paths) -> list[Look]:
    """Ask the server what it sees at each path.  One round trip."""
    paths = list(paths)
    if not paths:
        return []
    return parse_looks(run_ps(session, _look_script(paths)), len(paths))


def _stage_script(source: str, staging_dir: str) -> str:
    src, dst = ps_quote(source), ps_quote(staging_dir)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        f"  if (-not (Test-Path -LiteralPath {dst})) {{\n"
        f"    New-Item -ItemType Directory -Path {dst} -Force | Out-Null }}\n"
        #  -Force so a leftover read-only copy from a previous run does not
        #  block today's.  The brief is always overwrite.
        f"  Copy-Item -LiteralPath {src} -Destination {dst} -Force\n"
        f"  $leaf = Split-Path -Path {src} -Leaf\n"
        f"  $out  = Join-Path {dst} $leaf\n"
        f"  Write-Output ('{MARK} OK ' + (Get-Item -LiteralPath $out).Length)\n"
        "} catch {\n"
        f"  Write-Output ('{MARK} ERR ' + $_.Exception.Message)\n"
        "}")


def stage(session, source: str, staging_dir: str) -> int:
    """Copy one file share -> the server's staging folder.  Returns its size.

    This is server-local work: both ends are paths the SERVER resolves, so
    the data never crosses the WinRM channel.
    """
    got = _parse(run_ps(session, _stage_script(source, staging_dir)))
    if not got:
        raise RemoteError(f"the server said nothing about {source}")

    verb, _, rest = got[0].partition(" ")
    if verb != "OK":
        msg = rest.strip()
        if "denied" in msg.lower():
            raise RemoteError(
                f"{msg}\n      The server was reached, but it could not read "
                f"the share.\n      This is the double hop - see the note at "
                f"the top of staging.py.\n      Try WINRM_TRANSPORT = "
                f'"credssp", or set this row to Route=direct.')
        raise RemoteError(msg)
    return int(rest.strip() or 0)


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("staging --self-test\n\nPowerShell quoting")
    check("a plain path", ps_quote(r"\\a\b\c.csv"), r"'\\a\b\c.csv'")
    check("a backslash is NOT an escape in PowerShell",
          ps_quote(r"C:\temp\x"), r"'C:\temp\x'")
    check("an apostrophe doubles",
          ps_quote(r"\\a\Bob's Files\x.csv"), r"'\\a\Bob''s Files\x.csv'")
    check("a dollar is inert in single quotes",
          ps_quote(r"\\host\C$\x"), r"'\\host\C$\x'")

    print("\nreading the server's answers")
    out = (f"{MARK} OK 12345\n"
           f"{MARK} DENIED Access to the path is denied.\n"
           f"{MARK} MISSING Cannot find path\n")
    looks = parse_looks(out, 3)
    check("three answers", len(looks), 3)
    check("the first exists", (looks[0].exists, looks[0].size), (True, 12345))
    check("the second is a denial, not a missing file",
          (looks[1].exists, looks[1].denied), (False, True))
    check("the third is missing, not a denial",
          (looks[2].exists, looks[2].denied), (False, False))

    print("\nnoise around the answers is ignored")
    noisy = f"loading profile...\n{MARK} OK 7\nWARNING: something\n"
    check("only the marked line counts", parse_looks(noisy, 1)[0].size, 7)

    print("\nan answer count that does not match is an error, not a guess")
    try:
        parse_looks(f"{MARK} OK 1", 2)
        check("mismatch raises", False, True)
    except RemoteError:
        print("  ok    mismatch raises")

    print("\nthe generated scripts")
    s = _look_script([r"\\a\b\c.csv", r"\\a\b\d.csv"])
    check("one try block per path", s.count("try {"), 2)
    check("denials are caught separately", "DENIED" in s, True)

    s = _stage_script(r"\\a\b\c.csv", r"D:\staging")
    check("it forces the overwrite", "-Force" in s, True)
    check("it creates the staging folder", "New-Item" in s, True)
    check("the source is quoted for the server", r"'\\a\b\c.csv'" in s, True)
    check("so is the staging folder", r"'D:\staging'" in s, True)
    #  LiteralPath, not Path, everywhere it is available: a share or file
    #  with [ or ] in its name is a wildcard to -Path and silently matches
    #  nothing.
    check("the three reads use LiteralPath", s.count("-LiteralPath"), 3)

    print("\nno pywinrm is a clear message, not an ImportError")
    try:
        import winrm                                            # noqa: F401
        print("  skip  pywinrm is installed on this machine")
    except ImportError:
        try:
            connect("h", "u", "p")
            check("connect raises", False, True)
        except RemoteError as e:
            check("it names the fix", "pip install pywinrm" in str(e), True)

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
