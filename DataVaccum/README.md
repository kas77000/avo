# DataVaccum

Pulls the day's files off the shared drives onto this machine, so the run
that used to be an RDP session — log in, hunt through several shares, drag
the files back — is one command.

```
python vaccum.py                 fetch everything in config/files.csv
python vaccum.py --only prices   fetch one row
python vaccum.py --probe         transfer nothing; report what is reachable
python vaccum.py --dry-run       print what would be fetched
python vaccum.py --self-test
```

## First run on the corporate machine

1. `pip install pywinrm` — only needed if any row uses `Route=server`.
2. Copy `local_settings.py.example` to `local_settings.py` and fill it in.
   It is gitignored.
3. Fill in `config/files.csv` — one row per file.
4. **Run `--probe` before anything else.** It transfers nothing and tells
   you which `Route` each row should actually have. Guessing those is
   expected; the probe is how you correct them.
5. `python vaccum.py`.

## The two routes

Some of these shares answer from the corporate machine directly and some
only answer from the server, so the route is a property of the **file**, not
of the job, and lives in its row in `config/files.csv`:

| Route | What happens |
|---|---|
| `direct` | `robocopy` straight from the UNC path. One hop, nothing else involved. |
| `server` | WinRM tells the server to copy the file into the staging folder, then the same `robocopy` pulls it from `\\server\staging`. |

Prefer `direct` wherever it works — it is one hop instead of two, and it
needs no credentials in `local_settings.py`. `--probe` says so explicitly
when a `server` row would also work as `direct`.

## Why WinRM does not carry the files

WinRM has no file-transfer primitive. Everything that appears to have one —
pywinrm base64 loops, PowerShell's own `Copy-Item -FromSession` — encodes the
file and streams it through the shell's stdout in small chunks. That runs at
a couple of MB/s and starts hitting operation timeouts at the sizes involved
here.

So WinRM carries a `Copy-Item` command and nothing else, and SMB carries the
data at line speed. The staging folder exists purely to turn a share the
server can read into a path *this* machine can read.

## The double hop

This is the failure to expect on day one, and it does not announce itself.

You authenticate to the server. The server then has to authenticate to the
share on your behalf — and under NTLM or plain Kerberos it **cannot**. A
share the server can read perfectly well when you are sitting at it via RDP
comes back `Access is denied` from a WinRM session, and nothing in the
message explains why.

`--probe` reports a denial as `DENIED` rather than as a missing file, which
is the whole difference between a five-minute fix and an afternoon. The ways
out, in the order worth trying:

1. `WINRM_TRANSPORT = "credssp"` — delegates the credentials properly. Needs
   `pip install requests-credssp` here and `Enable-WSManCredSSP -Role Server`
   once on the server. Some estates disable CredSSP by policy, which is a
   real answer and not a misconfiguration — it exists because a compromised
   server gets your password.
2. `WINRM_TRANSPORT = "kerberos"` plus resource-based constrained delegation
   configured in AD. The right answer in a well-run domain, and it needs
   someone with the rights to set it up.
3. `Route=direct`, if the probe shows that share answers from here anyway.

## Everything is overwritten, every run

The files are regenerated daily, so there is nothing to compare and no skip
logic to get wrong. That is what `/IS /IT` are doing in `transfer.py`.

It matters more than it sounds. robocopy's default is to skip a file whose
size and timestamp match the destination — so a file rewritten with the same
size, or one whose timestamp did not move, would be silently skipped and you
would keep a stale copy that looks fresh. `/IS` is what stops that.

## Layout

| File | |
|---|---|
| `vaccum.py` | the CLI, and the run itself |
| `filelist.py` | `config/files.csv` — parsing and, mostly, refusing |
| `transfer.py` | the robocopy wrapper, and its exit-code bitmask |
| `staging.py` | the WinRM side: what the server can see, and staging |
| `config/files.csv` | the list of files. Edit in Excel. |

Each module runs its own `--self-test` with no network and no server.
