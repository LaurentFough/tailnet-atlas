#!/usr/bin/env python3
"""Download the two small databases the map can use to read public addresses.

    python3 scripts/fetch-geoip.py              # both -> data/geo/
    python3 scripts/fetch-geoip.py --only geo   # country flags only
    python3 scripts/fetch-geoip.py --only asn   # mobile-carrier detection only
    python3 scripts/fetch-geoip.py --dir /path

  geo  DB-IP "IP to Country Lite"  -> dbip-country-lite.csv.gz   (~5 MB, monthly)
       CC BY 4.0: the page credits DB-IP automatically when the file is loaded.
  asn  iptoasn.com "ip2asn-combined" -> ip2asn-combined.tsv.gz   (~11 MB, hourly)
       Public domain (PDDL 1.0): no attribution needed.

Only the databases are downloaded; the app looks addresses up locally, so no
address of yours is ever sent anywhere. compose mounts ./data/geo read-only into
the app, which reloads either file when it changes — re-running this from cron
needs no restart. Both files are optional: without one, that feature is just off.
A broken download never replaces a good file. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = {"User-Agent": "tailnet-atlas-fetch/1.0"}     # db-ip.com answers 403 to urllib's default


def _months(n: int = 3):
    d = datetime.date.today().replace(day=1)
    for _ in range(n):
        yield d.strftime("%Y-%m")
        d = (d - datetime.timedelta(days=1)).replace(day=1)


SOURCES = {
    "geo": {
        "file": "dbip-country-lite.csv.gz", "delimiter": ",", "min_rows": 1000,
        # a new month's file can lag a day or two, so fall back to the previous ones
        "urls": lambda: [f"https://download.db-ip.com/free/dbip-country-lite-{ym}.csv.gz"
                         for ym in _months()],
    },
    "asn": {
        "file": "ip2asn-combined.tsv.gz", "delimiter": "\t", "min_rows": 1000,
        "urls": lambda: ["https://iptoasn.com/data/ip2asn-combined.tsv.gz"],
    },
}


def fetch(name: str, out_dir: pathlib.Path) -> bool:
    src, dest = SOURCES[name], out_dir / SOURCES[name]["file"]
    tmp = None
    for url in src["urls"]():
        try:
            print(f"[{name}] trying {url}")
            fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".part")
            with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=120) as r:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            break
        except urllib.error.HTTPError as e:
            pathlib.Path(tmp).unlink(missing_ok=True)
            tmp = None
            if e.code != 404:
                print(f"[{name}] {e.code} {e.reason}", file=sys.stderr)
                return False
        except urllib.error.URLError as e:
            if tmp:
                pathlib.Path(tmp).unlink(missing_ok=True)
            print(f"[{name}] could not reach the server: {e.reason}", file=sys.stderr)
            return False
    else:
        print(f"[{name}] no file found", file=sys.stderr)
        return False

    # Do not replace a good file with a broken download.
    try:
        rows, sample = 0, None
        with gzip.open(tmp, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh, delimiter=src["delimiter"]):
                if len(row) >= 3:
                    rows += 1
                    sample = sample or row
        if rows < src["min_rows"]:
            raise ValueError(f"only {rows} rows")
    except Exception as e:
        pathlib.Path(tmp).unlink(missing_ok=True)
        print(f"[{name}] downloaded file is not usable ({e}); kept the old one", file=sys.stderr)
        return False
    os.replace(tmp, dest)           # atomic: the app never sees a half-written file
    print(f"[{name}] ok: {rows} ranges -> {dest}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", choices=sorted(SOURCES), help="fetch just one database")
    ap.add_argument("--dir", default=str(ROOT / "data" / "geo"))
    a = ap.parse_args()
    out = pathlib.Path(a.dir)
    out.mkdir(parents=True, exist_ok=True)
    results = [fetch(n, out) for n in ([a.only] if a.only else SOURCES)]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
