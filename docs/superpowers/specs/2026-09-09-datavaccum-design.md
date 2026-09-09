# DataVaccum — design

2026-09-09

## The job

Replace a manual RDP session: log in to a Windows server, hunt through
several shared drives, drag the day's files back by hand. The list of files
is known and stable; the hunting is not.

## Constraints, as established

| | |
|---|---|
| Where it runs | the corporate machine, which is not this development machine — so it is written blind and diagnoses itself on first run over there |
| Transport | WinRM. pywinrm is installed; RDP is the only channel confirmed by hand |
| Language | Python 3, stdlib + pywinrm, matching the rest of Nova |
| Source paths | fixed literal UNC paths, no date tokens, no wildcards |
| Share reachability | **mixed** — some shares answer from the corporate machine directly, some only from the server |
| Size | hundreds of MB per file, a few files per run |
| Destination | one flat local folder |
| Staleness | files are regenerated daily; always overwrite, no skip logic |
| Credentials | username and password from a gitignored `local_settings.py` |

## The decision that shapes everything

**WinRM does not carry the file bytes.**

WinRM has no file-transfer primitive. Every apparent one — pywinrm base64
loops, PowerShell's `Copy-Item -FromSession` — encodes the file and streams
it through the shell's stdout in small chunks, at a couple of MB/s, with
operation timeouts at these sizes. Rejected on the size constraint alone.

So WinRM carries a `Copy-Item` command, and SMB carries the data via
`robocopy`. The staging folder on the server exists purely to convert a path
the *server* can read into a path *this machine* can read.

## Route is a property of the file

Because reachability is mixed, the route lives in the row rather than in the
job:

- `direct` — robocopy straight from the UNC path. One hop, no credentials.
- `server` — WinRM stages share → the server's folder, then the same
  robocopy pulls from `\\server\staging`.

One copy path either way; the route only decides which directory robocopy is
pointed at. `--probe` resolves every path from both ends and reports rows
whose route is wrong, including `server` rows that would work as `direct`.

## Known failure modes, and how each is surfaced

1. **The double hop.** A WinRM session under NTLM cannot pass credentials to
   a file share, so a share the server reads fine over RDP returns "Access
   is denied". Surfaced as `DENIED` (distinct from missing) with the CredSSP
   / Kerberos-delegation / `Route=direct` remedies named.
2. **robocopy's exit codes are a bitmask.** 1 means success; ≥8 is the first
   failure. `if returncode:` and `check=True` both misread a good copy.
3. **robocopy returns 0 for a file that does not exist** — "copied nothing"
   is a success. Guarded by asserting the file actually landed; otherwise a
   run reports success while leaving yesterday's copy in place.
4. **Skipping an unchanged-looking file.** robocopy skips on matching size
   and timestamp, so a same-size rewrite would be silently skipped. `/IS /IT`
   force the copy. Verified against a same-size, same-timestamp,
   different-content file.
5. **A trailing backslash from `PureWindowsPath.parent`** at a share root
   escapes the closing quote of a robocopy argument and shifts the command
   line. Stripped.
6. **Drive letters in config.** Per-user and per-machine, and usually absent
   inside a WinRM session. Rejected at parse time.

## Modules

| | |
|---|---|
| `vaccum.py` | CLI, the run, `--probe` |
| `filelist.py` | `config/files.csv` — parsing and refusing |
| `transfer.py` | robocopy wrapper and its exit-code bitmask |
| `staging.py` | WinRM: what the server can see, and staging |

Each has a `--self-test` needing no network and no server.

## Deliberately not built

Date tokens, wildcards, mirrored directory structure, skip-if-unchanged,
resumable partial transfers, parallel copies, a scheduler. None are in the
job as described, and each would add a way for the run to appear to succeed
while fetching the wrong thing.
