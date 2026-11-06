#!/usr/bin/env python3
"""Download the DB-IP "IP to Country Lite" database for the map's country flags.

    python3 scripts/fetch-geoip.py                 # -> data/geo/dbip-country-lite.csv.gz
    python3 scripts/fetch-geoip.py --out other.gz

DB-IP publishes a new file every month (CC BY 4.0 — the page credits DB-IP
automatically whenever the file is loaded). This tries the current month and
falls back to the previous one, since a new month's file can lag by a day or
two. Only the database is downloaded; the app looks addresses up locally and
no address of yours is ever sent anywhere.

compose mounts ./data/geo read-only into the app, which reloads the file when
it changes — so re-running this monthly (cron is fine) needs no restart.
Standard library only.
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
URL = "https://download.db-ip.com/free/dbip-country-lite-{ym}.csv.gz"


def months(n: int = 3):
    d = datetime.date.today().replace(day=1)
    for _ in range(n):
        yield d.strftime("%Y-%m")
        d = (d - datetime.timedelta(days=1)).replace(day=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "data" / "geo" / "dbip-country-lite.csv.gz"))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = None
    for ym in months():
        url = URL.format(ym=ym)
        try:
            print(f"trying {url}")
            fd, tmp = tempfile.mkstemp(dir=out.parent, suffix=".part")
            # db-ip.com answers 403 to urllib's default User-Agent
            req = urllib.request.Request(url, headers={"User-Agent": "tailnet-atlas-fetch/1.0"})
            with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(req, timeout=60) as r:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            break
        except urllib.error.HTTPError as e:
            if tmp:
                pathlib.Path(tmp).unlink(missing_ok=True)
            tmp = None
            if e.code != 404:
                print(f"fetch-geoip: {e.code} {e.reason}", file=sys.stderr)
                return 1
        except urllib.error.URLError as e:
            if tmp:
                pathlib.Path(tmp).unlink(missing_ok=True)
            print(f"fetch-geoip: could not reach db-ip.com: {e.reason}", file=sys.stderr)
            return 1
    else:
        print("fetch-geoip: no file found for the last 3 months", file=sys.stderr)
        return 1

    # Do not replace a good file with a broken download.
    try:
        rows, sample = 0, None
        with gzip.open(tmp, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) >= 3:
                    rows += 1
                    sample = sample or row
        if rows < 1000:
            raise ValueError(f"only {rows} rows")
    except Exception as e:
        pathlib.Path(tmp).unlink(missing_ok=True)
        print(f"fetch-geoip: downloaded file is not usable ({e}); kept the old one",
              file=sys.stderr)
        return 1
    os.replace(tmp, out)            # atomic: the app never sees a half-written file
    print(f"ok: {rows} ranges -> {out}  (e.g. {','.join(sample)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
