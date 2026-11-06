# ToDone

Things we know about and have deliberately left for later.

## Tailscale users API returns 404 — fix later

**Symptom.** Every refresh logged, for every configured tailnet:

```
WARNING render: [<slug>] users not available: Client error '404 Not Found'
  for url 'https://api.tailscale.com/api/v2/tailnet/-/users'
```

**Impact.** Small. `Tailnet.fetch_users()` catches it, and `build_users()` derives
the Owners table from the device list instead. What is lost:

- each owner's display name, role and status (only the login is shown),
- the "self" ownership ring on the map: it is the owner *flagged by role in the
  API response*, so without it every user-owned device counts as "other".

Devices, grants and the map itself are unaffected.

**What we established** (checked by hand with `curl` and the same OAuth token that
works for devices and the policy file):

- Both tailnets return 404, so it is not one client's scopes.
- `GET /api/v2/tailnet/-/users` → `404 {"message":"not found"}` — an
  application-level answer from a route that exists.
- A path with no `tailnet/` segment → `404 {"message":"404 page not found"}` — the
  router's generic reply. That one was a malformed path, so it proves nothing.
- `GET /api/v2/tailnet/<real-tailnet-id>/users` (correct path, real ID) → the same
  `404 {"message":"not found"}`, **after** the `users:read` scope was added to the
  client. So neither the scope, the `-` shorthand nor the path is the cause.
- Not yet tried: a user-scoped API key (`tskey-api-…`) instead of an OAuth token;
  a brand-new token minted *after* the scope change (an old `$TOKEN` keeps its old
  scopes); a control call to a sibling endpoint (e.g. `/tailnet/-/keys`,
  `/tailnet/-/user-invites`) to see whether the whole users family 404s.
- Searching found no announcement that the endpoint was removed or changed.

**Ideas for the real fix.**

1. Find the current endpoint / required scope in Tailscale's API reference, or
   whether it needs an API key or a particular plan.
2. If it is gone for good, drop the call and derive display names some other way
   (e.g. from the devices' `user` field), and decide how to mark the "self" owner.
3. Update the README / `.env.example` scope list to match whatever turns out to be
   required.

**What the code does meanwhile.** After a 404 the app stops asking for 24 hours
(`USERS_RETRY_AFTER` in `app/render.py`) and then tries once more. The warning is
still logged on every real attempt, now including Tailscale's response body. Other
failures (timeouts, 5xx, auth) are retried on the next refresh. A success clears the
backoff.
