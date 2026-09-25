"""Can this machine reach these kdb processes?

Standard library only, so it runs on a box where nothing is installed.

    python kdb_ping.py host1:5001 host2:5002
    python kdb_ping.py -f targets.txt            # one host:port per line, # comments
    python kdb_ping.py -u user:pass host1:5001

For each target it opens a TCP socket, then does the kdb handshake
("user:pass" + capability byte + NUL; kdb answers with one byte).
  OK       - port open and kdb accepted the login
  REJECTED - port open, but kdb closed the connection (bad credentials / .z.pw)
  NOT KDB  - port open, but nothing kdb-like answered
  FAIL     - could not connect (refused, timeout, DNS, firewall)
Exit code is 0 only if every target is OK.
"""
import argparse
import getpass
import socket
import sys


def ping(host, port, creds, timeout):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        return "FAIL", str(e) or type(e).__name__
    try:
        sock.settimeout(timeout)
        sock.sendall(creds.encode() + b"\x03\x00")
        reply = sock.recv(1)
    except socket.timeout:
        return "NOT KDB", "port open, no handshake reply"
    except OSError as e:
        return "REJECTED", str(e)
    finally:
        sock.close()
    if not reply:
        return "REJECTED", "connection closed during handshake"
    return "OK", "capability %d" % reply[0]


def parse_target(text):
    host, _, port = text.strip().rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("expected host:port, got %r" % text)
    return host, int(port)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("targets", nargs="*", help="host:port")
    ap.add_argument("-f", "--file", help="file with one host:port per line")
    ap.add_argument("-u", "--user", default=getpass.getuser(),
                    help="user or user:pass for the handshake (default: login name)")
    ap.add_argument("-t", "--timeout", type=float, default=3.0, help="seconds (default 3)")
    args = ap.parse_args()

    lines = list(args.targets)
    if args.file:
        with open(args.file) as fh:
            lines += [l.split("#")[0] for l in fh]
    targets = [parse_target(l) for l in lines if l.strip()]
    if not targets:
        ap.error("no targets given")

    print("from %s as %s" % (socket.gethostname(), args.user.split(":")[0]))
    all_ok = True
    for host, port in targets:
        status, detail = ping(host, port, args.user, args.timeout)
        all_ok &= status == "OK"
        print("%-9s %s:%d  %s" % (status, host, port, detail))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
