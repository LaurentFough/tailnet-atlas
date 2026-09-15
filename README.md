# Tailnet Atlas

A live map of your tailnet that answers one question: **who can actually reach what?**

It reads your device list and your policy file from the Tailscale API and draws
them as one picture — devices grouped by where they physically are, lines taken
straight from your grants. One instance can hold maps for **several tailnets**
at once, switchable in-app from one URL — see [Multiple tailnets](#multiple-tailnets-one-url).

### ▶ [Try the live demo](https://crazyhoesl.github.io/tailnet-atlas/)

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
git clone https://github.com/crazyhoesl/tailnet-atlas
cd tailnet-atlas
cp .env.example .env                          # auth key, node name, OAuth client(s)
cp tailnets.example.toml tailnets.toml        # which tailnets to show
cp config.example.toml config-<slug>-tsnet.toml   # one per tailnet — your sites and networks
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

## Multiple tailnets, one URL

One process, one Tailscale node, several maps. The trick is that the two
things people usually bundle together are actually independent:

* **Showing a tailnet's data** only needs its OAuth client — a plain HTTPS API
  call to `api.tailscale.com`, reachable from anywhere. It does **not**
  require the app to be a peer of that tailnet.
* **Serving the page** needs exactly one Tailscale node, reachable via
  `tailscale serve` from whichever tailnet you want the URL to live on.

So `compose.yml` runs a single `ts-tailnet` sidecar (joined to your "home"
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
Pages serves as the [live demo](https://crazyhoesl.github.io/tailnet-atlas/) —
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
