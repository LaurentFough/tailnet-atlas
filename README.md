# Tailnet Atlas

A live map of your tailnet that answers one question: **who can actually reach what?**

It reads your device list and your policy file from the Tailscale API and draws
them as one picture — devices grouped by where they physically are, lines taken
straight from your grants. One instance can hold maps for **several tailnets**
at once, switchable in-app from one URL — see [Multiple tailnets](#multiple-tailnets-one-url).

### ▶ [Try the live demo](https://laurentfough.github.io/tailnet-atlas/)

Hover the nodes, click one to pin it, click a role in the legend to filter.

![Tailnet Atlas](docs/atlas-demo.gif)

*Everything in the demo and the recording is invented — fake devices, users,
sites and networks — but it is rendered by the real code, not mocked up. You
can regenerate it yourself, see [Demo](#demo).*

---

## What it shows

* **Devices are grouped by where they physically are**, not by role. The region
  a node lands in comes from the address it was last given by the network it
  joined. Nothing is hand-assigned: a phone that comes home moves out of
  "roaming" into the home cluster on the next refresh.
* **Lines are grants** from your policy file, and they are directional — the
  source initiates, the target never answers back. Particles flow the way
  access does.
* **Hover a node** for its site, Tailscale IP, local address, OS, owner,
  advertised routes, tags, and how many nodes it reaches / is reached by.
* **Click to pin** and everything unrelated dims — the view worth having open
  while you are about to change a grant.
* **The legend doubles as a role filter.**
* **A switcher above the map** lets you flip between tailnets when more than
  one is configured — the whole page (graph, tables, brand) swaps in place,
  no reload.
* Tables below the map for devices, grants, sites and owners, plus one that
  documents the placement rules. That table is generated from the same
  constants the placement code uses, so it cannot drift from the behaviour.

## The bit that was harder than expected

Locating a device sounds trivial and isn't. Tailscale reports *every* interface
a device has: container bridges, VM adapters, and — worst — travel routers that
hand out their own LAN and carry it everywhere. Grouping on "has a 192.168.8.x
address" puts your travel router at home while it is sitting in a hotel.

The rule that works:

> **Whoever hands out a network holds the `.1` in it.**

So drop those addresses and keep the one the device was *given*. That is its
uplink, and that is where it actually is. Pleasant side effect: a Docker host
stops showing a bridge address and shows its real LAN IP instead.

Deliberately **not** done via the device's advertised routes — a subnet router
that merely *joined* the network it announces holds a normal host address in
it, and that test would strip its real address. The gateway convention
separates "I created this network" from "I joined this network" exactly.

One trap if you build something similar: a phone hotspot hands out
`172.20.10.x`, which sits inside `172.16/12` and otherwise reads as a container
bridge. Rule order matters, which is why it is explicit in the config.

## Running it

You need a Tailscale **OAuth client** per tailnet you want to show (read-only
is enough: `devices:core:read` and `policy_file:read`), and a **tagged** auth
key for the one node the app itself runs as.

```bash
git clone https://github.com/LaurentFough/tailnet-atlas
cd tailnet-atlas
cp .env.example .env                          # auth key, node name, OAuth client(s)
cp tailnets.example.toml tailnets.toml        # which tailnets to show
cp config.example.toml config-<slug>-tsnet.toml   # one per tailnet — your sites and networks
cp compose.example.yml compose.yml            # then add a volume mount per config-<slug>-tsnet.toml
python3 scripts/preflight.py                  # warns if the node name is already taken (see below)
docker compose up -d --build
```

Then open `https://<TS_HOSTNAME>.<your-tailnet>.ts.net`.

The app binds `127.0.0.1` inside the Tailscale sidecar's network namespace and
is reachable only through `tailscale serve`. Funnel is explicitly disabled in
`serve.json`. It only ever reads from the API — for every tailnet it shows,
not just the one the sidecar joins (more on that below).

### Configuration

Two files, at two different scopes:

* **`tailnets.toml`** (copy from [`tailnets.example.toml`](tailnets.example.toml))
  lists which tailnets to show. One `[[tailnet]]` block per map: a `slug`, the
  path to its site config, and the names of the two env vars holding its
  OAuth credentials. The first block is what `/` shows by default.
* **One `config-<slug>-tsnet.toml` per tailnet** (copy from
  [`config.example.toml`](config.example.toml), which is commented in full).
  One `[[site]]` block per place, each with an address prefix that places a
  device there. Two site ids are reserved — `dock` takes whatever is left
  inside `172.16/12`, and `mobile` is the fallback for anything that matched
  nothing.

Positions (`fx`/`fy`) are only a starting hint. The layout pushes overlapping
regions apart and keeps them inside the frame, and on narrow screens it stacks
them in a single column, so you do not have to get them right.

## Automatic sites (`site_mode = "auto"`)

Hand-written `[[site]]` blocks match devices by **private** address prefix, and
private addresses are not unique: every home is `192.168.1.x`, a café can be too.
With a handful of subnets that gets ambiguous; with many it falls apart.

Set `site_mode = "auto"` in a tailnet's config and sites are derived from the
**public** address each device's traffic leaves through instead:

1. Docker bridges stay in the Docker cluster, exactly as before.
2. A device on a hotspot range (`172.20.10.x`, `192.168.43.x`, `192.168.42.x`) is
   roaming — its public address is the carrier's, shared with strangers.
3. Devices behind the same public address (IPv4, or the IPv6 `/64`) are one place.
   A cluster is a **site** if it has an anchor — a server, exit node, home-tagged
   device or subnet router — or at least `min_site_devices` (default 2) devices that
   are not phones/tablets. Otherwise it is roaming: one laptop at a café should not
   become a site.
4. A **lone server** — alone at its own public address, as happens behind a VPN or a
   second WAN — joins another site when its LAN belongs to exactly one of them.
   Vendor-default ranges (`192.168.1.0/24`, `10.0.0.0/24`…) never count, since
   thousands of homes share them (`common_lans` adds more).
5. A device with **no** public address (Tailscale sometimes reports only its LAN
   address) joins the site whose subnet router advertises its LAN address, else the
   one site where that LAN is seen; with none or several it is roaming. These are only
   fallbacks — matching on private ranges alone is the very thing that collides.

**Why is this device here?** Hover any node: the tooltip's **placed** row gives the
reason in a sentence ("alone at public address …", "its LAN 10.0.8.0/24 is seen at only
one site…", "roaming: no anchor…"). Start there when a device is somewhere you did not
expect.

Sites are labelled from their anchor (`hub · 192.168.10.0/24`), or just the anchor's
name when several sites share a LAN range. Give one a friendly name and a fixed
place with `public = ["203.0.113.7"]` on a `[[site]]` block. `pinned_roaming` and
manual `match` rules still work in auto mode, and the layout shrinks and separates
the regions so dozens of sites stay readable. Nothing leaves your machine: there
is no IP-to-location lookup, only what the Tailscale API already reports.

**Mobile networks.** With the optional ASN file (see below), a cluster with no anchor
whose devices are mostly on a mobile carrier's network is roaming too, however many
laptops share the tether, and a site with no anchor is named after its ISP. The
carrier check reads the network's *name* (words like mobile, cellular, LTE) and your
`mobile_asns` / `not_mobile_asns` lists, which win. The name check is a hint: it
catches networks that say so (T-Mobile, China Mobile, Two Degrees…) and misses ones
that do not (Vodafone Idea, Orange), and mobile and fixed service often share an
ASN — so list the ASNs you know. A server or other anchor on a mobile uplink still
makes a place, since many homes are on LTE routers.

Known limits, because the public address is a heuristic:

* A home with two uplinks (load balancing, failover) splits into two sites; pin
  both addresses to one `[[site]]` with `public = [...]`.
* Several homes behind one carrier-grade NAT address look like one place.
* An offline device keeps its last known address, so it stays where it was last seen.
* A home where only phones are online and there is no server reads as roaming
  until you pin it.

See `config.example.toml` for the settings. The default is still `manual`.

## Country flags and network names

Two optional databases, both read locally — nothing is ever sent anywhere:

| file | gives | licence |
|---|---|---|
| DB-IP Lite country (~5 MB, monthly) | the flags below | CC BY 4.0 (credited on the page) |
| iptoasn.com ip2asn (~11 MB, hourly) | each device's network: ISP names, mobile-carrier detection | public domain |

```bash
python3 scripts/fetch-geoip.py     # both -> data/geo/   (--only geo | --only asn)
docker compose up -d               # compose mounts ./data/geo into the app
```

Re-run it from cron to refresh; the app picks up new files without a restart. Either
file is optional — without it that feature is simply off. A node's tooltip then also
shows its network (`AS3243 MEO…`).

### Flags

Each site shows a flag beside its name — on the map, in the Sites table and in a
node's tooltip. Docker gets 🐳 and the roaming site 📱, since neither is in a
country; a site whose addresses can't be placed gets 🌐.

The country comes from your devices' **public** addresses, looked up in a local
copy of the free [DB-IP Lite](https://db-ip.com/db/download/ip-to-country-lite)
country database. Nothing is sent anywhere: you download the file, the app reads
it. A site takes the country most of its devices resolve to.

Without the country file there are no flags except Docker/roaming, and no error. The
page credits DB-IP whenever it is loaded.

To force or supply a flag — say, for a site that has no public address on record —
add `country = "PT"` to its `[[site]]` block.

It is country-level and a best guess: anycast and CDN addresses (Google's `8.8.8.8`
resolves to the US, one of its IPv6 DNS addresses to Canada), VPNs and mobile
carriers can land in the wrong country. Flags are emoji, so Windows shows two
letters (`PT`) instead of a flag.

## Multiple tailnets, one URL

One process, one Tailscale node, several maps. The trick is that the two
things people usually bundle together are actually independent:

* **Showing a tailnet's data** only needs its OAuth client — a plain HTTPS API
  call to `api.tailscale.com`, reachable from anywhere. It does **not**
  require the app to be a peer of that tailnet.
* **Serving the page** needs exactly one Tailscale node, reachable via
  `tailscale serve` from whichever tailnet you want the URL to live on.

So `compose.yml` (copied from [`compose.example.yml`](compose.example.yml)) runs a single `ts-tailnet` sidecar (joined to your "home"
tailnet) and a single `app` container namespaced to it — never one pair per
tailnet. Every tailnet in `tailnets.toml` just adds two more env vars in
`.env` (`oauth_id_env` / `oauth_secret_env`, named per-tailnet so their
credentials can't collide in one process) and one more `config-*.toml`
volume mount. The in-app switcher then just re-fetches `/api/state?net=<slug>`
and swaps the graph and tables in place.

Two things **not** to do if you add tailnets by hand:

* Don't run a second `tailscale` sidecar pointed at the same
  `./data/tailscale` directory to "add" a tailnet — two daemons sharing one
  state directory become the *same* node and fight over its identity. Each
  additional Tailscale **node** (as opposed to additional tailnet *data*)
  needs its own state directory.
* Don't reuse a bare env var name (`TS_OAUTH_CLIENT_ID`) across tailnets in
  compose's `environment:` block — `${VAR}` interpolation there is resolved
  once from the shell/root `.env`, the same value for every service, and
  silently overrides whatever a per-service `env_file:` set. That's why the
  credentials are named per-tailnet in `.env` instead.

### Node name and leftover nodes

The sidecar's name comes from `TS_HOSTNAME` in `.env` (required — there is no
default). Its identity is kept in `data/tailscale`, and `TS_AUTH_ONCE` makes a
restart or rebuild reuse it rather than log in again. Keep that directory, and
don't `docker compose down` + delete it casually: a node with no state registers
as a brand-new device.

If a node with that name is already in your tailnet — a lost state directory, a
container removed without logging out — Tailscale renames the new one
`<name>-1`, `<name>-2`, … and issues the cert for *that* name, so the URL you
use leads to the stale node. `scripts/preflight.py` lists the clashing nodes
before you start; `--delete` removes the offline ones (it asks first, never
touches online nodes, and needs the `devices:core` write scope on that
tailnet's OAuth client). `--strict` makes it fail a `&&` chain instead.

### Refresh behaviour

Each tailnet has its own background loop, rebuilding every 5 minutes
independently — one tailnet's API being down never stalls another's. While
the page is open it also polls every 10 seconds, for the currently-selected
tailnet, for up to 5 minutes of *viewing* time — a backgrounded tab never
polls. The server coalesces those requests behind a short TTL per tailnet, so
ten open tabs on the same map still mean one call upstream. What is served is
always the last successfully built version; if a tailnet's API goes down that
tailnet's page keeps working and grows a "stale" banner.

## Demo

No credentials needed — this pushes fictional devices through the real
rendering code, so it is the actual product with different input:

```bash
python3 demo/make_demo.py
open demo/out/atlas-demo.html
```

`demo/out/atlas-demo.html` is a single self-contained file you can open from
disk. The same build is written to `docs/index.html`, which is what GitHub
Pages serves as the [live demo](https://laurentfough.github.io/tailnet-atlas/) —
so regenerating and committing keeps the hosted page in step.

To re-record the GIF:

```bash
cd demo && npm install
DUMP=1 node record.js && node verify.js
```

`record.js` drives a real cursor with Puppeteer. `verify.js` decodes the
finished GIF with disposal applied and compares frames against the source PNGs
— the inter-frame diffing that gets the file from 22 MB down to 7 MB is exactly
the kind of optimisation that breaks silently.

`demo/geotest.js` replays the layout maths against a rendered page and checks
for overlapping nodes, colliding labels and regions leaving the frame — across
four window sizes and four different device distributions. Worth running after
touching anything in the layout.

## Status

Shared as-is because people asked for it, not as a maintained project. It works
on my setup; it may need adjusting for yours. No support, no roadmap, and I
probably will not review pull requests promptly — but the code is yours to take
in whatever direction you like.

## License

MIT
