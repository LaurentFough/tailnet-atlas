"""Offline public-IP -> network (ASN) lookup.

Reads iptoasn.com's "ip2asn-combined" file (public domain, PDDL 1.0 — no
attribution needed): tab-separated `start  end  ASN  country  "HANDLE - Org"`,
IPv4 and IPv6 mixed, optionally gzipped. Same shape as geo.py and the same
promises: lookups are local, the file is optional (without it everything here
says "unknown"), and replacing the file takes effect without a restart.

    lookup("203.0.113.7")   -> (3243, "MEO-INTERNET - MEO - SERVICOS ...") | None
    org("HANDLE - Org Ltd") -> "Org Ltd"
    looks_mobile(desc)      -> True for networks whose NAME says mobile

`looks_mobile` is only a hint. Many carriers run fixed and mobile service on one
ASN, and plenty of mobile networks never say so in their name ("Vodafone Idea"),
so it can neither prove a network is mobile nor that it is not — the config's
`mobile_asns` / `not_mobile_asns` lists are the authority (see render.py).
"""
from __future__ import annotations

import array
import bisect
import csv
import gzip
import ipaddress
import logging
import os
import re
import threading

log = logging.getLogger("asn")

PATH = os.environ.get("ASN_TSV", "/app/geo/ip2asn-combined.tsv.gz")

# Whole-word matches only: "TOT-MOBILE-AS-AP" yes, "EXXONMOBIL" no. "tmobile" is
# one word in practice, so it is listed on its own. Deliberately NOT "wireless":
# fixed-wireless ISPs are common in rural areas and are not roaming.
_MOBILE = re.compile(
    r"(?<![a-z0-9])(mobile|mobil|mobility|cellular|gsm|lte|movil|mobilfunk)(?![a-z0-9])|tmobile")

_lock = threading.Lock()
_state: dict = {"sig": None, "db": None, "warned": False}


class _DB:
    __slots__ = ("v4", "v6", "names")

    def __init__(self):
        self.v4 = (array.array("Q"), array.array("Q"), array.array("I"))
        self.v6 = (array.array("Q"), array.array("Q"), array.array("I"))
        self.names: dict[int, str] = {}


def _open(path: str):
    return (gzip.open(path, "rt", encoding="utf-8", newline="")
            if path.endswith(".gz") else open(path, encoding="utf-8", newline=""))


def _load(path: str) -> _DB:
    db, rows = _DB(), 0
    with _open(path) as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if len(row) < 5:
                continue
            try:
                n = int(row[2])
                a, b = ipaddress.ip_address(row[0]), ipaddress.ip_address(row[1])
            except ValueError:
                continue
            if n == 0:                      # "Not routed"
                continue
            db.names.setdefault(n, row[4].strip())
            fam, shift = (db.v4, 0) if a.version == 4 else (db.v6, 64)
            fam[0].append(int(a) >> shift)
            fam[1].append(int(b) >> shift)
            fam[2].append(n)
            rows += 1
    log.info("loaded %d network ranges from %s", rows, path)
    return db


def _current() -> _DB | None:
    try:
        st = os.stat(PATH)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            if not _state["warned"]:
                log.info("no ASN file at %s — mobile networks will not be recognised "
                         "(run scripts/fetch-geoip.py)", PATH)
                _state["warned"] = True
            _state["db"], _state["sig"] = None, None
        return None
    with _lock:
        if _state["sig"] != sig:
            try:
                _state["db"], _state["sig"] = _load(PATH), sig
                _state["warned"] = False
            except Exception as e:          # corrupt / half-written download
                log.warning("could not read %s: %s", PATH, e)
                _state["db"], _state["sig"] = None, sig
        return _state["db"]


def available() -> bool:
    return _current() is not None


def lookup(ip: str) -> tuple[int, str] | None:
    """(ASN, "HANDLE - Org name") for a public address, or None if unknown."""
    db = _current()
    if db is None:
        return None
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    starts, ends, asns = db.v4 if a.version == 4 else db.v6
    key = int(a) >> (0 if a.version == 4 else 64)
    i = bisect.bisect_right(starts, key) - 1
    if i >= 0 and key <= ends[i]:
        return asns[i], db.names.get(asns[i], "")
    return None


def org(desc: str) -> str:
    """'TOT-MOBILE-AS-AP - TOT Public Company Limited' -> 'TOT Public Company Limited'."""
    return desc.split(" - ", 1)[1].strip() if " - " in desc else desc.strip()


def looks_mobile(desc: str) -> bool:
    return bool(_MOBILE.search(desc.lower()))
