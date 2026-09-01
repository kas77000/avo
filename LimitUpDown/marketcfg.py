#!/usr/bin/env python3
"""Load and validate the three config files.

Validation is strict and loud.  A venue with no band tiers, a rounding mode
nobody implements, a bands row for a venue that markets.csv has never heard
of - each of those is a config bug that would otherwise surface as a missing
market in a production feed, which is exactly the failure nobody notices
until a trader does.

    python marketcfg.py --self-test
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import bands
import ticks

VALID_ROUNDING = ("inward", "outward", "nearest")
VALID_REFPRICE = ("close_print", "last_trade")
VALID_KIND = ("pct", "abs")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Venue:
    country: str
    venue_id: str
    bbg_venue: str
    bbg_composite: str
    cutoff: time
    ref_price: str
    tick_source: str
    min_price: Optional[Decimal]
    rounding: str


@dataclass(frozen=True)
class Config:
    venues: dict
    bands: dict
    ticks: dict


def _decimal(value: str, what: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ConfigError(f"{what}: {value!r} is not a number")


def load(config_dir, tsr_dir) -> Config:
    def rows(name):
        path = Path(config_dir) / name
        if not path.is_file():
            raise ConfigError(f"{path} does not exist")
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))

    venues = {}
    for r in rows("markets.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid in venues:
            raise ConfigError(f"markets.csv: duplicate venue {vid}")
        if r["Rounding"].strip() not in VALID_ROUNDING:
            raise ConfigError(
                f"markets.csv {vid}: Rounding {r['Rounding']!r} is not one of "
                f"{VALID_ROUNDING}")
        if r["RefPrice"].strip() not in VALID_REFPRICE:
            raise ConfigError(
                f"markets.csv {vid}: RefPrice {r['RefPrice']!r} is not one of "
                f"{VALID_REFPRICE}")
        raw_min = (r["MinPrice"] or "").strip()
        try:
            hh, mm, ss = (int(x) for x in r["Time"].strip().split(":"))
            cutoff = time(hh, mm, ss)
        except ValueError:
            raise ConfigError(
                f"markets.csv {vid}: Time {r['Time']!r} is not HH:MM:SS")
        venues[vid] = Venue(
            country=r["Country"].strip(),
            venue_id=vid,
            bbg_venue=r["BBGVenueCode"].strip(),
            bbg_composite=r["BBGComposite"].strip(),
            cutoff=cutoff,
            ref_price=r["RefPrice"].strip(),
            tick_source=r["TickSource"].strip(),
            min_price=(_decimal(raw_min, f"markets.csv {vid} MinPrice")
                       if raw_min else None),
            rounding=r["Rounding"].strip())
    if not venues:
        raise ConfigError("markets.csv defines no venues")

    band_map = {}
    for r in rows("bands.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid not in venues:
            raise ConfigError(
                f"bands.csv: venue {vid} is not defined in markets.csv")
        kind = r["Kind"].strip()
        if kind not in VALID_KIND:
            raise ConfigError(
                f"bands.csv {vid}: Kind {kind!r} is not one of {VALID_KIND}")
        band_map.setdefault(vid, []).append(bands.Tier(
            kind=kind,
            sym_prefix=r["SymPrefix"].strip(),
            floor_from=_decimal(r["FloorFrom"], f"bands.csv {vid} FloorFrom"),
            up=_decimal(r["Up"], f"bands.csv {vid} Up"),
            down=_decimal(r["Down"], f"bands.csv {vid} Down")))

    tick_rows = {}
    for r in rows("ticks.csv"):
        vid = r["FidessaVenueID"].strip()
        if vid not in venues:
            raise ConfigError(
                f"ticks.csv: venue {vid} is not defined in markets.csv")
        tick_rows.setdefault(vid, []).append(r)

    tick_map = {}
    for vid, v in venues.items():
        if vid not in band_map:
            raise ConfigError(f"{vid} has no band tiers in bands.csv")
        if v.tick_source == "config":
            if vid not in tick_rows:
                raise ConfigError(
                    f"{vid} has TickSource=config but no rows in ticks.csv")
            tick_map[vid] = ticks.parse_rows(tick_rows[vid])
        else:
            path = Path(tsr_dir) / v.tick_source
            if not path.is_file():
                raise ConfigError(f"{vid}: tick file {path} does not exist")
            tick_map[vid] = ticks.parse_tsr(path.read_text(encoding="utf-8"))
        if not tick_map[vid]:
            raise ConfigError(f"{vid}: tick table is empty")

    return Config(venues=venues, bands=band_map, ticks=tick_map)


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            fn()
            got = "no exception"
        except ConfigError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    MK = ("Country,FidessaVenueID,BBGVenueCode,BBGComposite,Time,RefPrice,"
          "TickSource,MinPrice,Rounding\n"
          "Indonesia,JKT-MAIN,IJ,IJ,07:59:00,close_print,spol_JKT.tsr,50,"
          "inward\n")
    BD = ("FidessaVenueID,Kind,SymPrefix,FloorFrom,Up,Down\n"
          "JKT-MAIN,pct,,50,0.35,0.35\n")
    TK = "FidessaVenueID,FloorFrom,Tick\n"
    TSR = "SPOL_JKT 0 1\n"

    def write(d, mk=MK, bd=BD, tk=TK, tsr=TSR):
        cfg = Path(d) / "config"
        cfg.mkdir(exist_ok=True)
        (cfg / "markets.csv").write_text(mk, encoding="utf-8")
        (cfg / "bands.csv").write_text(bd, encoding="utf-8")
        (cfg / "ticks.csv").write_text(tk, encoding="utf-8")
        (Path(d) / "spol_JKT.tsr").write_text(tsr, encoding="utf-8")
        return cfg, Path(d)

    print("marketcfg --self-test\n\na good config")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d)
        c = load(cfg, tsrd)
        v = c.venues["JKT-MAIN"]
        check("the venue is keyed by FidessaVenueID, never by BBG code",
              list(c.venues), ["JKT-MAIN"])
        check("cutoff parses to a time", v.cutoff, time(7, 59, 0))
        check("min price is a Decimal", v.min_price, Decimal("50"))
        check("the bbg code is kept as an attribute", v.bbg_venue, "IJ")
        check("one band tier", len(c.bands["JKT-MAIN"]), 1)
        check("the tier carries fractions, not multipliers",
              c.bands["JKT-MAIN"][0].up, Decimal("0.35"))
        check("the .tsr ladder was read for this venue",
              c.ticks["JKT-MAIN"], [(Decimal("0"), Decimal("1"))])

    print("\nblank MinPrice means no floor")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace(",50,inward", ",,inward"))
        check("no minimum", load(cfg, tsrd).venues["JKT-MAIN"].min_price, None)

    print("\nconfig that must be refused")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("inward", "sideways"))
        raises("an unimplemented rounding mode", lambda: load(cfg, tsrd),
               "Rounding")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("close_print", "crystal_ball"))
        raises("an unknown reference price source", lambda: load(cfg, tsrd),
               "RefPrice")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd=BD.replace("JKT-MAIN", "MARS-MAIN"))
        raises("a band tier for a venue markets.csv does not define",
               lambda: load(cfg, tsrd), "MARS-MAIN")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd="FidessaVenueID,Kind,SymPrefix,FloorFrom,"
                                "Up,Down\n")
        raises("a venue with no band tiers at all", lambda: load(cfg, tsrd),
               "no band tiers")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, bd=BD.replace("pct", "vibes"))
        raises("an unknown tier kind", lambda: load(cfg, tsrd), "Kind")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK + "Indonesia,JKT-MAIN,IJ,IJ,07:59:00,"
                                     "close_print,config,,inward\n")
        raises("the same venue defined twice", lambda: load(cfg, tsrd),
               "duplicate")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("spol_JKT.tsr", "config"))
        raises("TickSource=config with no ticks.csv rows",
               lambda: load(cfg, tsrd), "no rows in ticks.csv")
    with tempfile.TemporaryDirectory() as d:
        cfg, tsrd = write(d, mk=MK.replace("spol_JKT.tsr", "missing.tsr"))
        raises("a tick file that is not there", lambda: load(cfg, tsrd),
               "does not exist")

    print("\nthe real shipped config")
    here = Path(__file__).resolve().parent
    real = load(here / "config", here / "config")
    check("twelve venues", len(real.venues), 12)
    check("Indonesia has three band tiers",
          len(real.bands["JKT-MAIN"]), 3)
    check("China's Connect venues are configured like their onshore twins",
          [t.up for t in real.bands["SSC-MAIN"]],
          [t.up for t in real.bands["SHA-MAIN"]])
    check("KOE-MAIN is KQ, which looks swapped and is not",
          real.venues["KOE-MAIN"].bbg_venue, "KQ")
    check("KSC-MAIN is KP", real.venues["KSC-MAIN"].bbg_venue, "KP")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
