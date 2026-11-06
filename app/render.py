"""Builds the live Tailnet Atlas page from the Tailscale API.

Two things are derived rather than hard-coded, because hard-coding them is
exactly what broke the first version:

* EDGES come from the actual grants in the policy file. The first version
  listed node ids by hand; as soon as ids are generated from live data, none
  of them match and the graph silently loses every line — no error, just a
  still picture.
* SITES/positions come from the address a device was last seen on, so a new
  device lands somewhere sensible instead of disappearing.

Everything site-specific lives in config.toml, not in here. One process can
hold several tailnets at once (see tailnets.toml / TAILNETS_PATH) — each is
a `Tailnet` instance with its own OAuth credentials and site config, read
from environment variables named IN the manifest rather than hard-coded, so
credentials for several orgs can coexist without colliding.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("render")

API = "https://api.tailscale.com/api/v2"
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
TEMPLATE = os.environ.get("TEMPLATE_PATH", "/app/template.html")
MANIFEST = os.environ.get("TAILNETS_PATH", "/app/tailnets.toml")
REFRESH = int(os.environ.get("REFRESH_SECONDS", "300"))

# Two site ids carry meaning in the code and must exist in every config:
#   dock   — everything left inside 172.16/12, i.e. container bridges
#   mobile — the fallback for anything that matched nothing
RESERVED = ("dock", "mobile")

DOCKER_RE = re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\.")
TAG_OWNED = "__tagged__"
KIND = {"tag:client-full": "full", "tag:ha-access": "ha"}
# Tailscale's own "everyone with this platform role" autogroups. A grant
# sourced only from these is structural (true of every device, on every
# tailnet, always) rather than a deliberate share worth flagging — see
# Tailnet.build_access.
STRUCTURAL_SRC = {"autogroup:admin", "autogroup:owner"}
# After the users endpoint answers 404 we stop asking for this long: it is not
# going to start working in the next five minutes, and the page is fine
# without it (see Tailnet.fetch_users / ToDone.md).
USERS_RETRY_AFTER = 24 * 3600

# --- automatic sites (site_mode = "auto") -----------------------------------
# Ranges a phone hotspot / USB tether hands out. A device on one of these is
# roaming by definition, and its PUBLIC address is the carrier's, shared with
# strangers — so it must never anchor or join a site. iOS, Android, Android USB.
HOTSPOT_PREFIXES = ("172.20.10.", "192.168.43.", "192.168.42.")
MOBILE_OS = {"ios", "android"}
# Documentation ranges are not "global" to the ipaddress module, but they are
# what a demo or a test uses as a stand-in for a public address.
_DOC_NETS = [ipaddress.ip_network(n) for n in
             ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
# Region tints handed to auto sites, picked by hash so a site keeps its colour.
SITE_TINTS = ["255,209,102", "178,139,250", "91,140,240", "45,212,191",
              "246,134,120", "134,214,120", "240,160,220", "120,200,240"]
# dock / mobile carry meaning in the code. In auto mode a config may omit them.
AUTO_RESERVED = [
    {"id": "dock", "label": "Docker", "net": "172.16/12 bridges",
     "fx": 0.78, "fy": 0.46, "tint": "91,140,240"},
    {"id": "mobile", "label": "Mobile · roaming", "net": "phones · travel routers",
     "fx": 0.42, "fy": 0.78, "tint": "45,212,191"},
]


# --------------------------------------------------------------- HuJSON
def strip_hujson(text: str) -> str:
    """HuJSON -> JSON. Character-wise, not regex: the policy has comments at
    the END of lines, and a naive `//` rule would also destroy https:// URLs
    inside strings. So string/escape state is tracked."""
    out, i, n = [], 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def parse_acl(text: str) -> dict:
    try:
        return json.loads(strip_hujson(text))
    except json.JSONDecodeError as e:
        log.warning("policy not parseable: %s", e)
        return {}


# --------------------------------------------------------------- devices
def is_own_interface(ip: str) -> bool:
    """True if the device HOSTS this network rather than having joined it.

    Whoever hands out a network holds the gateway address in it. A travel
    router creates its own LAN and sits on .1 there; container bridges on a
    Docker host are the same story. Those addresses travel with the device and
    say nothing about where it is — only the remaining one, the address it was
    GIVEN by the network it plugged into, is its uplink.

    Deliberately NOT using the device's advertised routes for this: a subnet
    router that merely JOINED the network it announces holds a normal host
    address in it, and that test would wrongly strip its real address. The
    gateway convention separates "I created this network" from "I joined this
    network" exactly.
    """
    return ip.endswith(".1")


def is_exact_ip(v: str) -> bool:
    """A `match` value that fully parses as an address (no trailing dot, all
    four octets) pins one exact host. Anything else — typically a prefix
    left deliberately incomplete, like "10.0.8." — matches by prefix, as
    before. This needs no config flag: the two forms are already visually
    distinct, and every existing config already writes them this way."""
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def split_endpoint(e: str) -> str:
    """'1.2.3.4:41641' or '[2001:db8::1]:41641' -> the bare address."""
    return e.rsplit(":", 1)[0].strip("[]").strip()


def is_public(ip: str) -> bool:
    """A routable address on the open internet — what Tailscale learned about a
    device from outside its LAN. Excludes private, CGNAT (100.64/10, which is
    also Tailscale's own range), link-local and loopback."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global or any(a in n for n in _DOC_NETS)


def pub_key(ip: str) -> str:
    """What two devices must share to count as "behind the same uplink": the
    exact IPv4 address, or the /64 for IPv6 (each host has its own /128, the
    household shares the prefix)."""
    a = ipaddress.ip_address(ip)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if a.version == 6 else ip


def role_of(tags: list[str]) -> str:
    t = set(tags or [])
    if "tag:home" in t:
        return "home"
    if "tag:client-full" in t:
        return "full"
    if "tag:client" in t:
        return "client"
    if "tag:server" in t:
        return "server"
    if "tag:exit" in t:
        return "exit"
    if "tag:ha-access" in t:
        return "proxy"
    return "client"


def short_id(name: str, used: set) -> str:
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:10] or "n"
    cand, i = base, 1
    while cand in used:
        i += 1
        cand = f"{base}{i}"
    used.add(cand)
    return cand


def expand(target: str, devs: list[dict], hosts: dict | None = None) -> list[dict]:
    """Which devices does a grant source/target refer to?"""
    if target == "*":
        return list(devs)
    if target.startswith("tag:"):
        return [d for d in devs if target in d["tags"]]
    if target == "autogroup:internet":
        return [d for d in devs if d["exit"]]          # leaves the net via exit nodes
    if target.startswith("host:"):
        # `hosts` aliases a name to one address in the policy file's own
        # `hosts` map — this is exactly how a device gets exposed to one
        # specific person/group by name rather than by tag.
        addr = (hosts or {}).get(target[len("host:"):])
        return [d for d in devs if addr and d["ip"] == addr]
    if re.match(r"^\d+\.\d+\.\d+\.\d+/\d+$", target):
        return [d for d in devs if target in d["routes"]]   # the announcing router
    return []


def chip(v: str) -> str:
    k = v.replace("tag:", "")
    cls = "full" if k == "client-full" else k
    return f'<span class="chip {html.escape(cls)}">{html.escape(k)}</span>'


def multi(v) -> list[str]:
    """A config value that may be absent, one string, a comma-separated
    string, or a TOML array — always normalized to a list of trimmed
    strings. Lets `net`/`match` take either form without a config migration."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in str(v).split(",") if p.strip()]


# --------------------------------------------------------------- Tailnet
@dataclass
class Tailnet:
    """One tailnet's credentials, site config, and rendering — everything
    needed to fetch and draw its map. Several of these can live in one
    process; nothing here is shared global state."""

    slug: str
    cid: str
    secret: str
    cfg: dict = field(repr=False)
    # Optional user-level API key, used ONLY for the users list: that endpoint
    # answers 404 to OAuth tokens whatever their scopes, and answers to an API
    # key. An API key acts as the user who made it (it cannot be scoped down),
    # so it is opt-in and everything else keeps using the OAuth client.
    api_key: str = field(default="", repr=False)

    @classmethod
    def load(cls, slug: str, config_path: str, oauth_id_env: str,
              oauth_secret_env: str, api_key_env: str = "") -> "Tailnet":
        cid = os.environ.get(oauth_id_env, "")
        secret = os.environ.get(oauth_secret_env, "")
        if not cid or not secret:
            raise SystemExit(
                f"tailnets.toml: '{slug}' needs {oauth_id_env} and "
                f"{oauth_secret_env} set (see .env).")
        try:
            with open(config_path, "rb") as fh:
                cfg = tomllib.load(fh)
        except FileNotFoundError:
            raise SystemExit(f"tailnets.toml: '{slug}' has no config at {config_path}")
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{config_path} is not valid TOML: {e}")
        mode = str(cfg.get("site_mode", "manual")).lower()
        if mode not in ("manual", "auto"):
            raise SystemExit(f"{config_path}: site_mode must be \"manual\" or \"auto\", "
                             f"not {cfg.get('site_mode')!r}")
        if mode == "auto":
            # sites are derived, so a config needs no [[site]] blocks at all —
            # but dock/mobile must exist for the code that refers to them.
            have = {x.get("id") for x in cfg.get("site") or []}
            cfg["site"] = list(cfg.get("site") or []) + [
                dict(r) for r in AUTO_RESERVED if r["id"] not in have]
        sites = cfg.get("site") or []
        ids = [s["id"] for s in sites]
        missing = [r for r in RESERVED if r not in ids]
        if missing:
            raise SystemExit(f"{config_path}: missing required site id(s): {missing}")
        if len(set(ids)) != len(ids):
            raise SystemExit(f"{config_path}: duplicate site ids")
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if api_key_env and not api_key:
            log.warning("[%s] %s is named in tailnets.toml but not set — the Owners "
                        "table will be derived from the device list", slug, api_key_env)
        return cls(slug=slug, cid=cid, secret=secret, cfg=cfg, api_key=api_key)

    def __post_init__(self):
        self.tailnet_label = self.cfg.get("tailnet", self.slug)
        self.label_short = self.tailnet_label.split(" · ")[0]
        self.site_order = [s["id"] for s in self.cfg["site"]]
        self.site_meta = {
            s["id"]: {"label": s["label"], "net": ", ".join(multi(s.get("net"))),
                      "fx": float(s.get("fx", .5)), "fy": float(s.get("fy", .5)),
                      "tint": s.get("tint", "127,150,200")}
            for s in self.cfg["site"]
        }
        # Address prefix (or exact host) -> site, one entry per value. `match`
        # may hold several values (a TOML array, or a comma-separated string)
        # for one site — a disjoint set of subnets/hosts that are still "the
        # same place". They all resolve to that one site id, so this never
        # introduces ambiguity: a device still matches exactly one entry and
        # therefore exactly one site.
        #
        # Each value is checked as an EXACT host if it fully parses as an
        # address ("192.168.100.2"), otherwise as a literal PREFIX
        # ("10.0.8."). This matters once two site pin single hosts that
        # happen to share digits: prefix-matching "192.168.100.2" against
        # "192.168.100.240" would wrongly claim it too.
        #
        # Order matters and is taken from the config file: the first match
        # wins, which is how a hotspot range can be claimed before the
        # generic container-bridge rule gets to look at it. Flattening here
        # preserves that — all of one site's values stay grouped at the
        # position its [[site]] block has, relative to every other site.
        self.site_rules = [(m, s["id"], is_exact_ip(m))
                           for s in self.cfg["site"] for m in multi(s.get("match"))]
        # Devices that always count as roaming whatever address they report —
        # a cloud VM has no LAN that would place it anywhere meaningful.
        self.always_mobile = set(self.cfg.get("pinned_roaming") or [])
        # --- automatic sites -------------------------------------------------
        self.auto = str(self.cfg.get("site_mode", "manual")).lower() == "auto"
        self.min_site_devices = max(1, int(self.cfg.get("min_site_devices", 2)))
        self.hotspot_prefixes = tuple(multi(self.cfg.get("hotspot_nets"))) or HOTSPOT_PREFIXES
        # `public = [...]` on a [[site]] pins whichever cluster sits behind
        # that address (or CIDR) to that site, whatever its size or members.
        self.public_pins: list[tuple] = []
        for st in self.cfg["site"]:
            for v in multi(st.get("public")):
                try:
                    self.public_pins.append((ipaddress.ip_network(v, strict=False), st["id"]))
                except ValueError:
                    raise SystemExit(f"[{self.slug}] site '{st['id']}': "
                                     f"public = {v!r} is not an address or CIDR")
        # wall-clock time before which fetch_users() won't call the API again
        self._users_retry_at = 0.0

    # ----------------------------------------------------------- fetch
    def _token(self) -> str:
        r = httpx.post(f"{API}/oauth/token", auth=(self.cid, self.secret),
                       data={"grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        return r.json()["access_token"]

    def fetch(self) -> tuple[list, str]:
        h = {"Authorization": f"Bearer {self._token()}"}
        d = httpx.get(f"{API}/tailnet/-/devices?fields=all", headers=h, timeout=45)
        d.raise_for_status()
        a = httpx.get(f"{API}/tailnet/-/acl", headers=h, timeout=45)
        a.raise_for_status()
        return d.json().get("devices", []), a.text

    def fetch_users(self) -> list[dict]:
        """Tailnet users. Failing this must not take the page down — ownership
        is an extra, the map works without it.

        A 404 is treated as "this endpoint isn't available for this tailnet":
        every real attempt is logged, but after one the call is skipped for
        USERS_RETRY_AFTER rather than repeated on every refresh and live poll.
        Other failures (timeouts, 5xx, auth) are retried on the next refresh."""
        now = time.time()
        if now < self._users_retry_at:
            log.debug("[%s] users: skipped, retrying in %d min", self.slug,
                      (self._users_retry_at - now) // 60)
            return []
        try:
            # An API key if one is configured (the only credential this endpoint
            # accepts, as far as we have been able to tell), else the OAuth token.
            bearer = self.api_key or self._token()
            r = httpx.get(f"{API}/tailnet/-/users",
                          headers={"Authorization": f"Bearer {bearer}"}, timeout=30)
            r.raise_for_status()
            self._users_retry_at = 0.0
            return r.json().get("users") or []
        except Exception as e:
            resp = getattr(e, "response", None)
            code = getattr(resp, "status_code", None)
            body = ""
            if resp is not None:
                try:
                    body = (resp.text or "").strip()[:200]
                except Exception:
                    pass
            extra = f" — response: {body}" if body else ""
            if code == 404:
                self._users_retry_at = now + USERS_RETRY_AFTER
                extra += (" — endpoint not found for this credential" +
                          ("" if self.api_key else
                           " (OAuth tokens get 404 here; set api_key_env in tailnets.toml)") +
                          f"; Owners table is derived from the device list, "
                          f"not asking again for {USERS_RETRY_AFTER // 3600}h")
            elif code in (401, 403) and self.api_key:
                extra += " — API key rejected: expired or revoked? (keys expire after at most 90 days)"
            log.warning("[%s] users not available: %s%s", self.slug, e, extra)
            return []

    # ----------------------------------------------------------- devices
    @staticmethod
    def _endpoints(dev: dict) -> list[str]:
        return [split_endpoint(e) for e in
                ((dev.get("clientConnectivity") or {}).get("endpoints") or [])]

    def private_v4(self, dev: dict) -> list[str]:
        return [ip for ip in self._endpoints(dev)
                if ":" not in ip and ip.startswith(("192.168.", "172.", "10."))]

    def public_addrs(self, dev: dict) -> list[str]:
        """The internet-facing address(es) Tailscale learned for this device."""
        out: list[str] = []
        for ip in self._endpoints(dev):
            if is_public(ip) and ip not in out:
                out.append(ip)
        return out

    def lan_and_site(self, dev: dict) -> tuple[str, str | None, bool]:
        """-> (address to show, site, whether that address is an uplink/WAN).
        In auto mode "matched nothing" is None, not "mobile": the caller still
        has the public address to cluster on."""
        ips = self.private_v4(dev)
        pool = [ip for ip in ips if not is_own_interface(ip)] or ips

        served = [r for r in (dev.get("advertisedRoutes") or [])
                  if r not in ("0.0.0.0/0", "::/0") and ":" not in r]

        def _outside(ip: str) -> bool:
            try:
                a = ipaddress.ip_address(ip)
            except ValueError:
                return False
            return all(a not in ipaddress.ip_network(r, strict=False) for r in served)

        def result(ip: str, site: str) -> tuple[str, str, bool]:
            return ip, site, bool(served) and _outside(ip)

        for val, site, exact in self.site_rules:
            for ip in pool:
                if (ip == val) if exact else ip.startswith(val):
                    return result(ip, site)
        if self.auto:
            # a hotspot range sits inside 172.16/12 — claim it before the Docker
            # rule below does (manual configs do this with an ordinary `match`)
            for ip in pool:
                if ip.startswith(self.hotspot_prefixes):
                    return result(ip, "mobile")
        for ip in pool:
            if DOCKER_RE.match(ip):
                return result(ip, "dock")
        fallback = None if self.auto else "mobile"
        return result(pool[0], fallback) if pool else ("—", fallback, False)

    def build_devices(self, devs: list) -> list[dict]:
        out, used = [], set()
        for d in devs:
            name = d["name"].split(".")[0]
            tags = d.get("tags") or []
            lan, site, is_wan = self.lan_and_site(d)
            if name in self.always_mobile:
                site = "mobile"
            enabled = d.get("enabledRoutes") or []
            routes = [r for r in enabled if r not in ("0.0.0.0/0", "::/0")]
            out.append({
                "id": short_id(name, used), "name": name, "tags": tags,
                "ip": (d.get("addresses") or ["—"])[0],
                "lan": lan, "lanKind": "uplink" if is_wan else "lan", "site": site,
                "owner": (d.get("user") or TAG_OWNED), "ownerKind": "self",
                "siteLabel": self.site_meta[site]["label"] if site else "",
                "os": (d.get("os") or "").lower(),
                "role": role_of(tags),
                "exit": "0.0.0.0/0" in enabled,
                "subnet": bool(routes), "routes": routes,
                "online": bool(d.get("connectedToControl")),
                "update": bool(d.get("updateAvailable")),
                "user": d.get("user", ""),
                # Tailscale's actual node-sharing feature: this device belongs
                # to ANOTHER tailnet and was shared into this one — you don't
                # own it, can't tag or route through it, it just appears.
                # There is no equivalent field for the other direction: the
                # API never tells a tailnet which of ITS OWN devices it has
                # shared out to someone else, so that can't be shown here.
                "sharedIn": bool(d.get("isExternal")),
            })
            if self.auto:
                out[-1]["_pool"] = self.private_v4(d)
                out[-1]["_pubs"] = self.public_addrs(d)
        if self.auto:
            self.assign_sites(out)
            for d in out:
                meta = d.get("siteMeta") or self.site_meta[d["site"]]
                d["siteLabel"] = meta["label"]
                d.pop("_pool", None)
                d.pop("_pubs", None)
        out.sort(key=lambda x: x["name"])
        return out

    # ------------------------------------------------- automatic sites
    def assign_sites(self, devs: list[dict]) -> None:
        """Place every device that no explicit rule claimed (`site is None`).

        Private addresses are not unique — every home is 192.168.1.x — so
        they cannot say where a device is. The public address its traffic
        leaves through is: devices behind one uplink are at one place.

          1. a hotspot range            -> roaming. Its public address is a
                                           carrier's, shared with strangers.
          2. cluster by public address  -> a PLACE if it has an anchor (a
             (IPv4, or IPv6 /64)           server, exit node, home-tagged
                                           device or subnet router) or at
                                           least `min_site_devices` devices
                                           that are not phones/tablets;
                                           otherwise roaming. A `public =`
                                           pin on a [[site]] overrides this.
          3. no public address at all   -> the site of the one subnet router
                                           whose routes contain its LAN
                                           address; if none or several,
                                           roaming. Only ever a fallback: a
                                           café can use 192.168.1.x too.
        """
        todo = [d for d in devs if d["site"] is None]
        for d in todo:
            if any(ip.startswith(self.hotspot_prefixes) for ip in d["_pool"]):
                d["site"] = "mobile"
        todo = [d for d in todo if d["site"] is None]

        parent = {d["id"]: d["id"] for d in todo}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        owner: dict[str, str] = {}
        for d in todo:
            for k in {pub_key(a) for a in d["_pubs"]}:
                if k in owner:
                    parent[find(d["id"])] = find(owner[k])
                else:
                    owner[k] = d["id"]
        groups: dict[str, list[dict]] = {}
        for d in todo:
            if d["_pubs"]:
                groups.setdefault(find(d["id"]), []).append(d)
        metas = [m for m in (self._place_group(g) for g in groups.values()) if m]
        # A LAN range that every home shares (192.168.1.0/24) says nothing about
        # which site is which, so it only goes into a label while it is unique.
        shared = {k for k, n in Counter(m["_lan"] for m in metas if m["_lan"]).items() if n > 1}
        for m in metas:
            if m["_lan"] in shared:
                m["label"] = m["_anchor"] or m["_key"]
            for k in ("_lan", "_anchor", "_key"):
                m.pop(k)

        for d in todo:
            if d["site"] is not None or d["_pubs"]:
                continue
            cands = set()
            for r in devs:
                if r is d or r["site"] in (None, "mobile", "dock"):
                    continue
                for route in r["routes"]:
                    for ip in d["_pool"]:
                        try:
                            if ipaddress.ip_address(ip) in ipaddress.ip_network(route, strict=False):
                                cands.add(r["site"])
                        except ValueError:
                            pass
            if len(cands) == 1:
                d["site"] = cands.pop()
                meta = next((x.get("siteMeta") for x in devs if x["site"] == d["site"]
                             and x.get("siteMeta")), None)
                if meta:
                    d["siteMeta"] = meta
            else:
                d["site"] = "mobile"

    def _place_group(self, grp: list[dict]) -> dict | None:
        """Give one same-uplink cluster a site. -> its meta if it is a derived
        site (so the caller can fix up labels once it has seen all of them)."""
        # IPv4 first: it reads better as a label and is the more stable anchor
        keys = sorted({pub_key(a) for d in grp for a in d["_pubs"]},
                      key=lambda k: (":" in k, k))
        pin = next((sid for net, sid in self.public_pins
                    for d in grp for a in d["_pubs"]
                    if ipaddress.ip_address(a) in net), None)
        anchors = sorted((d for d in grp if d["role"] in ("home", "server", "exit") or d["subnet"]),
                         key=lambda d: (d["role"] != "home", not d["subnet"], d["name"]))
        people = [d for d in grp if d["os"] not in MOBILE_OS]
        if pin:
            for d in grp:
                d["site"] = pin
            return None
        if not anchors and len(people) < self.min_site_devices:
            for d in grp:
                d["site"] = "mobile"
            return None
        digest = hashlib.sha1(keys[0].encode()).hexdigest()
        h = int(digest, 16)
        lan = [n for n, _ in Counter(
            str(ipaddress.ip_network(f"{ip}/24", strict=False))
            for d in grp for ip in d["_pool"] if not DOCKER_RE.match(ip)).most_common(2)]
        a0 = anchors[0]["name"] if anchors else None
        label = (f"{a0} · {lan[0]}" if a0 and lan else a0 or (lan[0] if lan else keys[0]))
        meta = {"label": label,
                "net": ", ".join(lan + [f"via {keys[0]}"]),
                "fx": round(0.12 + (h % 1000) / 1000 * 0.76, 3),
                "fy": round(0.20 + ((h // 1000) % 1000) / 1000 * 0.62, 3),
                "tint": SITE_TINTS[(h // 1000000) % len(SITE_TINTS)],
                "_lan": lan[0] if lan else None, "_anchor": a0, "_key": keys[0]}
        for d in grp:
            d["site"] = "a-" + digest[:6]
            d["siteMeta"] = meta
        return meta

    def build_sites(self, devs: list[dict]) -> list[dict]:
        counts: dict[str, int] = {}
        for d in devs:
            counts[d["site"]] = counts.get(d["site"], 0) + 1
        metas = dict(self.site_meta)
        for d in devs:
            if d.get("siteMeta"):
                metas[d["site"]] = d["siteMeta"]
        # configured sites keep their configured order; derived ones follow,
        # biggest first (ties by id, so the order is stable between refreshes)
        derived = sorted((k for k in metas if k not in self.site_order),
                         key=lambda k: (-counts.get(k, 0), k))
        sites = []
        for sid in self.site_order + derived:
            n = counts.get(sid, 0)
            if not n:
                continue
            m = metas[sid]
            fr = min(0.28, max(0.085, 0.075 + 0.024 * n))
            sites.append({"id": sid, "label": m["label"], "net": m["net"],
                          "fx": m["fx"], "fy": m["fy"], "fr": round(fr, 3),
                          "tint": m["tint"], "count": n})
        return sites

    # ----------------------------------------------------------- edges
    def build_edges(self, acl: dict, devs: list[dict]) -> tuple[list[dict], list[int]]:
        hosts = acl.get("hosts") or {}
        edges, per_grant = [], []
        seen = set()
        for g in (acl.get("grants") or []):
            srcs = g.get("src") or []
            dsts = g.get("dst") or []
            kind = "peer"
            for s in srcs:
                kind = KIND.get(s, kind)
            n = 0
            for s in srcs:
                for a in expand(s, devs, hosts):
                    for t in dsts:
                        for b in expand(t, devs, hosts):
                            if a["id"] == b["id"]:
                                continue
                            key = (a["id"], b["id"], kind)
                            if key in seen:
                                continue
                            seen.add(key)
                            bidir = bool(set(srcs) & set(dsts))
                            edges.append({"a": a["id"], "b": b["id"], "kind": kind,
                                          "faint": len(dsts) > 3,
                                          **({"bidir": True} if bidir else {})})
                            n += 1
            per_grant.append(n)
        return edges, per_grant

    def build_access(self, acl: dict, devs: list[dict]) -> dict[str, list[dict]]:
        """For every device, the actual grants that reach it — not who is on
        the other end of them. A grant naming one tag is ONE rule here no
        matter how many devices carry that tag; `expand()` is only used to
        resolve which devices the grant's dst side covers, so the device
        this device's owner actually cares about is the tag/group/host that
        was written in the policy, not a flattened list of peers.

        This is what makes a device "exposed": every key in the returned
        dict has at least one inbound rule, i.e. something outside itself
        can reach it via policy. That's a different thing from Tailscale's
        own node-sharing feature (see `sharedIn` in build_devices) — this is
        "who does the ACL let in", not "who is this node shared with".

        A grant sourced ENTIRELY from Tailscale's own admin autogroups is
        skipped — "a tailnet admin can reach everything" is the platform's
        baseline, true of every device alike, not a deliberate act of
        exposure worth flagging. A grant naming a real tag or group (even
        one an org calls "admin") still counts.
        """
        hosts = acl.get("hosts") or {}
        by_id: dict[str, list[dict]] = {}
        for g in (acl.get("grants") or []):
            srcs = g.get("src") or []
            dsts = g.get("dst") or []
            ips = g.get("ip") or []
            if srcs and set(srcs) <= STRUCTURAL_SRC:
                continue
            targets = {d["id"] for t in dsts for d in expand(t, devs, hosts)}
            if not targets:
                continue
            rule = {"src": srcs, "ip": ips}
            for did in targets:
                rules = by_id.setdefault(did, [])
                if rule not in rules:
                    rules.append(rule)
        return by_id

    # ----------------------------------------------------------- html bits
    def device_rows(self, devs: list[dict], access: dict[str, list[dict]]) -> str:
        rows = []
        for d in devs:
            chips = "".join(chip(t) for t in sorted(d["tags"])) or \
                '<span class="note">untagged</span>'
            if d["routes"]:
                chips += f'<span class="note">{html.escape(" ".join(d["routes"]))}</span>'
            if d["ownerKind"] == "other":
                chips += f'<span class="note">user: {html.escape(d["owner"].split("@")[0])}</span>'
            if d["update"]:
                chips += '<span class="note">update available</span>'
            n_rules = len(access.get(d["id"], []))
            if n_rules:
                chips = (f'<span class="chip exposed" title="{n_rules} inbound rule'
                         f'{"s" if n_rules != 1 else ""}">exposed</span>') + chips
            if d["sharedIn"]:
                chips = '<span class="chip shared-in" title="owned by another tailnet, ' \
                        'shared into this one">shared in</span>' + chips
            off = "" if d["online"] else ' <span class="off">offline</span>'
            rows.append(
                f'<tr><td class="name">{html.escape(d["name"])}{off}</td>'
                f'<td class="ip">{html.escape(d["ip"])}</td>'
                f'<td class="ip">{html.escape(d["lan"])} '
                f'<span class="note">·{d["site"]}</span></td>'
                f'<td class="ip">{html.escape(d["os"])}</td>'
                f'<td><div class="chips">{chips}</div></td></tr>')
        return "\n".join(rows)

    def grant_rows(self, acl: dict, counts: list[int]) -> str:
        rows = []
        for i, g in enumerate((acl.get("grants") or []), 1):
            src = " ".join(chip(x) for x in (g.get("src") or []))
            dst = ' <span class="to">·</span> '.join(
                chip(x) if x.startswith("tag:") else
                f'<span class="ip">{html.escape(x)}</span>' for x in (g.get("dst") or []))
            ips = g.get("ip") or []
            ports = ('<span class="port all">any</span>' if ips == ["*"] else
                     " ".join(f'<span class="port">{html.escape(p)}</span>' for p in ips))
            n = counts[i - 1] if i - 1 < len(counts) else 0
            rows.append(f'<tr><td class="ip">{i}</td><td>{src}</td>'
                        f'<td class="flow">{dst}</td><td>{ports}</td>'
                        f'<td class="ip">{n}</td></tr>')
        return "\n".join(rows)

    def build_users(self, devs: list[dict]) -> list[dict]:
        """Owners with the devices attributed to them.

        Devices that joined on a tagged auth key have no user at all — they
        belong to their tag. That is not an error and it is worth showing, so
        they get their own pseudo-owner rather than being dropped.
        """
        api = {u.get("loginName"): u for u in self.fetch_users()}
        primary = next((login for login, u in api.items()
                        if u.get("role") == "owner"), None)
        for d in devs:
            d["ownerKind"] = ("tag" if d["owner"] == TAG_OWNED else
                              ("self" if d["owner"] == primary else "other"))
        # Owners the users API didn't return (it failed, or the OAuth client
        # lacks `users:read`) still own devices; derive them from the device
        # list so their devices don't vanish from the table. They just have no
        # displayName/role/status beyond the login.
        known = set(api) | {TAG_OWNED}
        order = (list(api)
                 + sorted({d["owner"] for d in devs if d["owner"] and d["owner"] not in known})
                 + [TAG_OWNED])
        out = []
        for login in order:
            mine = [d for d in devs if d["owner"] == login]
            if not mine:
                continue
            u = api.get(login) or {}
            sites = sorted({d["siteLabel"] for d in mine})
            out.append({
                "login": login,
                "name": ("tag-owned" if login == TAG_OWNED
                         else (u.get("displayName") or login.split("@")[0])),
                "role": "—" if login == TAG_OWNED else (u.get("role") or "member"),
                "status": "—" if login == TAG_OWNED else (u.get("status") or ""),
                "devices": len(mine),
                "online": sum(1 for d in mine if d["online"]),
                "names": sorted(d["name"] for d in mine),
                "sites": sites,
            })
        out.sort(key=lambda x: (x["login"] == TAG_OWNED, -x["devices"]))
        return out

    def user_rows(self, users: list[dict]) -> str:
        rows = []
        for u in users:
            tag = ("tag-owned" if u["login"] == TAG_OWNED else
                   ("owner" if u["role"] == "owner" else "member"))
            who = (f'<span class="name">{html.escape(u["name"])}</span>'
                   if u["login"] == TAG_OWNED else
                   f'<span class="name">{html.escape(u["name"])}</span>'
                   f'<div class="note">{html.escape(u["login"])}</div>')
            devs = " ".join(f'<span class="chip">{html.escape(n)}</span>'
                            for n in u["names"])
            rows.append(
                f'<tr><td>{who}</td>'
                f'<td><span class="chip {"home" if tag == "owner" else "client"}">'
                f'{tag}</span></td>'
                f'<td class="ip">{u["online"]}/{u["devices"]}</td>'
                f'<td class="note nw">'
                + "<br>".join(html.escape(s) for s in u["sites"]) + '</td>'
                f'<td><div class="chips">{devs}</div></td></tr>')
        return "\n".join(rows)

    def rule_rows(self) -> str:
        """Document the placement logic — generated FROM the constants that
        drive it, so the page cannot drift away from the code."""
        def net(x):
            return f'<span class="ip">{html.escape(x)}</span>'

        if self.auto:
            return self._auto_rule_rows(net)
        steps: list[tuple[str, str, str]] = [
            ("Collect",
             "Take every private address the device was last seen on "
             f"({net('10.x')}, {net('172.16-31.x')}, {net('192.168.x')}).",
             "Tailscale reports all interfaces, not just the useful one."),
            ("Drop self-hosted",
             f"Ignore any address ending in {net('.1')} — the gateway of a network "
             "the device hands out itself.",
             "Travel routers carry their own LAN everywhere; it says nothing "
             "about location. Same for Docker bridges."),
        ]
        place = "<br>".join(
            f"{net(val if exact else val + 'x')} &rarr; {self.site_meta[s]['label']}"
            for val, s, exact in self.site_rules)
        steps.append(("Match a place", place,
                      "Only networks bound to a physical location count. "
                      "The hotspot range is listed so it is not mistaken for a "
                      "Docker bridge — both sit inside 172.16/12."))
        steps.append(("Docker",
                      f"Anything left inside {net('172.16/12')} joins the "
                      f"{self.site_meta['dock']['label']} cluster.",
                      "Containers share the host's netns and have no LAN of "
                      "their own."))
        steps.append(("Otherwise",
                      f"{self.site_meta['mobile']['label']}.",
                      "Mobile data, a hotspot or an unknown network — the device "
                      "is not at any known site."))
        if self.always_mobile:
            pinned = ", ".join(f'<span class="name">{html.escape(n)}</span>'
                               for n in sorted(self.always_mobile))
            steps.append(("Pinned", f"{pinned} always counts as roaming.",
                          "A datacenter VM has no LAN that would place it "
                          "anywhere meaningful."))
        return "\n".join(
            f'<tr><td class="name">{i}. {t}</td><td>{r}</td>'
            f'<td class="note">{w}</td></tr>'
            for i, (t, r, w) in enumerate(steps, 1))

    def _auto_rule_rows(self, net) -> str:
        """The same table for site_mode = "auto". Built from the constants and
        settings that drive assign_sites(), so it cannot drift from it."""
        hot = ", ".join(net(h + "x") for h in self.hotspot_prefixes)
        steps = [
            ("Explicit rules",
             "A <span class=\"name\">match</span> prefix in the config still wins, "
             f"{len(self.site_rules)} defined."
             if self.site_rules else "None defined — every site is derived.",
             "Optional. Only for networks that really are bound to one place."),
            ("Docker",
             f"Anything left inside {net('172.16/12')} joins the "
             f"{self.site_meta['dock']['label']} cluster.",
             "Containers share the host's netns and have no LAN of their own."),
            ("Hotspot",
             f"A device on {hot} is roaming.",
             "Its public address is the carrier's, shared with strangers, so it "
             "must not anchor or join a site."),
            ("Same uplink",
             "Devices that leave the network through the same public address "
             f"({net('IPv4')}, or the {net('IPv6 /64')}) are one place.",
             "Private addresses repeat across every home and café; the public "
             "address does not."),
            ("A place needs",
             "an anchor — a server, exit node, home-tagged device or subnet router "
             f"— or at least {self.min_site_devices} devices that are not phones or tablets.",
             "Otherwise one phone or laptop at a café would become a site of its own."),
            ("No public address",
             "Joins the one site whose subnet router advertises its LAN address; "
             "none, or several, means roaming.",
             "Only a fallback: a café can use 192.168.1.x too."),
            ("Otherwise",
             f"{self.site_meta['mobile']['label']}.",
             "Mobile data, a hotspot or an unknown network."),
        ]
        if self.public_pins:
            steps.insert(1, ("Pinned sites",
                             "<br>".join(f"{net(str(n))} &rarr; {self.site_meta[sid]['label']}"
                                         for n, sid in self.public_pins),
                             "`public = [...]` on a site: whoever sits behind that "
                             "address is that site, whatever its size."))
        if self.always_mobile:
            pinned = ", ".join(f'<span class="name">{html.escape(n)}</span>'
                               for n in sorted(self.always_mobile))
            steps.append(("Pinned roaming", f"{pinned} always counts as roaming.",
                          "A datacenter VM has no LAN that would place it anywhere "
                          "meaningful."))
        return "\n".join(
            f'<tr><td class="name">{i}. {t}</td><td>{r}</td>'
            f'<td class="note">{w}</td></tr>'
            for i, (t, r, w) in enumerate(steps, 1))

    def site_rows(self, sites: list[dict], devs: list[dict]) -> str:
        rows = []
        for s in sites:
            members = [d for d in devs if d["site"] == s["id"]]
            roles = sorted({d["role"] for d in members})
            rows.append(
                f'<tr><td class="name">{html.escape(s["label"])}</td>'
                f'<td class="ip">{html.escape(s["net"])}</td>'
                f'<td class="ip">{len(members)}</td>'
                f'<td><div class="chips">'
                + "".join(f'<span class="chip {r}">{r}</span>' for r in roles)
                + '</div></td></tr>')
        return "\n".join(rows)

    # ----------------------------------------------------------- render
    RULE_NOTE = ("Rules are rendered from the same constants the placement code "
                 "uses, so this table cannot fall out of sync. A device that is "
                 "offline keeps its last known address, and therefore its last "
                 "known site, until it checks in again.")

    def build(self) -> dict:
        """Everything the page needs, as data.

        Kept separate from the HTML so the live poll can ship a few KB of JSON
        instead of re-sending the whole document every ten seconds.
        """
        devs_raw, acl_text = self.fetch()
        devs = self.build_devices(devs_raw)
        acl = parse_acl(acl_text)
        sites = self.build_sites(devs)
        edges, counts = self.build_edges(acl, devs)
        access = self.build_access(acl, devs)
        users = self.build_users(devs)

        node_js = [{"id": d["id"], "name": d["name"], "role": d["role"],
                    "site": d["site"], "siteLabel": d["siteLabel"], "ip": d["ip"],
                    "lan": d["lan"], "lanKind": d["lanKind"],
                    "loc": d["site"], "os": d["os"], "owner": d["owner"],
                    "ownerKind": d["ownerKind"],
                    "tags": d["tags"], "routes": d["routes"], "online": d["online"],
                    # "exposed" = at least one grant reaches this device from
                    # outside itself. `access` carries the actual rules (source
                    # tags/groups + ports) so the UI can say who, not just how
                    # many. "sharedIn" is the unrelated, real Tailscale feature:
                    # this node belongs to another tailnet and was shared here.
                    "exposed": d["id"] in access, "access": access.get(d["id"], []),
                    "sharedIn": d["sharedIn"]}
                   for d in devs]

        return {
            "slug": self.slug, "tailnet": self.tailnet_label,
            "tailnetShort": self.label_short,
            "nodes": node_js, "edges": edges, "sites": sites,
            "kpi": {"devices": len(devs),
                    "online": sum(1 for d in devs if d["online"]),
                    "grants": len(acl.get("grants") or []),
                    "sites": len(sites),
                    "exposed": len(access),
                    "sharedIn": sum(1 for d in devs if d["sharedIn"])},
            "rows": {"devices": self.device_rows(devs, access),
                     "grants": self.grant_rows(acl, counts),
                     "sites": self.site_rows(sites, devs),
                     "users": self.user_rows(users),
                     "rules": self.rule_rows()},
            "users": users,
            "stamp": datetime.now(TZ).strftime("%d %b %Y · %H:%M:%S"),
        }

    def render_html(self, data: dict) -> str:
        mins = REFRESH // 60
        tpl = open(TEMPLATE).read()
        return (tpl
                .replace("__NODES__", json.dumps(data["nodes"], ensure_ascii=False))
                .replace("__EDGES__", json.dumps(data["edges"]))
                .replace("__SITES__", json.dumps(data["sites"], ensure_ascii=False))
                .replace("__DEVICE_ROWS__", data["rows"]["devices"])
                .replace("__GRANT_ROWS__", data["rows"]["grants"])
                .replace("__SITE_ROWS__", data["rows"]["sites"])
                .replace("__USER_ROWS__", data["rows"]["users"])
                .replace("__RULE_ROWS__", data["rows"]["rules"])
                .replace("__RULE_NOTE__", self.RULE_NOTE)
                .replace("__N_DEVICES__", str(data["kpi"]["devices"]))
                .replace("__N_ONLINE__", str(data["kpi"]["online"]))
                .replace("__N_GRANTS__", str(data["kpi"]["grants"]))
                .replace("__N_SITES__", str(data["kpi"]["sites"]))
                .replace("__N_USERS__", str(len(data["users"])))
                .replace("__N_EXPOSED__", str(data["kpi"]["exposed"]))
                .replace("__N_SHAREDIN__", str(data["kpi"]["sharedIn"]))
                .replace("__REFRESH__", f"{mins} min" if mins else f"{REFRESH}s")
                .replace("__TAILNET__", self.tailnet_label)
                .replace("__TAILNET_SHORT__", self.label_short)
                .replace("__STAMP__", data["stamp"])
                .replace("__TAILNETS_JSON__", json.dumps(
                    [{"slug": t.slug, "label": t.label_short} for t in REGISTRY.values()],
                    ensure_ascii=False))
                .replace("__SLUG__", self.slug))


# --------------------------------------------------------------- registry
def load_registry(manifest_path: str = MANIFEST) -> dict[str, Tailnet]:
    try:
        with open(manifest_path, "rb") as fh:
            manifest = tomllib.load(fh)
    except FileNotFoundError:
        raise SystemExit(
            f"no manifest at {manifest_path}\n"
            "  cp tailnets.example.toml tailnets.toml   # then edit it\n"
            "tailnets.toml is mounted into the container by compose.yaml.")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{manifest_path} is not valid TOML: {e}")
    entries = manifest.get("tailnet") or []
    if not entries:
        raise SystemExit(f"{manifest_path}: no [[tailnet]] entries")
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    reg: dict[str, Tailnet] = {}
    for e in entries:
        slug = e["slug"]
        config_path = e["config"]
        if not os.path.isabs(config_path):
            config_path = os.path.join(base_dir, config_path)
        reg[slug] = Tailnet.load(slug, config_path, e["oauth_id_env"], e["oauth_secret_env"],
                                e.get("api_key_env", ""))
    return reg


REGISTRY: dict[str, Tailnet] = load_registry()
DEFAULT_SLUG = next(iter(REGISTRY))
