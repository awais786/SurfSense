# CLAUDE.md — mPass OIDC Integration: SurfSense

## Project Overview

Same mPass integration as Plane — oauth2-proxy sits in front of SurfSense and injects
`X-Auth-Request-Email` / `X-Auth-Request-User` headers on every authenticated request.
A **Starlette middleware** (same pattern as Plane's Django middleware) reads the header,
finds or creates a user, and injects them into `request.state` so the existing
`current_active_user` FastAPI dependency sees a fully authenticated user.

**No OIDC code lives in SurfSense.** oauth2-proxy owns the entire Cognito handshake.

---

## Architecture (same as Plane)

```
User Browser
    ↓
Traefik  →  ForwardAuth  →  oauth2-proxy  →  mPass/Cognito
    ↓
X-Auth-Request-Email: ali@moneta.com
X-Auth-Request-User:  <cognito-sub>
    ↓
SurfSense FastAPI
    → ProxyAuthMiddleware  →  find/create User  →  request.state.proxy_user = user
    → current_active_user dependency checks request.state.proxy_user first
    → route handler sees authenticated user — no JWT needed
```

---

## SurfSense Tech Stack (relevant to this task)

| Concern | Detail |
|---------|--------|
| Framework | FastAPI |
| Auth library | fastapi-users v15.0.3+ |
| Auth backend | `JWTStrategy` + `CustomBearerTransport` (`app/users.py`) |
| `current_active_user` | `fastapi_users.current_user(active=True)` — line 301 `app/users.py` |
| ORM | SQLAlchemy 2.0 async (`asyncpg`) |
| DB | PostgreSQL |
| User model | `User` in `app/db.py` — extends `SQLAlchemyBaseUserTableUUID` |
| User manager | `UserManager` in `app/users.py` — extends `UUIDIDMixin`, `BaseUserManager` |
| Config | `app/config/__init__.py`, loaded from `.env` via `python-dotenv` |
| Entry point | `main.py` → `app/app.py` |

---

## User Model

**File:** `surfsense_backend/app/db.py`
**Class:** `User` (extends `SQLAlchemyBaseUserTableUUID`)

Fields relevant to provisioning:

| Field | Type | Set on proxy-auth creation |
|-------|------|---------------------------|
| `id` | UUID PK | auto |
| `email` | String, unique | from header (normalised) |
| `hashed_password` | String | `secrets.token_urlsafe(32)` — random, never exposed |
| `is_active` | Boolean | `True` |
| `is_verified` | Boolean | `True` — proxy auth is verified by definition |
| `is_superuser` | Boolean | `False` |
| `display_name` | Optional[String] | `None` (can sync from Cognito claim later) |
| `avatar_url` | Optional[String] | `None` |

**Hooks that must still fire** (`app/users.py`):
- `on_after_register()` — creates default SearchSpace + RBAC roles + system prompts for every new user
- `on_after_login()` — updates `last_login` timestamp

---

## How the Middleware Pattern Works

### Plane (Django) vs SurfSense (FastAPI)

| Concern | Plane | SurfSense |
|---------|-------|-----------|
| Middleware type | Django WSGI middleware | Starlette `BaseHTTPMiddleware` |
| User injection | `user_login()` → Django session | `request.state.proxy_user = user` |
| Auth check in routes | `request.user.is_authenticated` | `current_active_user` dependency |
| Already-authed check | `request.user.is_authenticated` | `request.state.proxy_user` already set |
| DB access in middleware | Django ORM (sync) | `async_session_maker()` (async) |

### Override `current_active_user`

`current_active_user` (line 301 `app/users.py`) is a FastAPI dependency that reads a JWT
from `Authorization: Bearer`. We override it to check `request.state.proxy_user` first:

```python
# app/users.py  — replace the last two lines with:

_jwt_current_active_user = fastapi_users.current_user(active=True)
_jwt_current_optional_user = fastapi_users.current_user(active=True, optional=True)

async def current_active_user(
    request: Request,
    jwt_user: User | None = Depends(_jwt_current_optional_user),
) -> User:
    proxy_user = getattr(request.state, "proxy_user", None)
    if proxy_user is not None:
        return proxy_user
    if jwt_user is not None:
        return jwt_user
    from fastapi import HTTPException, status
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

async def current_optional_user(
    request: Request,
    jwt_user: User | None = Depends(_jwt_current_optional_user),
) -> User | None:
    proxy_user = getattr(request.state, "proxy_user", None)
    return proxy_user if proxy_user is not None else jwt_user
```

All existing route handlers that `Depends(current_active_user)` continue to work unchanged.

---

## Files Created / Modified

| File | Status | Notes |
|------|--------|-------|
| `app/middleware/proxy_auth.py` | **Done** | Starlette middleware — core logic |
| `app/users.py` | **Done** | Override `current_active_user` / `current_optional_user` |
| `app/app.py` | **Done** | Register middleware; conditionally remove native auth routers; `/users/me` override |
| `app/config/__init__.py` | **Done** | Added `MPASS_PROXY_AUTH_ENABLED` (default `false`), `MPASS_BYPASS_PATHS` |
| `tests/unit/middleware/test_proxy_auth.py` | **Done** | 25 unit tests — all 10 SKILLS.md spec cases pass |
| `docker-compose.local.yml` | **Done** | Standalone local dev stack (Traefik + oauth2-proxy + db + redis + backend) |
| `config/traefik/traefik.yml` | **Done** | Single entrypoint `web` on port 8929 |
| `config/traefik/dynamic.yml` | **Done** | ForwardAuth middleware; bypass routers for `/health` and `/oauth2` |

---

## Middleware Implementation

**File:** `surfsense_backend/app/middleware/proxy_auth.py`

Design contract (mirrors `proxy_auth_core.py` design in Plane):

- Reads `X-Auth-Request-Email` from `request.headers`
- If `MPASS_PROXY_AUTH_ENABLED` is `False` → pass through (kill switch)
- If `request.state.proxy_user` already set → pass through (idempotent)
- If path starts with a bypass prefix → pass through
  Default bypass prefixes: `["/health"]`
- If email header is absent → pass through unauthenticated
- If email present → `get_or_create` User via `async_session_maker` directly
  (not via `get_user_db` / `get_user_manager` — those are request-scoped dependencies
  and cannot be called from middleware)
- New users: `hashed_password = get_password_hash(secrets.token_urlsafe(32))`,
  `is_verified=True`, `is_active=True`
- After creation: manually trigger `on_after_register()` (creates SearchSpace + roles)
- After get-or-create: set `request.state.proxy_user = user`
- After get-or-create: update `last_login` (mirrors `on_after_login`)
- Inactive users: pass through unauthenticated even with valid header
- `IntegrityError` on concurrent creation → fallback to `select` by email,
  re-raise if user still not found

### Core utilities — inline (no PyPI package)

The helpers (`_normalise_email`, `_is_bypass_path`, `_coerce_bypass_paths`) are defined
directly in `app/middleware/proxy_auth.py`. The `mpass-proxy-auth` PyPI package was
considered but rejected — 12 lines, 2 apps, package overhead not worth it.

Key difference from the Django/Plane version:
- SurfSense uses `is_verified=True` (not `is_email_verified=True`)
- No `NEW_USER_FLAGS` dict — fields are set explicitly on the `User(...)` constructor

---

## Registration in `app/app.py`

### Add middleware (after existing middleware registrations):

```python
from app.middleware.proxy_auth import ProxyAuthMiddleware
app.add_middleware(ProxyAuthMiddleware)
```

Starlette processes middleware in **reverse registration order** — add it last so it runs
before the existing `ProxyHeadersMiddleware`, `SlowAPIMiddleware`, and `CORSMiddleware`.

### Conditionally disable native auth routes:

```python
if not config.MPASS_PROXY_AUTH_ENABLED:
    app.include_router(
        fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"]
    )
    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"]
    )
    app.include_router(
        fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"]
    )
    app.include_router(
        fastapi_users.get_verify_router(UserRead), prefix="/auth", tags=["auth"]
    )
    # Google OAuth router only when both native auth and Google are configured
    if config.AUTH_TYPE == "GOOGLE":
        app.include_router(google_oauth_router, prefix="/auth/google", tags=["auth"])
```

Always keep (proxy auth or not):
- `/auth/jwt/refresh`
- `/auth/jwt/revoke`
- `/auth/jwt/logout-all`
- `/users/me`
- `/verify-token`
- `/health`

---

## MIDDLEWARE order in `app/app.py` (after our change)

Starlette processes in reverse — bottom-most `add_middleware` runs first on request:

```
CORSMiddleware          ← outermost (added first)
SlowAPIMiddleware
ProxyHeadersMiddleware
RequestPerfMiddleware
ProxyAuthMiddleware     ← innermost (added last) — reads header, injects user
```

---

## Config Changes (`app/config/__init__.py`)

Add to `Config` class:

```python
MPASS_PROXY_AUTH_ENABLED = os.getenv("MPASS_PROXY_AUTH_ENABLED", "false").lower() == "true"
MPASS_BYPASS_PATHS = os.getenv("MPASS_BYPASS_PATHS", None)  # optional override
```

Add to `.env.example`:

```
# mPass proxy auth
MPASS_PROXY_AUTH_ENABLED=true
# MPASS_BYPASS_PATHS=/health   # comma-separated, defaults to /health
```

---

## Auth Routes: Keep / Remove

| Endpoint | Action | Reason |
|----------|--------|--------|
| `POST /auth/jwt/login` | **Remove** when proxy auth on | oauth2-proxy handles login |
| `POST /auth/register` | **Remove** when proxy auth on | auto-provisioned by middleware |
| `POST /auth/forgot-password` | **Remove** when proxy auth on | no passwords |
| `POST /auth/reset-password` | **Remove** when proxy auth on | no passwords |
| `POST /auth/request-verify-token` | **Remove** when proxy auth on | all users pre-verified |
| `POST /auth/verify` | **Remove** when proxy auth on | all users pre-verified |
| `GET /auth/google/authorize` | **Remove** when proxy auth on | oauth2-proxy handles OAuth |
| `GET /auth/google/authorize-redirect` | **Remove** when proxy auth on | same |
| `GET /auth/google/callback` | **Remove** when proxy auth on | same |
| `POST /auth/jwt/refresh` | **Keep** | token rotation still used |
| `POST /auth/jwt/revoke` | **Keep** | per-device logout |
| `POST /auth/jwt/logout-all` | **Keep** | logout all devices |
| `GET/PATCH /users/me` | **Keep** | profile management |
| `GET /verify-token` | **Keep** | frontend token check |
| `GET /health` | **Keep** | always unauthenticated |

---

## Environment Variables

### New
- `MPASS_PROXY_AUTH_ENABLED` — `true` / `false` (default: `true`)
- `MPASS_BYPASS_PATHS` — optional comma-separated list (default: `/health`)

### Remove (when proxy auth is active)
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `REGISTRATION_ENABLED`

### Keep
- `SECRET_KEY` — JWT signing (still used for `/auth/jwt/refresh`)
- `DATABASE_URL`
- `REDIS_APP_URL`
- `ACCESS_TOKEN_LIFETIME_SECONDS`
- `REFRESH_TOKEN_LIFETIME_SECONDS`
- `NEXT_FRONTEND_URL`

---

## Security Notes

- Traefik ForwardAuth **overwrites** `X-Auth-Request-*` headers — spoofing impossible on protected routes
- Bypass paths (`/health`) never reach the middleware, so spoofed headers there have no effect
- Users created with random hashed password — cannot log in via password
- Existing refresh-token rotation (`RefreshToken` + `family_id`) unchanged
- Inactive users pass through unauthenticated even with a valid header

---

## Delivery

Same as Plane — git patch applied via wrapper Dockerfile:

```
patches/surfsense/
  001-proxy-auth-middleware.patch
  002-disable-native-auth.patch
  Dockerfile.backend
  Dockerfile.web
```

---

## SurfSense-specific Implementation Notes

### FastAPI stateless vs Django session-based

Unlike Plane (Django), FastAPI has no server-side sessions. The middleware runs on **every
single API call**, not once per login session. Consequences:

- **`last_login` is throttled to once per 5 minutes** (`_LAST_LOGIN_THROTTLE_SECONDS = 300`)
  to avoid an `UPDATE + COMMIT` on every request.
- **`request.state.proxy_user`** lives only for the duration of a single request.
  There is no equivalent of "already authenticated via session" — the DB check happens
  every request (SELECT is cheap; mitigate further with Redis cache if needed).

### FastAPI route precedence

FastAPI uses **first-match routing**. `/users/me` override must be registered
**before** `app.include_router(fastapi_users.get_users_router(...))`, otherwise
fastapi-users' internal JWT-only route wins and `current_active_user` is never called.

### Two-session pattern for `on_after_register`

After creating a new user, `on_after_register` is triggered in a **fresh session** to
avoid `DetachedInstanceError`. The user object is re-fetched in `reg_session` by `user.id`
(which is a Python-level UUID generated at object construction, not a DB serial).

---

## Open Questions

1. **Frontend behaviour** — does the web app call `/auth/jwt/refresh` on load? If proxy
   auth is on and that route is removed, the frontend may break. Confirm before removing.
2. **Connector OAuth** (Google Drive, Gmail, Calendar) — these use separate OAuth flows
   for *connectors*, not user auth. They must remain unaffected by this change.
3. **Display name sync** — Cognito can pass a name claim. Populate `display_name` from
   a separate header on first creation? Currently left `None`.
4. **Redis cache for middleware** — avoid the per-request SELECT with a short-lived
   Redis cache keyed by email. Not yet implemented.
