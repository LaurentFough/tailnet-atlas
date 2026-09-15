"""Tailnet Atlas — serves the page and keeps it fed with live data.

One process, one URL, several tailnets: `render.REGISTRY` holds a `Tailnet`
per configured org (see tailnets.toml), and this module owns exactly the
per-tailnet cache/refresh loop that used to be a single set of globals. A
background loop rebuilds each tailnet on its own schedule, and what gets
served for a given `net=` is always the last SUCCESSFULLY built version for
THAT tailnet. If one tailnet's API is down, only its own map grows a
"stale" banner — the others are unaffected.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

import render
from render import REGISTRY, DEFAULT_SLUG

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

INTERVAL = int(os.environ.get("REFRESH_SECONDS", "300"))
# Floor for live polling: however many browsers poll us, we hit any one
# tailnet's API at most once per this many seconds.
LIVE_TTL = int(os.environ.get("LIVE_TTL_SECONDS", "9"))
app = FastAPI(title="tailnet")


class _Cache:
    """Everything refresh() needs to hold for one tailnet. One per entry in
    the registry — the coalescing lock is per-tailnet so a slow poll on one
    org never blocks another's."""

    def __init__(self, tn: render.Tailnet):
        self.tn = tn
        self.html = None
        self.data = None
        self.at = None
        self.error = None
        self.renders = 0
        self.fails = 0
        self.live = 0
        self.lock = threading.Lock()
        # held only while a refresh is in flight, so concurrent viewers
        # coalesce into one upstream fetch instead of stampeding
        self.fetching = threading.Lock()

    def refresh(self) -> bool:
        try:
            data = self.tn.build()
            html = self.tn.render_html(data)
        except Exception as e:
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"
                self.fails += 1
            log.warning("[%s] render failed: %s", self.tn.slug, e)
            return False
        with self.lock:
            self.html, self.data, self.error = html, data, None
            self.at = datetime.now(timezone.utc)
            self.renders += 1
        log.info("[%s] re-rendered (%d chars)", self.tn.slug, len(html))
        return True

    def age_seconds(self) -> float:
        with self.lock:
            at = self.at
        return 1e9 if at is None else (datetime.now(timezone.utc) - at).total_seconds()

    def refresh_if_stale(self, max_age: float) -> None:
        if self.age_seconds() <= max_age:
            return
        if not self.fetching.acquire(blocking=False):
            return
        try:
            if self.age_seconds() > max_age:
                self.refresh()
        finally:
            self.fetching.release()

    def loop(self):
        while True:
            time.sleep(INTERVAL)
            self.refresh()


CACHES: dict[str, _Cache] = {slug: _Cache(tn) for slug, tn in REGISTRY.items()}


def _cache(net: str | None) -> _Cache | None:
    return CACHES.get(net or DEFAULT_SLUG)


BANNER = ('<div style="position:fixed;left:0;right:0;bottom:0;z-index:99;'
          'background:#7a2233;color:#fff;font:13px/1.5 system-ui;'
          'padding:8px 14px;text-align:center">Data is stale — last update '
          '{age} ago ({err})</div>')


@app.get("/api/tailnets")
def api_tailnets():
    """Lets the in-app switcher list what it can show without hard-coding it
    into the template."""
    return JSONResponse({"default": DEFAULT_SLUG,
                          "tailnets": [{"slug": s, "label": c.tn.label_short}
                                       for s, c in CACHES.items()]})


@app.get("/api/state")
def api_state(net: str | None = Query(None)):
    """Data-only view for the live poll — a few KB instead of the whole page.
    `net` picks which tailnet's cache to serve; unknown/omitted falls back to
    the default."""
    c = _cache(net)
    if c is None:
        return JSONResponse({"error": f"unknown tailnet '{net}'"}, status_code=404)
    c.refresh_if_stale(LIVE_TTL)
    with c.lock:
        data, err, at = c.data, c.error, c.at
        c.live += 1
    if not data:
        return JSONResponse({"error": err or "not ready"}, status_code=503)
    out = dict(data)
    out["age"] = round(c.age_seconds(), 1)
    out["error"] = err
    out["at"] = at.isoformat() if at else None
    return JSONResponse(out)


@app.get("/", response_class=HTMLResponse)
def index(net: str | None = Query(None)):
    # A page load should show current data, not whatever the 5-minute loop
    # last left behind — but reuse anything fetched in the last few seconds.
    c = _cache(net)
    if c is None:
        return HTMLResponse(f"unknown tailnet '{net}'", status_code=404)
    c.refresh_if_stale(LIVE_TTL)
    with c.lock:
        html, at, err = c.html, c.at, c.error
    if not html:
        return HTMLResponse(
            "<html><body style='font:15px system-ui;padding:40px'>"
            "Building the tailnet view for the first time…"
            f"{'<br><br>Error: ' + err if err else ''}"
            "<script>setTimeout(()=>location.reload(),5000)</script>"
            "</body></html>", status_code=503)
    if err and at:
        mins = int((datetime.now(timezone.utc) - at).total_seconds() // 60)
        html += BANNER.format(age=f"{mins} min", err=err[:90])
    return HTMLResponse(html)


@app.get("/healthz")
def healthz():
    out = {}
    ok = True
    for slug, c in CACHES.items():
        with c.lock:
            at, err, n, f = c.at, c.error, c.renders, c.fails
        have_html = bool(c.html)
        ok = ok and have_html
        out[slug] = {"ok": have_html, "last_render": at.isoformat() if at else None,
                     "error": err, "renders": n, "failures": f,
                     "age_seconds": round(c.age_seconds(), 1)}
    return JSONResponse({"ok": ok, "interval_seconds": INTERVAL,
                         "live_ttl_seconds": LIVE_TTL, "tailnets": out})


@app.post("/api/refresh")
def api_refresh(net: str | None = Query(None)):
    c = _cache(net)
    if c is None:
        return JSONResponse({"status": "error", "note": f"unknown tailnet '{net}'"},
                            status_code=404)
    ok = c.refresh()
    return JSONResponse({"status": "ok" if ok else "error",
                         "note": "reloaded" if ok else c.error})


def main():
    for slug, c in CACHES.items():
        log.info("[%s] initial build…", slug)
        if not c.refresh():
            log.warning("[%s] initial build failed — retrying in the background", slug)
        threading.Thread(target=c.loop, daemon=True).start()
    log.info("background refresh every %d s, %d tailnet(s): %s",
             INTERVAL, len(CACHES), ", ".join(CACHES))
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
