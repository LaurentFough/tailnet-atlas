#!/usr/bin/env python3
"""Check (and optionally clean up) leftover Tailscale nodes before `docker compose up`.

Why this exists: when a node named `tatlas` is still registered in your tailnet
— say the sidecar's state was lost, or an old container was removed without
logging out — the next node that asks for that name is renamed `tatlas-1`,
then `tatlas-2`, and so on. The cert and `tailscale serve` then live on the
suffixed name while you keep browsing to the original name, which is the dead
node. Nothing in the app is broken; the name is just taken.

    python3 scripts/preflight.py                 # report only
    python3 scripts/preflight.py --delete        # offer to delete offline leftovers
    python3 scripts/preflight.py --strict        # exit 1 if any are found (for `&&` chains)

Reads TS_HOSTNAME from the environment or .env, and the OAuth client of the
tailnet the sidecar joins (first [[tailnet]] in tailnets.toml, or --tailnet SLUG).
Listing needs only the read-only `devices:core:read` scope the app already has.
--delete additionally needs the `devices:core` WRITE scope on that client; the
script says so if the API refuses.

Standard library only, so it runs on the host without the app's dependencies.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.tailscale.com/api/v2"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env(path: pathlib.Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, optional quotes, # comments."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_manifest(path: pathlib.Path) -> list[dict]:
    text = path.read_text()
    try:
        import tomllib
        return tomllib.loads(text).get("tailnet") or []
    except ImportError:  # Python < 3.11 on the host: read just the keys we need
        entries = []
        for block in re.split(r"^\s*\[\[tailnet\]\]", text, flags=re.M)[1:]:
            e = {}
            for key in ("slug", "oauth_id_env", "oauth_secret_env"):
                m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', block, flags=re.M)
                if m:
                    e[key] = m.group(1)
            entries.append(e)
        return entries


def call(method: str, url: str, token: str | None = None, data: bytes | None = None,
         basic: tuple[str, str] | None = None):
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if basic:
        req.add_header("Authorization", "Basic " + base64.b64encode(
            f"{basic[0]}:{basic[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else {}


def die(msg: str, code: int = 2):
    print(f"preflight: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tailnet", help="slug in tailnets.toml of the tailnet the sidecar joins "
                                      "(default: the first entry)")
    ap.add_argument("--hostname", help="node name to check (default: TS_HOSTNAME)")
    ap.add_argument("--delete", action="store_true",
                    help="offer to delete leftover OFFLINE nodes (asks per node)")
    ap.add_argument("--yes", action="store_true", help="with --delete: don't ask")
    ap.add_argument("--strict", action="store_true", help="exit 1 if leftovers are found")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--manifest", default=str(ROOT / "tailnets.toml"))
    a = ap.parse_args()

    env = {**load_env(pathlib.Path(a.env)), **os.environ}
    host = (a.hostname or env.get("TS_HOSTNAME", "")).strip()
    if not host:
        die("no hostname: set TS_HOSTNAME in .env or pass --hostname")

    mpath = pathlib.Path(a.manifest)
    if not mpath.is_file():
        die(f"no manifest at {mpath} (cp tailnets.example.toml tailnets.toml)")
    entries = load_manifest(mpath)
    if a.tailnet:
        entries = [e for e in entries if e.get("slug") == a.tailnet]
    if not entries:
        die("no matching [[tailnet]] entry")
    e = entries[0]
    cid, sec = env.get(e.get("oauth_id_env", ""), ""), env.get(e.get("oauth_secret_env", ""), "")
    if not cid or not sec:
        die(f"{e.get('oauth_id_env')}/{e.get('oauth_secret_env')} not set for "
            f"tailnet '{e.get('slug')}'")

    try:
        tok = call("POST", f"{API}/oauth/token", basic=(cid, sec),
                   data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
                   )["access_token"]
        devices = call("GET", f"{API}/tailnet/-/devices?fields=all", token=tok).get("devices", [])
    except urllib.error.HTTPError as ex:
        die(f"Tailscale API said {ex.code} {ex.reason} for {ex.url}")
    except urllib.error.URLError as ex:
        die(f"could not reach the Tailscale API: {ex.reason}")

    # "tatlas", or "tatlas-1", "tatlas-2", ... — the pattern Tailscale uses for name clashes
    pat = re.compile(rf"^{re.escape(host.lower())}(-\d+)?$")
    hits = [d for d in devices if pat.match((d.get("name") or "").split(".")[0].lower())]

    print(f"tailnet '{e.get('slug')}': {len(devices)} devices, "
          f"{len(hits)} named '{host}' or '{host}-N'")
    if not hits:
        print("ok — the name is free.")
        return 0

    for d in sorted(hits, key=lambda d: d.get("name", "")):
        state = "ONLINE" if d.get("connectedToControl") else "offline"
        print(f"  {d.get('name'):45} {state:8} last seen {d.get('lastSeen', '?')}  "
              f"id={d.get('nodeId') or d.get('id')}")

    exact_free = not any((d.get("name") or "").split(".")[0].lower() == host.lower() for d in hits)
    if len(hits) > 1 or not exact_free:
        print(f"\nA fresh node asking for '{host}' would be renamed '{host}-N' while any of "
              "these still exist.\nIf the sidecar's state in data/tailscale is intact, the "
              "ONLINE entry is yours — leave it; remove only stale offline ones.")

    if not a.delete:
        print("\nNothing changed. Re-run with --delete to remove offline leftovers, or "
              "delete them in the admin console.")
        return 1 if a.strict else 0

    removed = 0
    for d in hits:
        name, did = d.get("name"), d.get("nodeId") or d.get("id")
        if d.get("connectedToControl"):
            print(f"skip {name}: online (delete it by hand if you really mean to)")
            continue
        if not a.yes:
            if not sys.stdin.isatty():
                die("--delete needs a terminal to confirm, or pass --yes")
            if input(f"delete {name} (last seen {d.get('lastSeen', '?')})? [y/N] ").strip().lower() != "y":
                continue
        try:
            call("DELETE", f"{API}/device/{urllib.parse.quote(str(did))}", token=tok)
            print(f"deleted {name}")
            removed += 1
        except urllib.error.HTTPError as ex:
            hint = (" — the OAuth client needs the `devices:core` write scope"
                    if ex.code in (401, 403) else "")
            print(f"could not delete {name}: {ex.code} {ex.reason}{hint}", file=sys.stderr)
    print(f"{removed} node(s) deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
