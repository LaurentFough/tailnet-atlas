# ToDone

Things we know about and have deliberately left for later.

## Users list needs an API key, not an OAuth token — workaround in place

**Symptom.** Every refresh logged, for every configured tailnet:

```
WARNING render: [<slug>] users not available: Client error '404 Not Found'
  for url 'https://api.tailscale.com/api/v2/tailnet/-/users'
```

**Cause (found by hand with `curl`).** `GET /api/v2/tailnet/<id or ->/users` answers
`404 {"message":"not found"}` to an **OAuth token, whatever its scopes** (adding
`users:read` changed nothing), on both tailnets and with the real tailnet ID. The
same call with a user **API key** (`tskey-api-…`) returns the users. Devices and the
policy file work fine with OAuth. Why Tailscale does this is unknown; we found no
announcement that the endpoint changed.

**What the code does now.**

- Optional `api_key_env` per tailnet in `tailnets.toml` names an env var holding an
  API key. If set, it is used for the users call **only**; everything else still uses
  the read-only OAuth client. See `tailnets.example.toml` and `.env.example`.
- Without a key, the Owners table is derived from the device list. Lost: display
  names, roles, and the "self" ownership ring on the map (it is the owner flagged by
  role in the API response).
- After a 404, the call is skipped for 24 h (`USERS_RETRY_AFTER` in `app/render.py`),
  then tried once more. Every real attempt still logs a warning with Tailscale's
  response body. Other failures are retried on the next refresh.

**Still to do.**

1. **Choose the key's owner carefully.** An API key acts as the user who created it
   and cannot be limited to read-only. Create it under the least-privileged account
   that can list users (check which role that is), not a full admin if avoidable.
2. **Keys expire** (90 days at most). When one does, the log says "API key rejected:
   expired or revoked?" and the page falls back as above. Put a reminder in your
   calendar, or consider a nicer way to surface it on the page.
3. **Look for an OAuth-friendly route.** If Tailscale starts accepting OAuth for this
   endpoint, or offers another way to read user roles, drop the API key.
4. **Fix the "self" ring without the users API**, e.g. derive the primary owner some
   other way, if running without a key is a case worth supporting.
