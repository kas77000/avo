#!/usr/bin/env python3
"""Failure and exclusion-report email.

Plain text, one message, no templates.  The XML MailConfigurationList the R
job used carried nine named templates; every one of them said "something
went wrong, here is what" and the run already knows how to say that.

A run with no recipients configured sends nothing and does not complain -
that is the normal state of a dry run on a developer's machine.

    python mailer.py --self-test
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage


def build(subject: str, body: str, sender: str, to) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    return msg


def send(subject: str, body: str, host: str, sender: str, to) -> None:
    if not to or not host or host == "CHANGEME":
        return
    with smtplib.SMTP(host) as s:
        s.send_message(build(subject, body, sender, to))


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

    print("mailer --self-test\n\nbuilding the message")
    m = build("subject", "body\n", "from@x", ["a@x", "b@x"])
    check("the subject", m["Subject"], "subject")
    check("the sender", m["From"], "from@x")
    check("recipients are joined", m["To"], "a@x, b@x")
    check("the body", m.get_content(), "body\n")

    print("\nsending is a no-op when there is nowhere to send")
    #  if any of these tried to open a socket the self-test would hang or
    #  raise; reaching the next line IS the assertion
    send("s", "b", "mail.example.com", "f@x", [])
    send("s", "b", "", "f@x", ["a@x"])
    send("s", "b", "CHANGEME", "f@x", ["a@x"])
    check("no recipients, no host, or an unedited placeholder host all "
          "return quietly", True, True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
