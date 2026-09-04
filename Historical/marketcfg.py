#!/usr/bin/env python3
"""Per-market configuration, as a CSV the desk can edit in Excel.

The same `config/markets.csv` TradingData uses, and deliberately the same
file shape so the desk maintains one layout rather than two.  This job reads
three of its columns - the Fidessa market, the Bloomberg composite and the
timezone label - and ignores `NoShortSell` and `RespectShortSellPrice`
entirely.

`TimeZone` is this job's own addition and ships BLANK on every row.  It is
the seventh header cell of the output CSV, naming the clock column one is
in - and what that clock is cannot be known until qatt_time_probe.py has
run.  Filling it in before then would be writing down a guess.

WHAT THE COMPOSITE IS FOR.  qatt is keyed on a sym built from the Bloomberg
ticker and the COMPOSITE exchange code: Toyota is `7203.JP`, not `7203.JT`.
The crosscode carries the PRIMARY code (`7203 JT`), so something has to
supply the composite.  equity_master does, authoritatively, and that is what
the job uses.  This file is the fallback for a name equity_master has no row
for - and the run reports how many names took it, because a fallback that
starts carrying real traffic is a fact worth seeing.

    python marketcfg.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Market:
    fidessa_market: str
    bbg_composite: str
    time_zone: str


def load(path):
    out = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("FidessaMarket") or "").strip()
            if not key:
                continue
            out[key] = Market(
                fidessa_market=key,
                bbg_composite=(r.get("BBGComposite") or "").strip(),
                time_zone=(r.get("TimeZone") or "").strip())
    return out


def composite(market: str, markets) -> str:
    """The composite for a Fidessa market, or "" if it is not configured.

    "" is not an error here.  The crosscode carries venues this file has
    never listed - JNX-MAIN and CHJ-MAIN among them - and the answer for
    those is that this file has no opinion, which the caller reports rather
    than guesses around."""
    m = markets.get((market or "").strip())
    return m.bbg_composite if m else ""


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    here = Path(__file__).resolve().parent
    M = load(here / "config" / "markets.csv")

    print("marketcfg --self-test\n\nthe shipped config")
    check("Tokyo composites to JP, which is what qatt is keyed on - not the "
          "JT the crosscode carries",
          composite("TYO-MAIN", M), "JP")
    check("Korea's two boards share one composite",
          (composite("KSC-MAIN", M), composite("KOE-MAIN", M)), ("KS", "KS"))
    check("every China board composites to CH",
          {composite(k, M) for k in
           ("SHA-MAIN", "SHH-MAIN", "SHZ-MAIN", "SSC-MAIN",
            "SZA-MAIN", "SZC-MAIN")},
          {"CH"})
    check("Australia's primary and composite are the same letters, which is "
          "why AU names hide this whole problem",
          composite("ASX-MAIN", M), "AU")

    print("\nmarkets this file does not list")
    check("a Japanese alternative venue is not in here, and that is not an "
          "error - it is no opinion",
          composite("JNX-MAIN", M), "")
    check("nor is an unknown market", composite("XXX-MAIN", M), "")
    check("nor a blank one", composite("", M), "")
    check("whitespace around a market name is not part of it",
          composite("  TYO-MAIN  ", M), "JP")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
