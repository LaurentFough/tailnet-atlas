"""Offline public-IP -> country lookup.

Reads the DB-IP "IP to Country Lite" CSV (https://db-ip.com/db/download/ip-to-country-lite,
CC BY 4.0 — the page credits it wherever the data is shown). Lookups are local:
no address ever leaves this machine, only the database file is downloaded
(see scripts/fetch-geoip.py). The file is optional: without it every function
here quietly reports "unknown" and the map simply shows no flags.

    country("203.0.113.7")  -> "PT" | None
    available()             -> True once a database is loaded

Format: one range per line, `start_ip,end_ip,CC` (IPv4 and IPv6 mixed, sorted),
optionally gzipped. Python's stdlib only; the ranges are packed into `array`s
(about 8 bytes per bound) so ~700k rows stay small. IPv6 ranges are keyed on
their top 64 bits, which is enough: nothing is allocated smaller than a /64.

The file's mtime and size are checked on every lookup, so replacing the mounted
file (a monthly refresh) takes effect without restarting the app.
"""
from __future__ import annotations

import array
import bisect
import csv
import gzip
import ipaddress
import logging
import os
import threading

log = logging.getLogger("geo")

PATH = os.environ.get("GEOIP_CSV", "/app/geo/dbip-country-lite.csv.gz")
CREDIT = ('<a href="https://db-ip.com" style="color:inherit">'
          'IP Geolocation by DB-IP</a> (CC BY 4.0)')
UNKNOWN = {"ZZ", "XX", ""}          # DB-IP / ISO "no country" placeholders

_lock = threading.Lock()
_state: dict = {"mtime": None, "db": None, "warned": False}


class _DB:
    __slots__ = ("v4", "v6", "codes")

    def __init__(self):
        # per family: starts, ends, index into `codes`
        self.v4 = (array.array("Q"), array.array("Q"), array.array("H"))
        self.v6 = (array.array("Q"), array.array("Q"), array.array("H"))
        self.codes: list[str] = []


def _open(path: str):
    return (gzip.open(path, "rt", encoding="utf-8", newline="")
            if path.endswith(".gz") else open(path, encoding="utf-8", newline=""))


def _load(path: str) -> _DB:
    db, idx, rows = _DB(), {}, 0
    with _open(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 3:
                continue
            try:
                a, b = ipaddress.ip_address(row[0]), ipaddress.ip_address(row[1])
            except ValueError:
                continue                    # header or junk line
            cc = row[2].strip().upper()
            if cc in UNKNOWN or len(cc) != 2:
                continue
            ci = idx.setdefault(cc, len(idx))
            if ci == len(db.codes):
                db.codes.append(cc)
            fam, shift = (db.v4, 0) if a.version == 4 else (db.v6, 64)
            fam[0].append(int(a) >> shift)
            fam[1].append(int(b) >> shift)
            fam[2].append(ci)
            rows += 1
    log.info("loaded %d country ranges from %s", rows, path)
    return db


def _current() -> _DB | None:
    """The loaded database, reloading it if the file appeared or changed."""
    try:
        st = os.stat(PATH)
        # size as well as mtime: two writes inside one timestamp tick (a quick
        # replace, a coarse filesystem clock) would otherwise look unchanged
        mtime = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            if not _state["warned"]:
                log.info("no GeoIP file at %s — sites will not get flags "
                         "(run scripts/fetch-geoip.py)", PATH)
                _state["warned"] = True
            _state["db"], _state["mtime"] = None, None
        return None
    with _lock:
        if _state["mtime"] != mtime:
            try:
                _state["db"], _state["mtime"] = _load(PATH), mtime
                _state["warned"] = False
            except Exception as e:          # corrupt / half-written download
                log.warning("could not read %s: %s", PATH, e)
                _state["db"], _state["mtime"] = None, mtime
        return _state["db"]


def available() -> bool:
    return _current() is not None


def country(ip: str) -> str | None:
    """ISO 3166-1 alpha-2 code for a public address, or None if unknown."""
    db = _current()
    if db is None:
        return None
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    starts, ends, ci = db.v4 if a.version == 4 else db.v6
    key = int(a) >> (0 if a.version == 4 else 64)
    i = bisect.bisect_right(starts, key) - 1
    if i >= 0 and key <= ends[i]:
        return db.codes[ci[i]]
    return None


def flag(cc: str) -> str:
    """'PT' -> the flag emoji (two regional-indicator letters)."""
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper()) if len(cc) == 2 else ""
