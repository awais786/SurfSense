# SKILLS.md — Proxy Auth Integration Playbook

Repeatable pattern for integrating any app with an OIDC provider via
oauth2-proxy + Traefik ForwardAuth. Use as a checklist when onboarding a new app.

---

## Spec-Driven Anchored Development

Before writing any code, write the spec first. The spec is the contract —
it defines exactly what the middleware must do. Code is written to pass the spec.
The spec never changes unless the requirement changes.

### What "anchored" means

The spec acts as an anchor — it prevents scope creep, prevents over-engineering,
and gives a clear done condition. You know you are finished when all specs pass.

### The order

```
1. Write spec (GIVEN / WHEN / THEN)
     ↓
2. Confirm spec covers all cases
     ↓
3. Write the minimum code to pass the spec
     ↓
4. Verify all specs pass
     ↓
5. Refactor if needed — specs must still pass
```

### Spec template (GIVEN / WHEN / THEN)

Every test case must follow this structure:

```
GIVEN  <precondition — what is true before the action>
WHEN   <action — what happens>
THEN   <outcome — what must be true after>
```

Example:

```
GIVEN  a request with no X-Auth-Request-Email header
WHEN   the middleware processes the request
THEN   get_response is called
       AND login() is never called
       AND request.user remains anonymous
```

### Spec cases to always write (middleware)

Write these specs before any implementation:

| #   | GIVEN                                      | WHEN                       | THEN                                      |
| --- | ------------------------------------------ | -------------------------- | ----------------------------------------- |
| 1   | Kill switch is off                         | Any request arrives        | Middleware does nothing                   |
| 2   | User already has a session                 | Request arrives            | Skip — no DB, no login                    |
| 3   | Request path is a bypass path              | Request with email header  | Skip — no DB, no login                    |
| 4   | No email header                            | Request arrives            | Pass through unauthenticated              |
| 5   | Email is new (first seen)                  | Request with email header  | Create user, set unusable password, login |
| 6   | Email already exists                       | Request with email header  | Find user, no duplicate, login            |
| 7   | User exists but is inactive                | Request with email header  | Pass through unauthenticated              |
| 8   | Valid email header                         | Middleware runs            | login() called with correct args          |
| 9   | Email has uppercase / whitespace           | Request arrives            | Normalised before DB lookup               |
| 10  | Two concurrent requests for same new email | Both arrive simultaneously | Race condition handled, no crash          |

These 10 cases are the minimum viable spec for any proxy auth middleware
regardless of framework.

### How to use in a new session

When starting a new app integration, paste this into the session prompt:

```
Before writing any code:
1. Write the spec for the middleware using GIVEN/WHEN/THEN format
2. Cover all 10 cases from the spec template in SKILLS.md
3. Get confirmation the spec is complete
4. Only then write the implementation to pass the spec
5. Run tests and confirm all pass before declaring done
```

---

## The Universal Pattern

```
Layer 0 — Infrastructure (done once, shared across all apps)
  Traefik + oauth2-proxy + OIDC provider
  ↓ sets X-Auth-Request-Email on every authenticated request

Layer 1 — App middleware (~50 lines per app)
  Reads X-Auth-Request-Email header
  Finds or creates user in app's own user store
  Establishes app-native session

Layer 2 — Frontend (per app)
  401 interceptor → /oauth2/sign_in
  Logout clears all 3 session layers
```

Infrastructure is **identical** for all apps. Only Layer 1 and Layer 2 differ
per app because each app has its own user model and session mechanism.

---

## Integration Checklist (Per App)

### Infrastructure (shared, done once)

- [ ] Traefik with ForwardAuth middleware pointing to oauth2-proxy
- [ ] oauth2-proxy configured with OIDC provider, Redis session store
- [ ] `authResponseHeaders: X-Auth-Request-Email, X-Auth-Request-User`
- [ ] Bypass routers for admin routes, health checks, OPTIONS preflight, /oauth2/\*

### Backend (per app)

- [ ] Identify how the app stores users (model, table, unique field)
- [ ] Identify how the app establishes a session (login(), JWT, cookie, etc.)
- [ ] Write middleware/dependency that reads `X-Auth-Request-Email`
- [ ] Implement `get_or_create` user by email
- [ ] Set unusable/random password for proxy-auth users
- [ ] Handle race condition (concurrent requests creating the same user)
- [ ] Add bypass paths (admin routes that use local auth)
- [ ] Add kill switch env var (`<APP>_PROXY_AUTH_ENABLED`)
- [ ] Write tests (kill switch, no header, new user, existing user, bypass, inactive)

### Frontend (per app)

- [ ] 401 interceptor redirects to `/oauth2/sign_in?rd=<current_url>`
- [ ] Unauthenticated state redirects to `/oauth2/sign_in?rd=<current_url>`
- [ ] Logout clears Layer 1 (app session) + Layer 2 (oauth2-proxy) + Layer 3 (OIDC provider)
- [ ] `OIDC_LOGOUT_URL` env var for provider logout endpoint
- [ ] `OIDC_CLIENT_ID` env var

### OIDC provider app client (per deployment)

- [ ] Add `https://<app-host>/oauth2/callback` to allowed callback URLs
- [ ] Add `https://<app-host>` to allowed sign-out URLs

---

## Middleware Template Per Framework

### Django

```python
class ProxyAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, "<APP>_PROXY_AUTH_ENABLED", True)
        self.bypass_paths = getattr(settings, "<APP>_BYPASS_PATHS", ["/admin"])

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)
        if request.user.is_authenticated:
            return self.get_response(request)
        if any(request.path.startswith(p) for p in self.bypass_paths):
            return self.get_response(request)

        email = request.META.get("HTTP_X_AUTH_REQUEST_EMAIL")
        if not email:
            return self.get_response(request)

        user, created = User.objects.get_or_create(
            email=email.strip().lower(),
            defaults={"username": uuid4().hex, "password": make_password(None)}
        )
        if not user.is_active:
            return self.get_response(request)

        login(request, user)
        return self.get_response(request)
```

**Settings:** Add after `AuthenticationMiddleware` in `MIDDLEWARE` list.

---

### FastAPI

```python
class ProxyAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        email = request.headers.get("x-auth-request-email")
        if not email:
            return await call_next(request)

        # Skip if already authenticated
        # (check existing session/JWT depending on app's auth mechanism)

        normalised = email.strip().lower()
        user = await get_or_create_user(email=normalised)

        # Establish session
        # (depends on app's session mechanism — JWT, cookie, etc.)

        return await call_next(request)
```

**Setup:** Add to `app.add_middleware(ProxyAuthMiddleware)` in FastAPI app.

**FastAPI-specific notes:**
- FastAPI has no server-side session. The middleware runs on **every request**, not once
  per login. Throttle any per-user DB writes (e.g. `last_login`) to avoid write-per-request.
- `request.state.proxy_user` lives only for the current request cycle.
- Override `current_active_user` dependency to check `request.state.proxy_user` first,
  then fall back to JWT. Register the override **before** `include_router` — FastAPI uses
  first-match routing and the library's JWT-only route will win otherwise.

---

### Node.js (Express/Koa)

```javascript
async function proxyAuthMiddleware(ctx, next) {
  const email = ctx.headers["x-auth-request-email"];
  if (!email || ctx.state.user) return next();

  const normalised = email.trim().toLowerCase();
  let user = await User.findOne({ where: { email: normalised } });
  if (!user) {
    user = await User.create({ email: normalised });
  }

  // Establish session (depends on app's session mechanism)
  ctx.state.user = user;
  return next();
}
```

---

### Clojure (Ring)

```clojure
(defn proxy-auth-middleware [handler]
  (fn [request]
    (let [email (get-in request [:headers "x-auth-request-email"])]
      (if (or (nil? email) (authenticated? request))
        (handler request)
        (let [user (get-or-create-user! (normalize-email email))]
          (handler (assoc request :identity user)))))))
```

---

## Traefik Router Template (Per App)

```yaml
http:
  routers:
    # Protected routes — ForwardAuth required
    <app>-protected:
      entryPoints: [web]
      rule: "Host(`<app-host>`) && PathPrefix(`/api`)"
      priority: 10
      service: <app>
      middlewares:
        - oauth2-auth

    # Admin/bypass routes — no ForwardAuth
    <app>-admin:
      entryPoints: [web]
      rule: "Host(`<app-host>`) && PathPrefix(`/<admin-path>`)"
      priority: 20
      service: <app>

    # oauth2-proxy routes — always bypass
    oauth2-proxy:
      entryPoints: [web]
      rule: "PathPrefix(`/oauth2`)"
      priority: 30
      service: oauth2-proxy

  services:
    <app>:
      loadBalancer:
        servers:
          - url: "http://<app-container>:<port>"
```

---

## Logout Flow (Every App)

Three layers must be cleared in sequence:

| Layer | What                   | How                                                      |
| ----- | ---------------------- | -------------------------------------------------------- |
| 1     | App native session     | App's own logout endpoint                                |
| 2     | `_oauth2_proxy` cookie | `GET /oauth2/sign_out`                                   |
| 3     | OIDC provider session  | `GET <provider-logout-url>?client_id=...&logout_uri=...` |

**Frontend logout pattern:**

```javascript
async function signOut() {
  await appLogout(); // clears Layer 1

  const providerLogout = `${OIDC_LOGOUT_URL}?client_id=${OIDC_CLIENT_ID}&logout_uri=${encodeURIComponent(window.location.origin)}`;
  window.location.href = `/oauth2/sign_out?rd=${encodeURIComponent(providerLogout)}`;
  // clears Layer 2 then redirects to provider to clear Layer 3
}
```

---

## Environment Variables Template

### oauth2-proxy (shared across all apps)

```
OAUTH2_PROXY_PROVIDER=oidc
OAUTH2_PROXY_OIDC_ISSUER_URL=<issuer-url>
OAUTH2_PROXY_CLIENT_ID=<client-id>
OAUTH2_PROXY_CLIENT_SECRET=<client-secret>
OAUTH2_PROXY_COOKIE_SECRET=<32-byte-base64>
OAUTH2_PROXY_SESSION_STORE_TYPE=redis
OAUTH2_PROXY_REDIS_CONNECTION_URL=redis://<redis-host>:6379
OAUTH2_PROXY_SET_XAUTHREQUEST=true
OAUTH2_PROXY_EMAIL_DOMAINS=*
OAUTH2_PROXY_SCOPE=openid profile email
OAUTH2_PROXY_UPSTREAMS=static://202
```

### Per-app backend

```
<APP>_PROXY_AUTH_ENABLED=1          # kill switch, default on
<APP>_BYPASS_PATHS=/admin,/health   # comma-separated bypass paths
```

### Per-app frontend

```
OIDC_LOGOUT_URL=https://<provider-hosted-ui>/logout
OIDC_CLIENT_ID=<client-id>
```

---

## Tests Template

Every app should have tests covering these contracts:

| Test                  | What it verifies                                              |
| --------------------- | ------------------------------------------------------------- |
| Kill switch           | `<APP>_PROXY_AUTH_ENABLED=false` — middleware does nothing    |
| Already authenticated | Existing session — skip DB, skip login                        |
| No header             | Missing `X-Auth-Request-Email` — pass through unauthenticated |
| New user              | First-seen email — creates user, sets unusable password       |
| Existing user         | Known email — finds user, no duplicate                        |
| Inactive user         | `is_active=False` — pass through unauthenticated              |
| Login called          | `login()` called with correct args                            |
| Bypass path           | Admin route — skip DB, skip login                             |
| Email normalised      | `UPPER@EXAMPLE.COM` resolves same as `upper@example.com`      |
| Race condition        | `IntegrityError` on concurrent insert — falls back to get     |

---

## FastAPI / pytest-asyncio Test Pattern

Unit testing an async Starlette `BaseHTTPMiddleware` without a running server or DB.

### Key techniques

**1. Bypass `__init__` to avoid config/ASGI setup:**
```python
def _make_middleware(enabled=True, bypass_paths=None):
    mw = object.__new__(ProxyAuthMiddleware)
    mw.enabled = enabled
    mw.bypass_paths = bypass_paths or ["/health"]
    return mw
```

**2. Construct a minimal `Request` from a raw scope:**
```python
def _make_request(path="/api/data", headers=None):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "GET", "path": path,
        "headers": raw_headers, "query_string": b"", "root_path": "",
    }
    return Request(scope)
```

**3. Mock `async_session_maker` as a callable returning an async context manager:**
```python
session = AsyncMock()
session.add = MagicMock()  # add() is sync — AsyncMock makes it async by default → warning
cm = MagicMock()
cm.__aenter__ = AsyncMock(return_value=session)
cm.__aexit__ = AsyncMock(return_value=False)

with patch("app.middleware.proxy_auth.async_session_maker", return_value=cm):
    ...
```

**4. Two-session test (new user + `on_after_register`):**
```python
with patch("app.middleware.proxy_auth.async_session_maker", side_effect=[s1_cm, s2_cm]), \
     patch("app.users.UserManager", return_value=mock_manager), \
     patch("app.middleware.proxy_auth.SQLAlchemyUserDatabase"):
    await mw.dispatch(request, call_next)
```

**5. Race-condition test — `side_effect` list on commit:**
```python
session.commit = AsyncMock(side_effect=[IntegrityError(None, None, None), None])
# First commit raises, second succeeds
```

**6. `asyncio_mode = "auto"` in `pyproject.toml`** — no `@pytest.mark.asyncio` needed per test.

### `session.add` warning

`AsyncMock` makes **all** attribute access return async mocks, including sync methods like
`session.add`. Call `session.add = MagicMock()` explicitly to silence the
`RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited` warning.

---

## Key Lessons Learned

1. **Stale container environment** — `docker compose restart` does NOT pick up
   env changes. Always use `--force-recreate` after changing env vars.

2. **Redis session store is required** — OIDC provider JWTs are often large
   (>4 KB). Without Redis, oauth2-proxy splits sessions across multiple cookies
   and fails to reassemble them → infinite redirect loop.

3. **3-layer logout** — clearing only the app session is not enough. The
   oauth2-proxy cookie immediately re-authenticates the user on the next request.

4. **Sign-out URL must be registered** — `logout_uri` must exactly match a
   registered allowed sign-out URL in the OIDC provider (http vs https matters).

5. **Fork over package** — a shared package works for the backend but the
   frontend still needs patching per app. A thin fork is simpler than maintaining
   both a package and a fork.

6. **Header spoofing is not a concern** — Traefik ForwardAuth overwrites any
   client-supplied `X-Auth-Request-*` headers before they reach the app.

7. **Bind backend port to localhost only** — expose the app port as
   `127.0.0.1:<port>:<port>` not `<port>:<port>` in docker-compose. Otherwise
   anyone on the same network can hit the API directly and bypass ForwardAuth
   entirely with spoofed headers.

8. **Use URL API for logout URL construction** — build the OIDC logout URL using
   `new URL()` + `searchParams.set()` instead of string concatenation. Handles
   encoding automatically and won't break if the base URL already has query params.

9. **FastAPI stateless — throttle DB writes in middleware** — unlike Django (one
   middleware call per session), FastAPI middleware runs on every request. Any DB write
   inside the middleware (e.g. `last_login`) must be throttled or it adds a
   `UPDATE + COMMIT` to every API call.

10. **FastAPI first-match routing** — when overriding a route already registered by a
    third-party router (e.g. fastapi-users `/users/me`), register your override BEFORE
    `app.include_router(...)`. The first matching route wins; the override will be silently
    ignored if it is added after.

11. **`DetachedInstanceError` across sessions** — after an `IntegrityError` race condition,
    the SQLAlchemy user object is attached to a rolled-back (dead) session. Always re-fetch
    the user in any new session using `select(User).where(User.id == user.id)` before
    passing it to code that may lazy-load attributes.

12. **Python-level UUID default** — `SQLAlchemyBaseUserTableUUID` uses
    `default=uuid.uuid4` (Python-level, not server default). The `id` is populated on
    object construction, so `user.id` is valid before the INSERT is committed. Safe to
    use as a lookup key in a subsequent session.
