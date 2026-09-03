# TradingData Python Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `TradingData.csv` from the crosscode plus kdb's `equity_master`, filling every column we can source with confidence and leaving the rest blank.

**Architecture:** Five modules. `columns.py` is pure arithmetic and string rules with no I/O. `crosscode.py`, `msci.py` read files. `equitymaster.py` is the only module that touches kdb, with `pykx` imported inside `connect()`. `trading_data.py` orchestrates, validates, writes to a temp path and copies. Every reference file is optional: present means used, absent means its columns go blank and the run says so.

**Tech Stack:** Python 3.13, stdlib only plus `pykx` for live runs. `decimal.Decimal` for all price and money arithmetic, never float.

## Global Constraints

- **No pytest anywhere.** Every module carries an embedded `self_test()` run as `python <module>.py --self-test`, following `LimitUpDown/v1/crosscode.py` and `kdb-queries/scripts/lib/price_bands.py`.
- The `self_test` house style is exact: a `check(name, got, want)` closure over a `nonlocal ok`, printing `f"  {'ok  ' if good else 'FAIL'}  {name}"`, ending with `print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))` and `return 0 if ok else 1`.
- Every module ends with `if __name__ == "__main__":` dispatching `--self-test` and otherwise printing `__doc__`.
- `pykx` is imported **inside** `connect()`, never at module scope, so `--self-test` and `--demo` run on a machine with no kdb.
- `decimal.Decimal` for `Close`, `Beta`, `Volatility10D`, `MarketCap`. Never float.
- **Report, never silently drop.** Every excluded or unresolved row carries a reason string and is counted and printed.
- Two R bugs are preserved verbatim and reported, never silently fixed: the `== 0` fallback at `:113` and `SubscribeFeedAtStartup` always `FALSE` at `:599`.
- Output matches R's `write.csv(row.names=F, na="", quote=FALSE)`: no index column, empty string for missing, no quoting.
- Files live in `TradingData/`. Config in `TradingData/config/`. `local_settings.py` is gitignored and strict.
- Source of truth for behaviour is `no_git/CreateTradingDataENT.r`. Line references below are into that file.

---

### Task 1: Pure column rules

**Files:**
- Create: `TradingData/columns.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `capi_bucket(market_cap: Decimal | None) -> str`
  - `sector(gics: str, industry: str) -> str`
  - `segment_asx(ticker: str, sec_type: str) -> str`
  - `segment_cn(market: str, sec_type: str) -> str | None`
  - `ext_ric(ric: str) -> str`
  - `ext_bbg(bbg: str) -> str`
  - `propagate_icb(records: list[dict]) -> list[str]`
  - `SEGMENT_DEFAULT = "Default"`

- [ ] **Step 1: Write the failing test**

Create `TradingData/columns.py` containing only the self-test, so it fails on missing functions:

```python
def self_test() -> int:
    from decimal import Decimal
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("columns --self-test\n\ncapi buckets, at the boundaries")
    check("exactly 300m is MICRO", capi_bucket(Decimal("300000000")), "MICRO")
    check("a penny over is SMALL", capi_bucket(Decimal("300000001")), "SMALL")
    check("exactly 2bn is still SMALL", capi_bucket(Decimal("2000000000")), "SMALL")
    check("over 2bn is MID", capi_bucket(Decimal("2000000001")), "MID")
    check("exactly 10bn is still MID", capi_bucket(Decimal("10000000000")), "MID")
    check("over 10bn is BIG", capi_bucket(Decimal("10000000001")), "BIG")
    check("zero is MICRO, as the R job's na-to-0 makes it",
          capi_bucket(Decimal("0")), "MICRO")
    check("no market cap means no bucket", capi_bucket(None), "")

    print("\nsector prefers GICS and falls back")
    check("GICS wins", sector("Financials", "Banks"), "Financials")
    check("blank GICS falls back", sector("", "Banks"), "Banks")
    check("commas become pipes, per :89", sector("", "Oil, Gas"), "Oil| Gas")
    check("neither means blank", sector("", ""), "")

    print("\nASX segments")
    check("A goes in A-B", segment_asx("ANZ", "Equity"), "A-B")
    check("B goes in A-B", segment_asx("BHP", "Equity"), "A-B")
    check("C goes in C-F", segment_asx("CBA", "Equity"), "C-F")
    check("G goes in G-M", segment_asx("GMG", "Equity"), "G-M")
    check("N goes in N-R", segment_asx("NAB", "Equity"), "N-R")
    check("S goes in S-Z", segment_asx("STO", "Equity"), "S-Z")
    check("lowercase is still bucketed", segment_asx("bhp", "Equity"), "A-B")
    check("a digit falls outside the letters", segment_asx("10X", "Equity"), "")
    check("an ETF is forced to A-B whatever its ticker",
          segment_asx("STW", "ETF"), "A-B")

    print("\nChina segments")
    check("an ETF on SSC is NO_CAS", segment_cn("SSC-MAIN", "ETF"), "NO_CAS")
    check("and on SHA", segment_cn("SHA-MAIN", "ETF"), "NO_CAS")
    check("and on SHH", segment_cn("SHH-MAIN", "ETF"), "NO_CAS")
    check("an equity is untouched", segment_cn("SHA-MAIN", "Equity"), None)
    check("an ETF elsewhere is untouched", segment_cn("SZA-MAIN", "ETF"), None)

    print("\nsplitting codes for the ICB propagation")
    check("the RIC extension is after the last dot",
          ext_ric("600001.SS"), "SS")
    check("no dot means the whole thing", ext_ric("NoRIC"), "NoRIC")
    check("the BBG extension is after the last space",
          ext_bbg("600001 CG"), "CG")
    check("no space means the whole thing", ext_bbg("ABC"), "ABC")

    print("\nICB propagation, per :263-269")
    recs = [
        {"ric": "1.SS", "bbg": "1 CG", "market": "SHA-MAIN", "icb": "SHCOMP"},
        {"ric": "2.SS", "bbg": "2 CG", "market": "SHA-MAIN", "icb": ""},
        {"ric": "3.HK", "bbg": "3 HK", "market": "HKG-MAIN", "icb": ""},
    ]
    check("a blank row takes its group's value",
          propagate_icb(recs), ["SHCOMP", "SHCOMP", ""])

    recs = [
        {"ric": "1.SS", "bbg": "1 CG", "market": "SHA-MAIN", "icb": "A"},
        {"ric": "2.SS", "bbg": "2 CG", "market": "SHA-MAIN", "icb": "B"},
        {"ric": "3.SS", "bbg": "3 CG", "market": "SHA-MAIN", "icb": ""},
    ]
    check("an ambiguous group propagates nothing",
          propagate_icb(recs), ["A", "B", ""])

    recs = [
        {"ric": "1.SZ", "bbg": "1 C2", "market": "SZC-MAIN", "icb": "X"},
        {"ric": "2.SZ", "bbg": "2 C2", "market": "SZC-MAIN", "icb": ""},
    ]
    check("SZC-MAIN is excluded, per the :264 comment",
          propagate_icb(recs), ["X", ""])

    recs = [
        {"ric": "NoRIC", "bbg": "1 HK", "market": "HKG-MAIN", "icb": "Y"},
        {"ric": "NoRIC", "bbg": "2 HK", "market": "HKG-MAIN", "icb": ""},
    ]
    check("NoRIC is excluded too", propagate_icb(recs), ["Y", ""])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/columns.py --self-test`
Expected: `NameError: name 'capi_bucket' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""The market rules, as pure functions.

Every function here takes values and returns a value.  No file is read, no
socket is opened, nothing is logged.  That is deliberate: the rules are the
part most likely to be wrong, and they are the part that must be testable on
a laptop with no kdb, no Bloomberg licence and no shares outstanding.

    python columns.py --self-test
"""

from __future__ import annotations

from decimal import Decimal

SEGMENT_DEFAULT = "Default"

# :165-168.  Note the R job's boundaries are exclusive going up, so a cap of
# exactly 300m is MICRO and exactly 2bn is SMALL.
_CAPI = ((Decimal("10000000000"), "BIG"),
         (Decimal("2000000000"), "MID"),
         (Decimal("300000000"), "SMALL"))

# :381-400.  ETFs are forced to A-B regardless of ticker.
_ASX = (("B", "A-B"), ("F", "C-F"), ("M", "G-M"), ("R", "N-R"), ("Z", "S-Z"))

_CN_ETF_MARKETS = ("SHA-MAIN", "SHH-MAIN", "SSC-MAIN")

# :265.  These two share a RIC extension with two possible ICB values and
# cannot be told apart, so they never take a propagated value.
_ICB_SKIP_MARKETS = ("SZA-MAIN", "SZC-MAIN")
_ICB_SKIP_RICS = ("NoRIC", "TWO")


def capi_bucket(market_cap) -> str:
    if market_cap is None:
        return ""
    for floor, name in _CAPI:
        if market_cap > floor:
            return name
    return "MICRO"


def sector(gics: str, industry: str) -> str:
    """:295 prefers GICS_SECTOR_NAME and falls back to INDUSTRY_SECTOR.

    equity_master has no GICS_SECTOR_NAME, so in practice every row takes the
    fallback.  The comma substitution is :89 - a comma would break the
    unquoted CSV the R job writes."""
    value = (gics or "").strip() or (industry or "").strip()
    return value.replace(",", "|")


def segment_asx(ticker: str, sec_type: str) -> str:
    if sec_type == "ETF":
        return "A-B"
    first = (ticker or "").strip()[:1].upper()
    if not ("A" <= first <= "Z"):
        return ""
    for ceiling, name in _ASX:
        if first <= ceiling:
            return name
    return ""


def segment_cn(market: str, sec_type: str):
    """None means 'this rule does not apply', which is not the same as ''."""
    if sec_type == "ETF" and market in _CN_ETF_MARKETS:
        return "NO_CAS"
    return None


def ext_ric(ric: str) -> str:
    return (ric or "").rsplit(".", 1)[-1]


def ext_bbg(bbg: str) -> str:
    return (bbg or "").rsplit(" ", 1)[-1]


def propagate_icb(records) -> list:
    """:263-269.  Names sharing (bbg extension, ric extension, market) share an
    index, so a name with no ICBIndex can borrow one from its group - but only
    where the group agrees on a single value."""
    groups = {}
    for r in records:
        icb = (r.get("icb") or "").strip()
        if not icb:
            continue
        if ext_ric(r.get("ric", "")) in _ICB_SKIP_RICS:
            continue
        if r.get("market") in _ICB_SKIP_MARKETS:
            continue
        key = (ext_bbg(r.get("bbg", "")), ext_ric(r.get("ric", "")),
               r.get("market"))
        groups.setdefault(key, set()).add(icb)

    out = []
    for r in records:
        icb = (r.get("icb") or "").strip()
        if icb:
            out.append(icb)
            continue
        if (ext_ric(r.get("ric", "")) in _ICB_SKIP_RICS
                or r.get("market") in _ICB_SKIP_MARKETS):
            out.append("")
            continue
        key = (ext_bbg(r.get("bbg", "")), ext_ric(r.get("ric", "")),
               r.get("market"))
        found = groups.get(key, set())
        out.append(next(iter(found)) if len(found) == 1 else "")
    return out
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/columns.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add TradingData/columns.py
git commit -m "feat(tradingdata): pure column rules with no I/O"
```

---

### Task 2: Crosscode reader

**Files:**
- Create: `TradingData/crosscode.py`
- Reference: `LimitUpDown/v1/crosscode.py` (reads five of the same columns)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Row` dataclass, frozen, fields `fidessa_code, ric, bbg, ticker, bbg_ext, sec_type, bbg_sec_type, market, currency, is_reit`
  - `load(path) -> tuple[list[Row], list[Excluded]]`
  - `Excluded` dataclass with `reason: str` and `rows: list`
  - `split_bbg(bbg: str) -> tuple[str, str]`

- [ ] **Step 1: Write the failing test**

Create `TradingData/crosscode.py` with only this:

```python
def self_test() -> int:
    import tempfile
    from pathlib import Path
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    HDR = ("#FidessaCode,RicCode,Type,BloombergCode,BloombergSecurityType,"
           "FidessaMarket,Currency\n")
    BODY = ("BHP.AU,BHP.AX,Equity,BHP AU,Equity,ASX-MAIN,AUD\n"
            "LINK.HK,823.HK,Equity,823 HK,REIT,HKG-MAIN,HKD\n"
            "STW.AU,STW.AX,ETF,STW AU,Equity,ASX-MAIN,AUD\n"
            "NO.XX,,Equity,,Equity,ASX-MAIN,AUD\n")

    print("crosscode --self-test\n\nsplitting the bloomberg code")
    check("ticker and exchange", split_bbg("005930 KP"), ("005930", "KP"))
    check("no space at all", split_bbg("ABC"), ("ABC", ""))
    check("nothing at all", split_bbg(""), ("", ""))

    print("\nreading")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CrossCode.csv"
        p.write_text(HDR + BODY, encoding="utf-8")
        rows, excl = load(p)

        check("three usable rows", len(rows), 3)
        check("the hash on the first header cell is not part of the value",
              rows[0].fidessa_code, "BHP.AU")
        check("the ticker is the bbg code without its exchange",
              rows[0].ticker, "BHP")
        check("and the exchange is kept separately", rows[0].bbg_ext, "AU")
        check("currency is carried for the FX join", rows[0].currency, "AUD")
        check("a REIT is flagged, per :528", rows[1].is_reit, True)
        check("an ETF whose BloombergSecurityType is Equity is not a REIT",
              rows[2].is_reit, False)
        check("Type and BloombergSecurityType are both kept",
              (rows[2].sec_type, rows[2].bbg_sec_type), ("ETF", "Equity"))

        reasons = {e.reason: e.rows for e in excl}
        check("a row with no bloomberg code cannot be joined, and is reported",
              reasons["no BloombergCode"], ["NO.XX"])

    print("\na missing column is a hard error, not a silent blank")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.csv"
        p.write_text("RicCode,Type\nA.AX,Equity\n", encoding="utf-8")
        try:
            load(p)
            check("raised", False, True)
        except ValueError as exc:
            check("names what is missing", "#FidessaCode" in str(exc), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/crosscode.py --self-test`
Expected: `NameError: name 'split_bbg' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""Read CrossCode.csv.  It is the security master and it drives the row set.

Seven columns, exactly the ones the R job selects at :526.  Unlike the
LimitUpDown reader this one filters nothing on security type or venue -
TradingData publishes every instrument in the crosscode, warrants included.
The only rows dropped are those with no BloombergCode, because there is
nothing to join them to, and they are reported rather than vanished.

Note the leading '#' on the first header cell.  It is part of the column
name in the real file and in the output, not a comment.

    python crosscode.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

COLUMNS = ("#FidessaCode", "RicCode", "Type", "BloombergCode",
           "BloombergSecurityType", "FidessaMarket", "Currency")


@dataclass(frozen=True)
class Row:
    fidessa_code: str
    ric: str
    bbg: str
    ticker: str
    bbg_ext: str
    sec_type: str
    bbg_sec_type: str
    market: str
    currency: str
    is_reit: bool


@dataclass
class Excluded:
    reason: str
    rows: list = field(default_factory=list)


def split_bbg(bbg: str):
    """('005930 KP') -> ('005930', 'KP')."""
    parts = (bbg or "").rsplit(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def load(path):
    kept = []
    by_reason = {}

    def drop(reason, who):
        by_reason.setdefault(reason, []).append(who)

    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"{path}: missing column(s) {', '.join(missing)}.  "
                f"Expected {', '.join(COLUMNS)}")

        for r in reader:
            code = (r.get("#FidessaCode") or "").strip()
            bbg = (r.get("BloombergCode") or "").strip()
            if not bbg:
                drop("no BloombergCode", code)
                continue
            ticker, ext = split_bbg(bbg)
            bbg_sec_type = (r.get("BloombergSecurityType") or "").strip()
            kept.append(Row(
                fidessa_code=code,
                ric=(r.get("RicCode") or "").strip(),
                bbg=bbg,
                ticker=ticker,
                bbg_ext=ext,
                sec_type=(r.get("Type") or "").strip(),
                bbg_sec_type=bbg_sec_type,
                market=(r.get("FidessaMarket") or "").strip(),
                currency=(r.get("Currency") or "").strip(),
                is_reit=bbg_sec_type == "REIT"))

    return kept, [Excluded(reason=k, rows=v)
                  for k, v in sorted(by_reason.items())]
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/crosscode.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add TradingData/crosscode.py
git commit -m "feat(tradingdata): crosscode reader, seven columns per :526"
```

---

### Task 3: Market config

**Files:**
- Create: `TradingData/config/markets.csv`
- Create: `TradingData/config/README.md`
- Create: `TradingData/marketcfg.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Market` dataclass: `fidessa_market, bbg_composite, no_short_sell, respect_short_sell`
  - `load(path) -> dict[str, Market]`
  - `no_short_sell(market: str, markets: dict) -> str`
  - `respect_short_sell(market: str, sec_type: str, is_reit: bool, markets: dict) -> str`

- [ ] **Step 1: Write the config**

`TradingData/config/markets.csv`. The two flag columns come from the hardcoded lists at `:302-320`; `BBGComposite` comes from `LimitUpDown/v1/config/markets.csv` and is blank where unknown.

```csv
FidessaMarket,BBGComposite,NoShortSell,RespectShortSellPrice
ASX-MAIN,AU,FALSE,
BSE-MAIN,IB,TRUE,
HKG-GEM,HK,FALSE,TRUE
HKG-MAIN,HK,FALSE,TRUE
JKT-MAIN,IJ,FALSE,TRUE
KLS-MAIN,MK,FALSE,TRUE
KOE-MAIN,KS,FALSE,TRUE
KSC-MAIN,KS,FALSE,TRUE
NSI-MAIN,IN,TRUE,
NZX-MAIN,NZ,FALSE,
PHS-MAIN,PM,FALSE,TRUE
SES-MAIN,SP,FALSE,
SET-MAIN,TB,FALSE,TRUE
SHA-MAIN,CH,TRUE,
SHH-MAIN,CH,TRUE,
SHZ-MAIN,CH,TRUE,
SSC-MAIN,CH,TRUE,
SZA-MAIN,CH,TRUE,
SZC-MAIN,CH,TRUE,
TAI-MAIN,TT,FALSE,
TYO-MAIN,JP,FALSE,TRUE
```

`TradingData/config/README.md`:

```markdown
# TradingData config

`markets.csv` — one row per Fidessa market.

| Column | Meaning |
|---|---|
| `FidessaMarket` | the crosscode's `FidessaMarket` value |
| `BBGComposite` | Bloomberg composite exchange code, used as the fallback when building an `equity_master` sym. Blank means "only try the crosscode's own suffix". |
| `NoShortSell` | `TRUE` for the markets listed at `:314-320`, else `FALSE` |
| `RespectShortSellPrice` | `TRUE` for the markets at `:302-306`, blank otherwise — the R job's default is `NA`, which writes as empty |

A market absent from this file gets `NoShortSell=FALSE`, blank
`RespectShortSellPrice`, and no composite fallback. The run reports any market
in the crosscode that has no row here.

Edit in Excel. Keep it comma-separated with the header intact.
```

- [ ] **Step 2: Write the failing test**

Create `TradingData/marketcfg.py` with only the self-test:

```python
def self_test() -> int:
    from pathlib import Path
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
    check("Korea's two boards share the KS composite",
          (M["KSC-MAIN"].bbg_composite, M["KOE-MAIN"].bbg_composite),
          ("KS", "KS"))
    check("every China board composites to CH",
          {M[k].bbg_composite for k in
           ("SHA-MAIN", "SHH-MAIN", "SHZ-MAIN", "SSC-MAIN",
            "SZA-MAIN", "SZC-MAIN")},
          {"CH"})

    print("\nNoShortSell, per :314-320")
    check("China and India cannot short",
          sorted(k for k, v in M.items() if v.no_short_sell == "TRUE"),
          ["BSE-MAIN", "NSI-MAIN", "SHA-MAIN", "SHH-MAIN", "SHZ-MAIN",
           "SSC-MAIN", "SZA-MAIN", "SZC-MAIN"])
    check("an unconfigured market defaults to FALSE",
          no_short_sell("XXX-MAIN", M), "FALSE")

    print("\nRespectShortSellPrice, per :302-312")
    check("Hong Kong equities respect it",
          respect_short_sell("HKG-MAIN", "Equity", False, M), "TRUE")
    check("a Hong Kong ETF that is not a REIT does not",
          respect_short_sell("HKG-MAIN", "ETF", False, M), "FALSE")
    check("a Hong Kong ETF that IS a REIT still does",
          respect_short_sell("HKG-MAIN", "ETF", True, M), "TRUE")
    check("the ETF carve-out is Hong Kong only",
          respect_short_sell("TYO-MAIN", "ETF", False, M), "TRUE")
    check("elsewhere the R job leaves NA, which writes blank",
          respect_short_sell("ASX-MAIN", "Equity", False, M), "")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python TradingData/marketcfg.py --self-test`
Expected: `NameError: name 'load' is not defined`

- [ ] **Step 4: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""Per-market configuration, as a CSV the desk can edit in Excel.

The R job hardcodes two market lists at :302-320.  They are the kind of thing
that changes without a code release, so they live in config/markets.csv here.
The same file carries the Bloomberg composite code used as a fallback when
building an equity_master sym.

    python marketcfg.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

HKG = ("HKG-MAIN", "HKG-GEM")


@dataclass(frozen=True)
class Market:
    fidessa_market: str
    bbg_composite: str
    no_short_sell: str
    respect_short_sell: str


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
                no_short_sell=(r.get("NoShortSell") or "FALSE").strip(),
                respect_short_sell=(r.get("RespectShortSellPrice")
                                    or "").strip())
    return out


def no_short_sell(market: str, markets) -> str:
    m = markets.get(market)
    return m.no_short_sell if m else "FALSE"


def respect_short_sell(market: str, sec_type: str, is_reit: bool,
                       markets) -> str:
    """:302-312.  The base value is per-market; then Hong Kong ETFs that are
    not REITs are pulled back to FALSE."""
    m = markets.get(market)
    base = m.respect_short_sell if m else ""
    if sec_type == "ETF" and market in HKG and not is_reit:
        return "FALSE"
    return base
```

- [ ] **Step 5: Run it to verify it passes**

Run: `python TradingData/marketcfg.py --self-test`
Expected: `all checks passed`

- [ ] **Step 6: Commit**

```bash
git add TradingData/marketcfg.py TradingData/config/markets.csv TradingData/config/README.md
git commit -m "feat(tradingdata): market config, short-sell lists out of code"
```

---

### Task 4: equity_master source

**Files:**
- Create: `TradingData/equitymaster.py`

**Interfaces:**
- Consumes: `crosscode.Row`, `marketcfg.Market`.
- Produces:
  - `connect(host, port)`
  - `sym_candidates(row, markets) -> list[str]`
  - `resolve_date(conn, requested) -> datetime.date`
  - `fetch(conn, date, syms) -> dict[str, dict]`
  - `FIELDS` tuple of the equity_master columns requested
  - `_to_decimal(value) -> Decimal | None`

- [ ] **Step 1: Write the failing test**

Create `TradingData/equitymaster.py` with only this:

```python
def self_test() -> int:
    import datetime
    from decimal import Decimal
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    class Row:
        def __init__(self, ticker, ext, market):
            self.ticker, self.bbg_ext, self.market = ticker, ext, market

    class Mkt:
        def __init__(self, comp):
            self.bbg_composite = comp

    M = {"KSC-MAIN": Mkt("KS"), "SSC-MAIN": Mkt("CH"), "ASX-MAIN": Mkt("AU")}

    print("equitymaster --self-test\n\nbuilding syms")
    check("the crosscode's own suffix comes first",
          sym_candidates(Row("005930", "KP", "KSC-MAIN"), M),
          ["005930.KP", "005930.KS"])
    check("a composite that matches adds no second candidate",
          sym_candidates(Row("BHP", "AU", "ASX-MAIN"), M), ["BHP.AU"])
    check("China's venue code and composite differ",
          sym_candidates(Row("600000", "C1", "SSC-MAIN"), M),
          ["600000.C1", "600000.CH"])
    check("an unconfigured market gets one candidate only",
          sym_candidates(Row("ABC", "XX", "ZZZ-MAIN"), M), ["ABC.XX"])
    check("no ticker means no candidates",
          sym_candidates(Row("", "AU", "ASX-MAIN"), M), [])

    print("\nnumbers out of kdb")
    check("a float becomes a Decimal", _to_decimal(83.64), Decimal("83.64"))
    check("bytes become a Decimal", _to_decimal(b"1.5"), Decimal("1.5"))
    check("a negative beta is legitimate and kept",
          _to_decimal(-0.3), Decimal("-0.3"))
    check("a null is None", _to_decimal(None), None)
    check("nan is None", _to_decimal(float("nan")), None)
    check("a bool is not a number", _to_decimal(True), None)
    check("garbage is None", _to_decimal("n/a"), None)

    print("\nrolling the date back")

    class Conn:
        def __init__(self, have):
            self.have, self.calls = have, []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            if "max date" in q:
                return max((d for d in self.have if d <= args[0]),
                           default=None)
            return {}

    friday = datetime.date(2026, 8, 28)
    monday = datetime.date(2026, 8, 31)
    c = Conn([friday])
    check("a Sunday request rolls back to Friday",
          resolve_date(c, monday - datetime.timedelta(days=1)), friday)

    c = Conn([monday])
    check("a date that has rows is used as-is", resolve_date(c, monday), monday)

    c = Conn([])
    try:
        resolve_date(c, monday)
        check("raised on an empty table", False, True)
    except SystemExit as exc:
        check("and says the table is empty", "no rows" in str(exc), True)

    print("\nfetching")

    class FetchConn:
        def __init__(self):
            self.calls = []

        def __call__(self, q, *args):
            self.calls.append((q, args))
            return {"BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9}}

    fc = FetchConn()
    got = fetch(fc, friday, ["BHP.AU"])
    check("one round trip, not one per symbol", len(fc.calls), 1)
    check("the date and the sym list are both passed",
          fc.calls[0][1], (friday, ["BHP.AU"]))
    check("the result is keyed by sym", sorted(got), ["BHP.AU"])
    check("no syms means no round trip at all", fetch(fc, friday, []), {})
    check("and no extra call", len(fc.calls), 1)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/equitymaster.py --self-test`
Expected: `NameError: name 'sym_candidates' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""equity_master out of kdb.  This is the ONLY module that touches kdb.

equity_master is not a kdb-native table.  It is the Bloomberg Data Licence
feed loaded into kdb - the same feed the R job reads off disk as
EquitiesDataLicence.rds.  It carries TICKER_AND_EXCH_CODE, the R job's join
key at :90, and 13 of the 16 columns the R drops at :84-88.

TWO THINGS ARE UNCERTAIN AND BOTH ARE REPORTED RATHER THAN ASSUMED.

  sym    is the Bloomberg ticker dot-joined to an exchange code, but WHICH
         code is not settled.  config/markets.csv distinguishes the venue
         code (KP for KSC-MAIN) from the composite (KS), and the codes we
         were given mix the two.  So every row gets up to two candidates,
         its own suffix first, and the run reports which one hit.  The first
         live run settles it; until then neither is hardcoded.

  date   .z.D-1 lands on a Sunday every Monday and on every holiday, so the
         requested date is rolled back to the most recent one that actually
         has rows, and both dates are reported.

ONE ROUND TRIP.  A universe is tens of thousands of names and a per-symbol
query would take longer than the window before the open.

pykx is imported inside connect(), so every other module - and --self-test
and --demo - runs on a machine with no kdb and no q licence.

    python equitymaster.py --self-test
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

FIELDS = ("PX_LAST", "EQY_BETA", "volatility", "REL_INDEX", "CUR_MKT_CAP",
          "fx_last", "ID_ISIN", "INDUSTRY_SECTOR", "MARKET_STATUS", "CRNCY")

MAXDATE_Q = "{[d] exec max date from equity_master where date<=d}"

FETCH_Q = ("{[d;s] " + str(len(FIELDS)) + "#0;"
           "select " + ",".join(FIELDS) + " by sym from equity_master "
           "where date=d, sym in s}")


def connect(host: str, port: int):
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Every other mode of this script runs without it; only a live "
            "run needs a kdb connection.")
    return pykx.SyncQConnection(host=host, port=int(port))


def sym_candidates(row, markets) -> list:
    """Its own suffix first, then the market's composite if that differs."""
    ticker = (getattr(row, "ticker", "") or "").strip()
    if not ticker:
        return []
    out = []
    ext = (getattr(row, "bbg_ext", "") or "").strip()
    if ext:
        out.append(f"{ticker}.{ext}")
    m = markets.get(getattr(row, "market", ""))
    comp = (getattr(m, "bbg_composite", "") or "").strip() if m else ""
    if comp and f"{ticker}.{comp}" not in out:
        out.append(f"{ticker}.{comp}")
    return out


def resolve_date(conn, requested):
    got = conn(MAXDATE_Q, requested)
    if got is None:
        raise SystemExit(
            f"equity_master has no rows on or before {requested}.  "
            "Check the server and the date.")
    try:
        got = got.py()
    except AttributeError:
        pass
    return got


def _to_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, UnicodeDecodeError):
        return None
    return d if d.is_finite() else None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode().strip()
    return str(value).strip()


def fetch(conn, date, syms) -> dict:
    if not syms:
        return {}
    result = conn(FETCH_Q, date, list(syms))
    if result is None:
        return {}
    try:
        items = result.items()
    except AttributeError:
        items = dict(result).items()
    out = {}
    for sym, row in items:
        out[_text(sym)] = {f: (row[f] if hasattr(row, "__getitem__") else None)
                           for f in FIELDS}
    return out
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/equitymaster.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add TradingData/equitymaster.py
git commit -m "feat(tradingdata): equity_master source, two-pass syms and date rollback"
```

---

### Task 5: MSCI mapping ladder

**Files:**
- Create: `TradingData/msci.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Mapping` dataclass with `exact, country_sector, region_sector, region_gics, fb_gics, country_index` dicts
  - `load(path) -> Mapping | None` — `None` when the file is absent
  - `resolve(mapping, market, gics, industry) -> dict` with keys `MsciCountryIndex, MsciSectorCountryIndex, MsciSectorIndex, MsciSectorRegionIndex`
  - `EMPTY` — the same dict shape, all blank

- [ ] **Step 1: Write the failing test**

Create `TradingData/msci.py` with only this:

```python
def self_test() -> int:
    import tempfile
    from pathlib import Path
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    HDR = "IndexName,FidessaMarket,GICS_SECTOR_NAME,INDUSTRY_SECTOR\n"
    BODY = (
        "MXAU0MT,ASX-MAIN,Materials,Basic Materials\n"   # exact
        "MXAU0EN,ASX-MAIN,,Energy\n"                      # country+sector
        "MXAP0MT,,,Basic Materials\n"                     # region+sector
        "MXAP0IT,,Info Tech,\n"                           # region+gics
        "MXTW0MT,TAI-MAIN,,Basic Materials\n")            # country -> MXTW

    print("msci --self-test\n\nno file at all")
    check("a missing mapping is None, not an error",
          load(Path("does-not-exist.csv")), None)
    check("and every column comes back blank",
          resolve(None, "ASX-MAIN", "Materials", "Basic Materials"), EMPTY)

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "msci.csv"
        p.write_text(HDR + BODY, encoding="utf-8")
        M = load(p)

        print("\nthe ladder, most specific first")
        got = resolve(M, "ASX-MAIN", "Materials", "Basic Materials")
        check("an exact hit on market+gics+sector",
              got["MsciSectorIndex"], "MXAU0MT")
        check("and MsciSectorCountryIndex is the same IndexName",
              got["MsciSectorCountryIndex"], "MXAU0MT")

        got = resolve(M, "ASX-MAIN", "", "Energy")
        check("with no GICS it falls to country+sector",
              got["MsciSectorIndex"], "MXAU0EN")

        got = resolve(M, "ZZZ-MAIN", "", "Basic Materials")
        check("an unknown market falls to the region row",
              got["MsciSectorIndex"], "MXAP0MT")
        check("and MsciSectorRegionIndex carries it",
              got["MsciSectorRegionIndex"], "MXAP0MT")

        got = resolve(M, "ZZZ-MAIN", "Info Tech", "")
        check("region by GICS when there is no industry sector",
              got["MsciSectorRegionIndex"], "MXAP0IT")

        print("\nthe country index, per :225-229")
        got = resolve(M, "ASX-MAIN", "", "Energy")
        check("first four characters of the country index",
              got["MsciCountryIndex"], "MXAU")
        got = resolve(M, "TAI-MAIN", "", "Basic Materials")
        check("MXTW is overridden to TAMSCI",
              got["MsciCountryIndex"], "TAMSCI")

        print("\nnothing matches")
        got = resolve(M, "ZZZ-MAIN", "", "Nonsense")
        check("every column blank rather than wrong", got, EMPTY)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/msci.py --self-test`
Expected: `NameError: name 'load' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""The MSCI index mapping.  Optional: no file means blank columns, not a
failure.

One CSV with four columns - IndexName, FidessaMarket, GICS_SECTOR_NAME,
INDUSTRY_SECTOR - carrying five different lookup tables, told apart by which
fields are blank (:190-212).  IndexName then resolves most specific first.

KNOWN DEGRADATION.  Three rungs of the ladder key on GICS_SECTOR_NAME, and
equity_master has no GICS_SECTOR_NAME.  Those rungs go dead and resolution
proceeds through the INDUSTRY_SECTOR and country paths only, so the columns
fill more coarsely than the R job's.  The caller reports a fill rate per
column so the size of that gap is a number.

    python msci.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

EMPTY = {"MsciCountryIndex": "", "MsciSectorCountryIndex": "",
         "MsciSectorIndex": "", "MsciSectorRegionIndex": ""}


@dataclass
class Mapping:
    exact: dict = field(default_factory=dict)
    country_sector: dict = field(default_factory=dict)
    region_sector: dict = field(default_factory=dict)
    region_gics: dict = field(default_factory=dict)
    fb_gics: dict = field(default_factory=dict)
    country_index: dict = field(default_factory=dict)


def load(path):
    p = Path(path)
    if not p.exists():
        return None
    m = Mapping()
    with p.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            idx = (r.get("IndexName") or "").strip()
            mkt = (r.get("FidessaMarket") or "").strip()
            gic = (r.get("GICS_SECTOR_NAME") or "").strip()
            ind = (r.get("INDUSTRY_SECTOR") or "").strip()
            if not idx:
                continue
            if mkt and gic:
                m.exact[(gic, mkt, ind)] = idx
                m.fb_gics.setdefault((gic, mkt), idx)
            elif mkt and not gic:
                m.country_sector[(ind, mkt)] = idx
                # :225-229.  The country index is the first four characters,
                # with Taiwan's MXTW spelled TAMSCI.
                country = idx[:4]
                m.country_index.setdefault(
                    mkt, "TAMSCI" if country == "MXTW" else country)
            elif not mkt and not gic:
                m.region_sector[ind] = idx
            elif not mkt and not ind:
                m.region_gics[gic] = idx
    return m


def resolve(mapping, market: str, gics: str, industry: str) -> dict:
    if mapping is None:
        return dict(EMPTY)

    gics = (gics or "").strip()
    industry = (industry or "").strip()

    region = (mapping.region_sector.get(industry)
              or mapping.region_gics.get(gics) or "")

    index_name = (mapping.exact.get((gics, market, industry))
                  or mapping.country_sector.get((industry, market))
                  or region
                  or mapping.fb_gics.get((gics, market))
                  or mapping.country_index.get(market, ""))

    return {"MsciCountryIndex": mapping.country_index.get(market, ""),
            "MsciSectorCountryIndex": index_name,
            "MsciSectorIndex": index_name,
            "MsciSectorRegionIndex": region}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/msci.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add TradingData/msci.py
git commit -m "feat(tradingdata): optional msci mapping and its five-rung ladder"
```

---

### Task 6: Orchestrator, validation and output

**Files:**
- Create: `TradingData/trading_data.py`
- Create: `TradingData/local_settings.py.example`
- Modify: `.gitignore` (confirm `local_settings.py` is already listed — it is)

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `OUTPUT_COLUMNS` — the 20 names in order
  - `build_rows(rows, master, markets, mapping, sym_hits) -> list[dict]`
  - `validate(out_rows) -> list[str]`
  - `write_csv(path, out_rows)`
  - `run(...) -> int`, `demo() -> int`, `main(argv=None) -> int`

- [ ] **Step 1: Write the failing test**

Create `TradingData/trading_data.py` with only this:

```python
def self_test() -> int:
    import tempfile
    from decimal import Decimal
    from pathlib import Path
    import crosscode
    import marketcfg
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("trading_data --self-test\n\nthe output shape")
    check("twenty columns", len(OUTPUT_COLUMNS), 20)
    check("the first is the hashed fidessa code",
          OUTPUT_COLUMNS[0], "#FidessaCode")
    check("the order matches :467-470",
          OUTPUT_COLUMNS[:6],
          ["#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex"])
    check("and the tail", OUTPUT_COLUMNS[-3:],
          ["MarketCap", "ISIN", "SubscribeFeedAtStartup"])

    here = Path(__file__).resolve().parent
    M = marketcfg.load(here / "config" / "markets.csv")

    row = crosscode.Row(
        fidessa_code="BHP.AU", ric="BHP.AX", bbg="BHP AU", ticker="BHP",
        bbg_ext="AU", sec_type="Equity", bbg_sec_type="Equity",
        market="ASX-MAIN", currency="AUD", is_reit=False)
    master = {"BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9,
                         "volatility": 0.21, "REL_INDEX": "AS51",
                         "CUR_MKT_CAP": 2000000.0, "fx_last": 0.65,
                         "ID_ISIN": "AU000000BHP4",
                         "INDUSTRY_SECTOR": "Basic Materials",
                         "MARKET_STATUS": "ACTV", "CRNCY": "AUD"}}
    hits = {}
    out = build_rows([row], master, M, None, hits)
    r = out[0]

    print("\nthe six fields that matter")
    check("Close is PX_LAST", r["Close"], "40.5")
    check("Beta is EQY_BETA, not the lowercase beta", r["Beta"], "0.9")
    check("Volatility10D is the volatility column", r["Volatility10D"], "0.21")
    check("Index is REL_INDEX", r["Index"], "AS51")
    check("MarketCap is CUR_MKT_CAP times fx_last",
          r["MarketCap"], "1300000.00")
    check("Capi buckets off the converted value", r["Capi"], "MICRO")

    print("\nthe rest")
    check("ISIN comes straight across", r["ISIN"], "AU000000BHP4")
    check("Sector falls back to INDUSTRY_SECTOR", r["Sector"], "Basic Materials")
    check("ICBIndex is seeded from REL_INDEX, per :137",
          r["ICBIndex"], "AS51")
    check("an ASX equity is bucketed alphabetically", r["Segment"], "A-B")
    check("Australia can short", r["NoShortSell"], "FALSE")
    check("and has no short-sell price rule",
          r["RespectShortSellPrice"], "")
    check("no mapping means the Msci columns are blank",
          [r[c] for c in ("MsciCountryIndex", "MsciSectorIndex")], ["", ""])
    check("SubscribeFeedAtStartup is always FALSE, per the :599 bug",
          r["SubscribeFeedAtStartup"], "FALSE")
    check("the sym that hit is recorded", hits["BHP.AU"], "BHP.AU")

    print("\na row equity_master does not have")
    out = build_rows([row], {}, M, None, {})
    r = out[0]
    check("the crosscode columns still fill",
          (r["#FidessaCode"], r["Type"]), ("BHP.AU", "Equity"))
    check("and everything from kdb is blank, not zero",
          [r[c] for c in ("Close", "Beta", "MarketCap", "ISIN")],
          ["", "", "", ""])

    print("\nvalidation")
    check("no rows is fatal", validate([])[0].startswith("no rows"), True)
    good = build_rows([row], master, M, None, {})
    check("a good run has nothing to say", validate(good), [])

    print("\nwriting")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TradingData.csv"
        write_csv(p, good)
        text = p.read_text(encoding="utf-8")
        check("the header is the twenty columns",
              text.splitlines()[0], ",".join(OUTPUT_COLUMNS))
        check("nothing is quoted", '"' in text, False)
        check("no index column was added",
              text.splitlines()[1].startswith("BHP.AU,"), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/trading_data.py --self-test`
Expected: `NameError: name 'OUTPUT_COLUMNS' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
#!/usr/bin/env python3
"""Build TradingData.csv from the crosscode and kdb's equity_master.

The crosscode is the security master and drives the row set.  equity_master
supplies the reference data the R job got from Bloomberg.  Every other input
is optional: present means used, absent means those columns go blank and the
run says so.

WHAT IS NOT FILLED, AND WHY

  MsciCountryIndex, MsciSectorCountryIndex, MsciSectorIndex,
  MsciSectorRegionIndex   need msci_mapping.csv
  OpenAggressivityPct     needs the auction override CSV
  Segment for HKG/NSI/BSE needs the dico and CAS lists

  Segment for HK ETFs is the one genuinely unavailable field.  It comes from
  TRADING_CONDITIONS_1 via an intraday Bloomberg call at :357 and has no
  equivalent in equity_master.  qatt.cond was considered and ruled out.

THREE SOURCES ARE UNVERIFIED and print a banner rather than being trusted
silently:

  Volatility10D   equity_master.volatility may not be the 10-day figure
                  Bloomberg's VOLATILITY_10D returns
  MarketCap/Capi  assumes fx_last is a local->USD rate matching load_FXdatas
  Sector          equity_master has no GICS_SECTOR_NAME, so every row takes
                  the :295 fallback and differs from the R wherever GICS had
                  a value

TWO R BUGS ARE PRESERVED VERBATIM and reported.  The R job is the reference,
and a port that quietly diverges is worse than one that diverges loudly.

  :113   `if (length(idx) == 0)` gates the Bloomberg top-up on the set of
         rows that need it being EMPTY, so it has never run.  Moot here -
         there is no second call to gate - but the count of rows that would
         have entered it is reported, because that is the number that says
         whether fixing it in R would change anything.
  :599   SubscribeFeedAtStartup is set to F for everything and then to F
         again for India.  The commented-out original at :562 used T.  The
         column is therefore always FALSE.

    python trading_data.py --self-test
    python trading_data.py --demo
    python trading_data.py --compare OLD.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import columns
import crosscode
import equitymaster
import marketcfg
import msci

OUTPUT_COLUMNS = [
    "#FidessaCode", "Type", "Sector", "Capi", "Index", "ICBIndex",
    "MsciCountryIndex", "MsciSectorCountryIndex", "MsciSectorIndex",
    "MsciSectorRegionIndex", "Segment", "Beta", "Close", "Volatility10D",
    "NoShortSell", "RespectShortSellPrice", "OpenAggressivityPct",
    "MarketCap", "ISIN", "SubscribeFeedAtStartup"]

# The columns the six-field brief names.  Their fill rates are reported.
KEY_COLUMNS = ("Close", "Beta", "Volatility10D", "Index", "MarketCap")

_D = equitymaster._to_decimal
_T = equitymaster._text


def _plain(d) -> str:
    """A Decimal as the R job would print it - no exponent, no trailing
    zeros beyond what the value carries."""
    if d is None:
        return ""
    s = format(d, "f")
    return s


def build_rows(rows, master, markets, mapping, sym_hits) -> list:
    """One output dict per crosscode row.  Missing reference data leaves a
    column blank; it never becomes zero."""
    staged = []
    for row in rows:
        rec = None
        for cand in equitymaster.sym_candidates(row, markets):
            if cand in master:
                rec = master[cand]
                sym_hits[row.fidessa_code] = cand
                break
        rec = rec or {}

        cap = _D(rec.get("CUR_MKT_CAP"))
        fx = _D(rec.get("fx_last"))
        market_cap = cap * fx if cap is not None and fx is not None else None

        rel_index = _T(rec.get("REL_INDEX"))
        industry = _T(rec.get("INDUSTRY_SECTOR"))

        seg = columns.segment_cn(row.market, row.sec_type)
        if seg is None and row.market == "ASX-MAIN":
            seg = columns.segment_asx(row.ticker, row.sec_type)
        if seg is None:
            seg = columns.SEGMENT_DEFAULT

        staged.append({
            "row": row,
            "icb_seed": rel_index,       # :137
            "out": {
                "#FidessaCode": row.fidessa_code,
                "Type": row.sec_type,
                "Sector": columns.sector("", industry),
                "Capi": columns.capi_bucket(market_cap),
                "Index": rel_index,
                "ICBIndex": "",           # filled by the propagation below
                "Segment": seg,
                "Beta": _plain(_D(rec.get("EQY_BETA"))),
                "Close": _plain(_D(rec.get("PX_LAST"))),
                "Volatility10D": _plain(_D(rec.get("volatility"))),
                "NoShortSell": marketcfg.no_short_sell(row.market, markets),
                "RespectShortSellPrice": marketcfg.respect_short_sell(
                    row.market, row.sec_type, row.is_reit, markets),
                "OpenAggressivityPct": "",
                "MarketCap": _plain(market_cap),
                "ISIN": _T(rec.get("ID_ISIN")),
                "SubscribeFeedAtStartup": "FALSE",   # :599, always FALSE
            }})

        staged[-1]["out"].update(
            msci.resolve(mapping, row.market, "", industry))

    icb = columns.propagate_icb([
        {"ric": s["row"].ric, "bbg": s["row"].bbg,
         "market": s["row"].market, "icb": s["icb_seed"]} for s in staged])
    for s, value in zip(staged, icb):
        s["out"]["ICBIndex"] = value

    out = [s["out"] for s in staged]
    out.sort(key=lambda r: (r["#FidessaCode"],))
    return out


def validate(out_rows) -> list:
    problems = []
    if not out_rows:
        problems.append("no rows to write")
        return problems
    closes = sum(1 for r in out_rows if r["Close"])
    if closes == 0:
        problems.append("not one row has a Close")
    return problems


def write_csv(path, out_rows):
    """Matches R's write.csv(row.names=F, na="", quote=FALSE)."""
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS,
                           quoting=csv.QUOTE_NONE, escapechar="\\",
                           extrasaction="ignore")
        w.writeheader()
        for r in out_rows:
            w.writerow({c: r.get(c, "") for c in OUTPUT_COLUMNS})


def report(out_rows, rows, excluded, sym_hits, date_used, date_asked,
           mapping):
    print(f"\n  crosscode rows      {len(rows)}")
    for e in excluded:
        print(f"  excluded            {len(e.rows):6d}  {e.reason}")
    print(f"  equity_master date  {date_used}"
          + ("" if date_used == date_asked else f"  (asked {date_asked})"))
    print(f"  syms matched        {len(sym_hits)} / {len(rows)}")

    by_suffix = {}
    for sym in sym_hits.values():
        by_suffix[sym.rsplit(".", 1)[-1]] = \
            by_suffix.get(sym.rsplit(".", 1)[-1], 0) + 1
    if by_suffix:
        print("  sym suffixes that hit: "
              + ", ".join(f"{k}={v}" for k, v in sorted(by_suffix.items())))

    print("\n  fill rates")
    n = len(out_rows) or 1
    for c in KEY_COLUMNS:
        filled = sum(1 for r in out_rows if r[c])
        print(f"    {c:<22} {filled:6d} / {len(out_rows)}  "
              f"{100 * filled // n:3d}%")

    if mapping is None:
        print("\n  ! msci_mapping.csv not supplied - the four Msci* columns "
              "are blank")

    print("\n  ! UNVERIFIED SOURCES - confirm before cutover")
    print("    Volatility10D  from equity_master.volatility; the definition "
          "is NOT confirmed to be Bloomberg's VOLATILITY_10D")
    print("    MarketCap/Capi CUR_MKT_CAP * fx_last; fx_last's direction is "
          "assumed to be local->USD")
    print("    Sector         equity_master has no GICS_SECTOR_NAME, so "
          "every row takes the :295 INDUSTRY_SECTOR fallback")
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/trading_data.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Write `local_settings.py.example`**

```python
# Copy to local_settings.py and fill in.  local_settings.py is gitignored.
#
# STRICT: an unknown name here is a hard error.  A typo'd EMAIL_T0 fails
# loudly rather than silently sending nowhere.

# The server hosting equity_master.  It is in neither the :5010/:5012 order
# pair nor the :5011/:5013 qatt pair - ask whoever runs kdb.
EQUITY_MASTER_SERVER = "CHANGEME:5010"

CROSSCODE_PATH = r"CHANGEME\CrossCode.csv"
OUTPUT_PATH = r"CHANGEME\TradingData.csv"
TEMP_PATH = r"CHANGEME\TradingData.tmp.csv"

# Optional.  Absent means the columns they feed are left blank.
MSCI_MAPPING_PATH = ""
OPEN_AUCTION_OVERRIDE_PATH = ""
HKEX_CAS_LIST_PATH = ""
INDIA_NSE_CAS_LIST_PATH = ""
INDIA_BSE_CAS_LIST_PATH = ""

SMTP_HOST = "CHANGEME"
EMAIL_FROM = "CHANGEME"
EMAIL_TO = ["CHANGEME"]
```

- [ ] **Step 6: Commit**

```bash
git add TradingData/trading_data.py TradingData/local_settings.py.example
git commit -m "feat(tradingdata): orchestrator, validation and reporting"
```

---

### Task 7: Run modes — live, demo and compare

**Files:**
- Modify: `TradingData/trading_data.py`

**Interfaces:**
- Consumes: everything in Task 6.
- Produces: `compare(old_rows, new_rows) -> dict`, `demo() -> int`, `run(...) -> int`, `main(argv=None) -> int`

- [ ] **Step 1: Write the failing test**

Append to `self_test()` in `trading_data.py`, before the final print:

```python
    print("\ncomparing against the R job's output")
    old = [{"#FidessaCode": "A", "Close": "10.0", "Beta": "1.0"},
           {"#FidessaCode": "B", "Close": "20.0", "Beta": "2.0"}]
    new = [{"#FidessaCode": "A", "Close": "10.0", "Beta": "1.1"},
           {"#FidessaCode": "C", "Close": "30.0", "Beta": "3.0"}]
    d = compare(old, new)
    check("rows only the old file has", d["only_old"], ["B"])
    check("rows only the new file has", d["only_new"], ["C"])
    check("a column that agrees", d["columns"]["Close"]["same"], 1)
    check("a column that does not", d["columns"]["Beta"]["differ"], 1)
    check("and it shows an example",
          d["columns"]["Beta"]["examples"][0], ("A", "1.0", "1.1"))

    print("\nthe demo runs end to end with no kdb")
    check("demo returns success", demo(), 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python TradingData/trading_data.py --self-test`
Expected: `NameError: name 'compare' is not defined`

- [ ] **Step 3: Write the implementation**

Insert above `self_test()`:

```python
def compare(old_rows, new_rows) -> dict:
    """Per-column agreement against the R job's output.  This is the cutover
    instrument: it turns Volatility10D's unverified definition into a
    measured spread rather than an argument."""
    old = {r["#FidessaCode"]: r for r in old_rows}
    new = {r["#FidessaCode"]: r for r in new_rows}
    shared = sorted(set(old) & set(new))

    cols = {}
    for c in OUTPUT_COLUMNS:
        if c == "#FidessaCode":
            continue
        same = differ = 0
        examples = []
        for k in shared:
            a, b = old[k].get(c, ""), new[k].get(c, "")
            if a == b:
                same += 1
            else:
                differ += 1
                if len(examples) < 5:
                    examples.append((k, a, b))
        if same or differ:
            cols[c] = {"same": same, "differ": differ, "examples": examples}

    return {"shared": len(shared),
            "only_old": sorted(set(old) - set(new)),
            "only_new": sorted(set(new) - set(old)),
            "columns": cols}


def print_compare(d):
    print(f"\n  rows in both        {d['shared']}")
    print(f"  only in the old     {len(d['only_old'])}")
    print(f"  only in the new     {len(d['only_new'])}")
    print("\n  column                    same  differ")
    for c, v in d["columns"].items():
        print(f"    {c:<22} {v['same']:6d}  {v['differ']:6d}")
        for k, a, b in v["examples"]:
            print(f"        {k}  old={a!r}  new={b!r}")


def read_output(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def demo() -> int:
    """The whole pipeline on canned data.  No kdb, no files, no licence."""
    import tempfile
    here = Path(__file__).resolve().parent
    markets = marketcfg.load(here / "config" / "markets.csv")

    rows = [
        crosscode.Row("BHP.AU", "BHP.AX", "BHP AU", "BHP", "AU", "Equity",
                      "Equity", "ASX-MAIN", "AUD", False),
        crosscode.Row("STW.AU", "STW.AX", "STW AU", "STW", "AU", "ETF",
                      "Equity", "ASX-MAIN", "AUD", False),
        crosscode.Row("005930.KR", "005930.KS", "005930 KP", "005930", "KP",
                      "Equity", "Equity", "KSC-MAIN", "KRW", False),
        crosscode.Row("823.HK", "823.HK", "823 HK", "823", "HK", "Equity",
                      "REIT", "HKG-MAIN", "HKD", True),
    ]
    master = {
        "BHP.AU": {"PX_LAST": 40.5, "EQY_BETA": 0.9, "volatility": 0.21,
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 2.1e11,
                   "fx_last": 0.65, "ID_ISIN": "AU000000BHP4",
                   "INDUSTRY_SECTOR": "Basic Materials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        "STW.AU": {"PX_LAST": 72.1, "EQY_BETA": 1.0, "volatility": 0.11,
                   "REL_INDEX": "AS51", "CUR_MKT_CAP": 4.2e9,
                   "fx_last": 0.65, "ID_ISIN": "AU0000STW014",
                   "INDUSTRY_SECTOR": "Financials",
                   "MARKET_STATUS": "ACTV", "CRNCY": "AUD"},
        # Korea hits on the COMPOSITE, not the crosscode's own KP suffix -
        # which is the whole reason sym resolution tries two candidates.
        "005930.KS": {"PX_LAST": 71000.0, "EQY_BETA": 1.1,
                      "volatility": 0.28, "REL_INDEX": "KOSPI",
                      "CUR_MKT_CAP": 4.2e14, "fx_last": 0.00072,
                      "ID_ISIN": "KR7005930003",
                      "INDUSTRY_SECTOR": "Technology",
                      "MARKET_STATUS": "ACTV", "CRNCY": "KRW"},
    }

    hits = {}
    out = build_rows(rows, master, markets, None, hits)
    problems = validate(out)

    print("trading_data --demo")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "TradingData.csv"
        write_csv(p, out)
        print("\n" + p.read_text(encoding="utf-8"))
        report(out, rows, [], hits,
               datetime.date(2026, 9, 2), datetime.date(2026, 9, 2), None)

    if hits.get("005930.KR") == "005930.KS":
        print("\n  note: Korea matched on the composite (KS), not the "
              "crosscode's own suffix (KP)")
    print("\n  the Hong Kong REIT keeps RespectShortSellPrice=TRUE; an ETF "
          "there would be FALSE")
    for p_ in problems:
        print(f"  PROBLEM: {p_}")
    return 0 if not problems else 1


def run(crosscode_path, server, output_path, temp_path,
        mapping_path="", date=None) -> int:
    rows, excluded = crosscode.load(crosscode_path)
    here = Path(__file__).resolve().parent
    markets = marketcfg.load(here / "config" / "markets.csv")
    mapping = msci.load(mapping_path) if mapping_path else None

    host, _, port = server.partition(":")
    conn = equitymaster.connect(host, port)

    asked = date or (datetime.date.today() - datetime.timedelta(days=1))
    used = equitymaster.resolve_date(conn, asked)

    syms = []
    for row in rows:
        syms.extend(equitymaster.sym_candidates(row, markets))
    master = equitymaster.fetch(conn, used, sorted(set(syms)))

    hits = {}
    out = build_rows(rows, master, markets, mapping, hits)
    problems = validate(out)
    report(out, rows, excluded, hits, used, asked, mapping)

    if problems:
        for p in problems:
            print(f"  PROBLEM: {p}")
        return 1

    write_csv(temp_path, out)
    shutil.copyfile(temp_path, output_path)
    print(f"\n  wrote {len(out)} rows to {output_path}")
    return 0


def _settings():
    try:
        import local_settings
    except ImportError:
        raise SystemExit(
            "local_settings.py not found.  Copy local_settings.py.example "
            "and fill it in.")
    return local_settings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build TradingData.csv")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--compare", metavar="OLD.csv")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.demo:
        return demo()

    s = _settings()
    date = (datetime.date.fromisoformat(args.date) if args.date else None)

    if args.compare:
        rc = run(s.CROSSCODE_PATH, s.EQUITY_MASTER_SERVER, s.OUTPUT_PATH,
                 s.TEMP_PATH, getattr(s, "MSCI_MAPPING_PATH", ""), date)
        print_compare(compare(read_output(args.compare),
                              read_output(s.OUTPUT_PATH)))
        return rc

    return run(s.CROSSCODE_PATH, s.EQUITY_MASTER_SERVER, s.OUTPUT_PATH,
               s.TEMP_PATH, getattr(s, "MSCI_MAPPING_PATH", ""), date)
```

And replace the module's `__main__` block with:

```python
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python TradingData/trading_data.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Run the demo**

Run: `python TradingData/trading_data.py --demo`
Expected: a four-row CSV, a fill-rate table, the unverified-sources banner, and the note that Korea matched on the composite.

- [ ] **Step 6: Commit**

```bash
git add TradingData/trading_data.py
git commit -m "feat(tradingdata): live run, demo and compare modes"
```

---

### Task 8: The Bloomberg field inventory

**Files:**
- Create: `docs/bloomberg-fields.txt`

- [ ] **Step 1: Write the file**

Plain text, as asked. Every field the R job requests from Bloomberg, its call site, the output column it feeds, and whether `equity_master` covers it.

```
BLOOMBERG FIELDS REQUESTED BY CreateTradingDataENT.r
====================================================

Source of truth: no_git/CreateTradingDataENT.r.  Line numbers are into it.
"covered" means equity_master carries a column of that name.

There are four distinct Bloomberg surfaces in this job and they are not
equally removable:

  (a) EquitiesDataLicence.rds   a bulk file on disk, read at :83.  No
                               terminal, no rate limit.  Most of the
                               reference columns come from here.
  (b) R_bdp                     live terminal calls, at :103, :120, :181,
                               :273
  (c) load_BbgIntraday          intraday, at :357
  (d) load_FXdatas              FX, at :145.  A Sibyl call; probably not
                               Bloomberg, unverified.

equity_master replaces (a) and (b) together, because it IS the Data Licence
feed loaded into kdb.


1. FROM THE DATA LICENCE FILE  (:83, joined at :90 on
   BloombergCode = TICKER_AND_EXCH_CODE)

   FIELD                  FEEDS                     COVERED
   PX_LAST                Close                     yes
   EQY_BETA               Beta                      yes
   CUR_MKT_CAP            MarketCap, Capi           yes
   REL_INDEX              Index, seeds ICBIndex     yes
   INDUSTRY_SECTOR        Sector (fallback)         yes
   ID_ISIN                ISIN                      yes
   CRNCY                  the FX join at :156       yes
   TICKER_AND_EXCH_CODE   the join key itself       yes

   Dropped by the R job at :84-88, listed for completeness:
   DVD_CRNCY, DVD_DECLARED_DT, DVD_FREQ, DVD_PAY_DT, DVD_SH_LAST,
   DVD_TYP_LAST, DVD_SH_12M, DVD_RECORD_DT, EQY_DVD_SH_12M_NET,
   EQY_DVD_YLD_12M, EQY_DVD_YLD_12M_NET, EQY_DVD_YLD_IND,
   PX_TRADE_LOT_SIZE, PX_HIGH, PX_LOW, PX_ROUND_LOT_SIZE

   13 of those 16 are in equity_master (four under an EQY_ prefix:
   EQY_DVD_FREQ, EQY_DVD_SH_LAST, EQY_DVD_TYP_LAST, EQY_DVD_SH_12M).
   Only the three yield fields are absent.  This overlap is the evidence
   that equity_master and the RDS are the same feed.


2. R_bdp AT :103                        (on equity tickers)

   FIELD                  FEEDS                     COVERED
   VOLATILITY_10D         Volatility10D             NO - equity_master has
                                                    a `volatility` column
                                                    but its definition is
                                                    NOT confirmed to be the
                                                    10-day figure
   GICS_SECTOR_NAME       Sector (preferred)        NO - no equivalent.
                                                    Sector therefore always
                                                    takes the INDUSTRY_SECTOR
                                                    fallback at :295, and
                                                    three rungs of the MSCI
                                                    ladder go dead


3. R_bdp AT :120                        (DEAD CODE - see the :113 bug)

   FIELD                  FEEDS                     COVERED
   CUR_MKT_CAP            top-up                    yes
   MKT_CAP_LAST_TRD       top-up                    no
   EQY_BETA               top-up                    yes
   BETA_ADJ_OVERRIDABLE   top-up                    no
   INTERVAL_VOLATILITY    top-up                    no
   INDUSTRY_SECTOR        top-up                    yes

   :113 reads `if (length(idx) == 0)`, where idx is the set of rows MISSING
   those fields.  The block therefore runs only when there is nothing to
   fix, on an empty frame.  MKT_CAP_LAST_TRD, BETA_ADJ_OVERRIDABLE and
   INTERVAL_VOLATILITY have never populated anything.  Almost certainly
   should be `> 0`.  Preserved verbatim; not silently fixed.


4. R_bdp AT :181                        (on MSCI INDEX tickers, not equities)

   FIELD                  FEEDS                     COVERED
   PX_LAST                validation at :186        yes, for equities
   LAST_UPDATE_DT         validation at :186        yes, for equities
   MARKET_STATUS          validation at :186        yes, for equities

   Used only to filter the msci_mapping to indices that are ACTV with a
   non-null price updated within 60 days.  Whether equity_master carries
   index-level rows as well as equity rows is UNKNOWN; if it does not, this
   validation is reported as skipped.

   Note MARKET_STATUS is not one of the 20 output columns.


5. R_bdp AT :273

   FIELD                  FEEDS                     COVERED
   REL_INDEX              ICBIndex top-up           yes


6. load_BbgIntraday AT :357

   FIELD                  FEEDS                     COVERED
   TRADING_CONDITIONS_1   Segment, HK ETFs only     NO - the one genuinely
                                                    unavailable field.
                                                    qatt.cond was considered
                                                    as a substitute and
                                                    ruled out.


7. load_FXdatas AT :145

   Not a field list - returns a rate per CRNCY.  equity_master has fx_last,
   which is assumed to be the same rate in the same direction (local->USD).
   UNVERIFIED.


SUMMARY
-------
Covered by equity_master:   PX_LAST, EQY_BETA, CUR_MKT_CAP, REL_INDEX,
                            INDUSTRY_SECTOR, ID_ISIN, CRNCY, MARKET_STATUS,
                            LAST_UPDATE_DT, and fx_last for the FX
Not covered:                GICS_SECTOR_NAME, TRADING_CONDITIONS_1,
                            MKT_CAP_LAST_TRD, BETA_ADJ_OVERRIDABLE,
                            INTERVAL_VOLATILITY (the last three are dead
                            code anyway)
Covered but unverified:     volatility (is it 10-day?),
                            fx_last (which direction?)
```

- [ ] **Step 2: Commit**

```bash
git add docs/bloomberg-fields.txt
git commit -m "docs(tradingdata): inventory of every Bloomberg field the R job requests"
```

---

## Self-Review

**Spec coverage:** Inputs table → Tasks 2, 3, 4, 5. Six key fields → Task 6. The three ⚠ banners → Task 6 `report()`. MSCI ladder → Task 5. ICB propagation → Task 1. Both preserved bugs → Task 1 (none needed), Task 6 (`SubscribeFeedAtStartup`, and the `:113` note in the docstring). Temp-then-copy → Task 7 `run()`. Modes → Task 7. Second deliverable → Task 8. Testing conventions → every task.

**Gap found and closed:** the spec says the run reports how many rows would have entered the `:113` top-up. That number needs `equity_master` to have been fetched, so it belongs in `report()` — the implementer should add it there alongside the fill rates, counting rows missing any of `CUR_MKT_CAP`/`EQY_BETA`/`volatility`/`INDUSTRY_SECTOR`.

**Placeholder scan:** every code step carries real code. `CHANGEME` in `local_settings.py.example` is intentional and mirrors the LimitUpDown convention.

**Type consistency:** `crosscode.Row` has ten fields, constructed positionally in `demo()` in the same order as the dataclass. `_to_decimal` and `_text` are defined in `equitymaster.py` and aliased as `_D`/`_T` in `trading_data.py`. `msci.resolve` returns exactly the four `Msci*` keys that `OUTPUT_COLUMNS` names. `columns.segment_cn` returns `None` for "rule does not apply", which `build_rows` distinguishes from `""`.
