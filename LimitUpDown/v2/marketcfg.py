#!/usr/bin/env python3
"""Load and validate the config, and enforce the split.

THE SPLIT IS THE POINT.  The universe divides in two: Indonesia is
computed from a tier table and a tick ladder, everything else comes from
Bloomberg.  That division lives here, as one column:

    Source=bloomberg   ask B-PIPE for MIN_LIMIT and MAX_LIMIT
    Source=computed    band = f(previous close, tiers), rounded to the tick

A column rather than a branch means a second computed market - or Indonesia
moving to Bloomberg, if Bloomberg turns out to price it - is an edit, not a
patch.

VALIDATION IS STRICT AND LOUD, and most of it exists to catch a venue that
is half configured.  A bloomberg venue carrying a tick file, or a computed
venue with no tiers, is somebody's half-finished edit; both would otherwise
surface as a market silently missing from a production feed.

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

VALID_SOURCE = ("bloomberg", "computed")
VALID_ROUNDING = ("none", "inward", "outward", "nearest")
VALID_KIND = ("pct", "abs")

#  Columns a bloomberg venue must leave blank - it has no arithmetic to do.
COMPUTED_ONLY = ("TickSource", "MinPrice", "Rounding")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Venue:
    country: str
    venue_id: str
    cutoff: time
    source: str
    tick_source: str
    min_price: Optional[Decimal]
    rounding: str
    bbg_composite: str = ""

    @property
    def computed(self) -> bool:
        return self.source == "computed"


@dataclass(frozen=True)
class Config:
    venues: dict
    bands: dict
    ticks: dict

    def by_source(self, rows):
        """rows -> (the ones Bloomberg prices, the ones we compute).

        Every row's venue must be configured, which crosscode.load already
        guarantees - it drops anything whose FidessaMarket is not in
        markets.csv.  Deliberately a KeyError rather than a silent skip: a
        row that reached here with an unknown venue is a bug upstream, and
        losing it quietly would be a market missing from the feed."""
        ask, compute = [], []
        for r in rows:
            (compute if self.venues[r.venue_id].computed else ask).append(r)
        return ask, compute


def _decimal(value: str, what: str) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ConfigError(f"{what}: {value!r} is not a number")


def _rows(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path} does not exist")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        first = (reader.fieldnames or [""])[0]
        #  A comment is a line that STARTS with #, which after parsing means
        #  the FIRST column does - not the venue column.  The shipped files
        #  use this to carry their notes.
        return [r for r in reader
                if not (r.get(first) or "").strip().startswith("#")]


def load(config_dir, tsr_dir=None) -> Config:
    config_dir = Path(config_dir)
    tsr_dir = Path(tsr_dir) if tsr_dir else config_dir

    venues = {}
    for r in _rows(config_dir / "markets.csv"):
        vid = (r.get("FidessaVenueID") or "").strip()
        if not vid:
            continue
        if vid in venues:
            raise ConfigError(f"markets.csv: duplicate venue {vid}")

        source = (r.get("Source") or "").strip().lower()
        if source not in VALID_SOURCE:
            raise ConfigError(
                f"markets.csv {vid}: Source {source!r} is not one of "
                f"{VALID_SOURCE}")

        raw_time = (r.get("Time") or "").strip()
        try:
            hh, mm, ss = (int(x) for x in raw_time.split(":"))
            cutoff = time(hh, mm, ss)
        except ValueError:
            raise ConfigError(
                f"markets.csv {vid}: Time {raw_time!r} is not HH:MM:SS")

        tick_source = (r.get("TickSource") or "").strip()
        raw_min = (r.get("MinPrice") or "").strip()
        rounding = (r.get("Rounding") or "").strip()

        #  EVERY VENUE IS SWITCHABLE.  Flipping Source to computed must be a
        #  one-word edit, so the rounding and tick settings are allowed to
        #  sit on a bloomberg venue unused, ready for the day it flips.
        #  They are still VALIDATED - a latent setting that is wrong is a
        #  trap that springs on whoever makes the switch, months later.
        if rounding and rounding not in VALID_ROUNDING:
            raise ConfigError(
                f"markets.csv {vid}: Rounding {rounding!r} is not one of "
                f"{VALID_ROUNDING}")
        #  Blank means none, which is what most markets want: a percentage
        #  band and no tick to land on.
        effective = rounding or "none"
        if effective == "none" and tick_source:
            raise ConfigError(
                f"markets.csv {vid}: Rounding=none but TickSource is "
                f"{tick_source!r}. A venue that does not round must not name "
                f"a tick table.")
        if effective != "none" and not tick_source:
            raise ConfigError(
                f"markets.csv {vid}: Rounding={rounding} needs a TickSource, "
                f"which is blank")

        venues[vid] = Venue(
            country=(r.get("Country") or "").strip(),
            venue_id=vid, cutoff=cutoff, source=source,
            bbg_composite=(r.get("BBGComposite") or "").strip(),
            tick_source=tick_source,
            min_price=(_decimal(raw_min, f"markets.csv {vid} MinPrice")
                       if raw_min else None),
            rounding=rounding or "none")

    if not venues:
        raise ConfigError(f"{config_dir / 'markets.csv'} defines no venues")

    #  ALWAYS read, even when Bloomberg prices everything today.  The tiers
    #  for a bloomberg venue are what make it switchable, and validating
    #  them now means the switch cannot fail on a typo written months ago.
    band_map = {}
    if (config_dir / "bands.csv").is_file():
        for r in _rows(config_dir / "bands.csv"):
            vid = (r.get("FidessaVenueID") or "").strip()
            if not vid:
                continue
            if vid not in venues:
                raise ConfigError(
                    f"bands.csv: venue {vid} is not defined in markets.csv")
            kind = (r.get("Kind") or "").strip()
            if kind not in VALID_KIND:
                raise ConfigError(
                    f"bands.csv {vid}: Kind {kind!r} is not one of "
                    f"{VALID_KIND}")
            band_map.setdefault(vid, []).append(bands.Tier(
                kind=kind,
                sym_prefix=(r.get("SymPrefix") or "").strip(),
                floor_from=_decimal(r["FloorFrom"],
                                    f"bands.csv {vid} FloorFrom"),
                up=_decimal(r["Up"], f"bands.csv {vid} Up"),
                down=_decimal(r["Down"], f"bands.csv {vid} Down")))

    tick_map = {}
    for vid, v in venues.items():
        if v.computed and vid not in band_map:
            raise ConfigError(
                f"{vid} has Source=computed but no band tiers in bands.csv. "
                f"Add its tiers, or leave it on Source=bloomberg - a market "
                f"whose rule nobody has written down cannot be computed.")
        if v.rounding == "none" or not v.tick_source:
            continue
        path = tsr_dir / v.tick_source
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
            got = repr(fn())
        except ConfigError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    HDR = "Country,FidessaVenueID,Time,Source,TickSource,MinPrice,Rounding\n"
    IDN = "Indonesia,JKT-MAIN,07:59:00,computed,spol_JKT.tsr,50,inward\n"
    BBG = "China,SHA-MAIN,09:03:00,bloomberg,,,\n"
    BD = ("FidessaVenueID,Kind,SymPrefix,FloorFrom,Up,Down\n"
          "JKT-MAIN,pct,,50,0.35,0.35\n")
    TSR = "SPOL_JKT 0 1\nSPOL_JKT 200 2\n"

    def write(d, mk=HDR + IDN + BBG, bd=BD, tsr=TSR):
        cfg = Path(d) / "config"
        cfg.mkdir(exist_ok=True)
        (cfg / "markets.csv").write_text(mk, encoding="utf-8")
        (cfg / "bands.csv").write_text(bd, encoding="utf-8")
        (cfg / "spol_JKT.tsr").write_text(tsr, encoding="utf-8")
        return cfg

    print("marketcfg --self-test\n\na good config")
    with tempfile.TemporaryDirectory() as d:
        c = load(write(d))
        check("both venues", sorted(c.venues), ["JKT-MAIN", "SHA-MAIN"])
        check("Indonesia is the computed one",
              c.venues["JKT-MAIN"].computed, True)
        check("China is not", c.venues["SHA-MAIN"].computed, False)
        check("the min price is a Decimal", c.venues["JKT-MAIN"].min_price,
              Decimal("50"))
        check("only the computed venue has tiers", list(c.bands), ["JKT-MAIN"])
        check("and a tick ladder", c.ticks["JKT-MAIN"],
              [(Decimal("0"), Decimal("1")), (Decimal("200"), Decimal("2"))])

    print("\nsplitting a universe by source")
    class R:
        def __init__(self, venue_id):
            self.venue_id = venue_id

    with tempfile.TemporaryDirectory() as d:
        c = load(write(d))
        ask, compute = c.by_source([R("SHA-MAIN"), R("JKT-MAIN"),
                                    R("SHA-MAIN")])
        check("China goes to Bloomberg", [r.venue_id for r in ask],
              ["SHA-MAIN", "SHA-MAIN"])
        check("Indonesia is computed here",
              [r.venue_id for r in compute], ["JKT-MAIN"])

    print("\nhalf configured venues, which are all somebody's half done edit")
    with tempfile.TemporaryDirectory() as d:
        raises("a tick file with no Rounding to use it - naming a ladder "
               "and not saying how to land on it is a half-finished edit",
               lambda: load(write(d, mk=HDR + IDN +
                                  "China,SHA-MAIN,09:03:00,bloomberg,"
                                  "spol_JKT.tsr,,\n")),
               "must not name a tick table")
    with tempfile.TemporaryDirectory() as d:
        cfg = load(write(d, mk=HDR + IDN +
                         "China,SHA-MAIN,09:03:00,bloomberg,,50,\n"))
        check("a MinPrice on a bloomberg venue is LATENT, not an "
              "error - it is what makes the venue switchable in one "
              "word", cfg.venues["SHA-MAIN"].min_price, Decimal("50"))
    with tempfile.TemporaryDirectory() as d:
        raises("a computed venue with no tiers",
               lambda: load(write(d, bd="FidessaVenueID,Kind,SymPrefix,"
                                        "FloorFrom,Up,Down\n")),
               "no band tiers")
    with tempfile.TemporaryDirectory() as d:
        cfg = load(write(d, bd=BD + "SHA-MAIN,pct,,0,0.1,0.1\n"))
        check("tiers for a venue Bloomberg prices today are LOADED "
              "and VALIDATED, not rejected - they are the switch, and "
              "a typo found now beats one found by whoever flips it",
              [t.up for t in cfg.bands["SHA-MAIN"]], [Decimal("0.1")])
    with tempfile.TemporaryDirectory() as d:
        raises("a computed venue that rounds but names no tick file",
               lambda: load(write(d, mk=HDR +
                                  "Indonesia,JKT-MAIN,07:59:00,computed,,50,"
                                  "inward\n")),
               "needs a TickSource")
    with tempfile.TemporaryDirectory() as d:
        raises("a computed venue that does not round but names one anyway",
               lambda: load(write(d, mk=HDR +
                                  "Indonesia,JKT-MAIN,07:59:00,computed,"
                                  "spol_JKT.tsr,50,none\n")),
               "must not name a tick table")
    with tempfile.TemporaryDirectory() as d:
        raises("a tick file that is not there",
               lambda: load(write(d, mk=HDR + IDN.replace("spol_JKT.tsr",
                                                          "missing.tsr"))),
               "does not exist")
    with tempfile.TemporaryDirectory() as d:
        raises("an unknown source", lambda: load(write(
            d, mk=HDR + "X,X-MAIN,07:00:00,telepathy,,,\n")), "Source")
    with tempfile.TemporaryDirectory() as d:
        raises("the same venue twice",
               lambda: load(write(d, mk=HDR + IDN + IDN)), "duplicate")
    with tempfile.TemporaryDirectory() as d:
        raises("a time that is not a time",
               lambda: load(write(d, mk=HDR + IDN.replace("07:59:00",
                                                          "soon"))),
               "not HH:MM:SS")

    print("\na config where Bloomberg prices everything")
    with tempfile.TemporaryDirectory() as d:
        cfg = write(d, mk=HDR + BBG)
        (cfg / "bands.csv").unlink()
        c = load(cfg)
        check("needs no bands.csv at all, and does not go looking",
              (c.bands, c.ticks), ({}, {}))

    print("\nthe real shipped config")
    real = load(Path(__file__).resolve().parent / "config")
    check("fifteen venues", len(real.venues), 15)
    check("seven countries in scope",
          sorted({v.country for v in real.venues.values()}),
          ["China", "Indonesia", "Japan", "Korea", "Malaysia", "Philippines",
           "Taiwan"])
    check("Indonesia is the only computed one",
          [v.venue_id for v in real.venues.values() if v.computed],
          ["JKT-MAIN"])
    check("with three tiers", len(real.bands["JKT-MAIN"]), 3)
    check("the 50 rupiah floor", real.venues["JKT-MAIN"].min_price,
          Decimal("50"))
    check("rounding inward",
          real.venues["JKT-MAIN"].rounding, "inward")
    check("Japan is the Tokyo listing plus both PTS venues",
          sorted(k for k, v in real.venues.items() if v.country == "Japan"),
          ["CHJ-MAIN", "JNX-MAIN", "TYO-MAIN"])
    check("all three Japanese venues share Korea's 07:30 cutoff",
          {v.cutoff for v in real.venues.values() if v.country == "Japan"},
          {time(7, 30)})
    check("Thailand and India are OUT, and not by accident - each needs a "
          "filter that is not written here",
          [k for k in real.venues
           if k in ("SET-MAIN", "NSI-MAIN", "BSE-MAIN", "BSE-SECONDARY")],
          [])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
