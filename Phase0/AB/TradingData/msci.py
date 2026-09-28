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

    region = ((mapping.region_sector.get(industry) if industry else "")
              or (mapping.region_gics.get(gics) if gics else "") or "")

    index_name = (mapping.exact.get((gics, market, industry))
                  or mapping.country_sector.get((industry, market))
                  or region
                  or mapping.fb_gics.get((gics, market))
                  or mapping.country_index.get(market, ""))

    return {"MsciCountryIndex": mapping.country_index.get(market, ""),
            "MsciSectorCountryIndex": index_name,
            "MsciSectorIndex": index_name,
            "MsciSectorRegionIndex": region}


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    HDR = "IndexName,FidessaMarket,GICS_SECTOR_NAME,INDUSTRY_SECTOR\n"
    BODY = (
        "MXAU0MT,ASX-MAIN,Materials,Basic Materials\n"
        "MXAU0EN,ASX-MAIN,,Energy\n"
        "MXAP0MT,,,Basic Materials\n"
        "MXAP0IT,,Info Tech,\n"
        "MXTW0MT,TAI-MAIN,,Basic Materials\n")

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
