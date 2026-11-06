# ToDone

Things we know about and have deliberately left for later.

## Users list: fixed with an API key — follow-ups remain

**Resolved.** The users endpoint (`GET /api/v2/tailnet/-/users`) answers
`404 {"message":"not found"}` to an **OAuth token, whatever its scopes** (adding
`users:read` changed nothing; both tailnets, with `-` and with the real tailnet ID).
The same call with a user **API key** (`tskey-api-…`) returns the users. Why
Tailscale does this is unknown; we found no announcement that the endpoint changed.

An optional per-tailnet `api_key_env` in `tailnets.toml` now names an env var holding
an API key, used for the users call **only** — devices and the policy file still use
the read-only OAuth client. See `tailnets.example.toml` and `.env.example`. After
adding it, the app image must be **rebuilt** (`docker compose up -d --build app`);
`render.py` is baked into the image, and a plain `restart` does not re-read `.env`.
Confirmed working after the rebuild: no `users not available` warning in the logs.

Without a key the page still works: the Owners table is derived from the device list
(no display names, roles, or "self" ring), the 404 is logged with Tailscale's
response body, and the call is skipped for 24 h (`USERS_RETRY_AFTER` in
`app/render.py`) before one more try.

**Still to do.**

1. **Least-privilege key owner.** An API key acts as the user who created it and cannot
   be limited to read-only. Check which role is enough to list users and create the
   keys under that account rather than a full admin.
2. **Expiry.** Keys last 90 days at most. When one expires the log says "API key
   rejected: expired or revoked?" and the page falls back as above. Set a calendar
   reminder, or surface it on the page.
3. **OAuth later.** If Tailscale starts accepting OAuth for this endpoint, drop the
   API keys.
4. **No-key case.** The "self" ownership ring needs the users API (it is the owner
   flagged by role). Derive it another way if running without a key is worth
   supporting.
