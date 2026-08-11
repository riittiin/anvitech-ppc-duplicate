# Two-role login (Admin / User) — Design

**Date:** 2026-06-25
**Status:** approved (pending spec review)
**Branch:** `two-role-login`

## Goal

Replace the single HTTP Basic Auth gate (browser popup, one shared
`APP_USERNAME`/`APP_PASSWORD`) with an **app-owned login page** that supports
**two roles** with different permissions:

- **Admin** — everything the app can do today (full control).
- **User** — read-only view of all tabs, plus exactly two actions: download the
  Rule 6 machine-allocation sheet, and submit Capture Actuals (Rule 7) entries.

The login logic is — and already was — entirely the app's own code; Render only
stored the password values as environment variables. This change makes that
ownership visible (a real login screen + logout) and adds the role split.

## Decisions (confirmed with the user)

1. **Login mechanism:** a real in-app login page backed by a **signed session
   cookie**, replacing the browser Basic-Auth popup. No new third-party library —
   the cookie is signed with Python's stdlib `hmac`/`hashlib`, matching this
   project's keep-dependencies-minimal style.
2. **Credentials location:** **baked into the code** (user's explicit choice), in
   one clearly-marked block, with an **optional env-var override** so a password
   can be changed in Render later without a code edit.
3. **Credentials (baked defaults):**
   | Role | Username | Password |
   |---|---|---|
   | admin | `anvitech` | `1930rail` |
   | user | `anvitech_user` | `anvitech12345678` |
4. **User sees everything read-only**, but the only things a user can *act on* are
   the Rule 6 download and the Capture Actuals form.
5. **User has no Plan button.** Instead, the user always sees **the admin's
   last-saved plan settings**, so a downloaded sheet matches what the planner set
   up. (Achieved by persisting the plan config — see below.)
6. **"Mark order complete" is available to BOTH roles** — the floor user marks an
   SO complete from the Capture Actuals form (it's a shop-floor signal that the SO
   is done), so the checkbox stays visible for users and is accepted server-side.
7. **Security is a first-class requirement.** The whole change is hardened against
   the common ways a small web app gets compromised — see "Security hardening".

## Permission matrix

| Capability | Admin | User |
|---|---|---|
| View Orders / Rules 1–8 / Gantt | ✅ | ✅ (read-only) |
| Config bar (window, setup, overlap, priority, downtime toggle) | ✅ | ❌ hidden |
| Plan button (`POST /run`, `POST /rerun`) | ✅ | ❌ hidden; auto-loads admin's plan |
| Upload Excel (`POST /upload`) | ✅ | ❌ 403 |
| Delete selected / Delete all (`/orders/delete`, `/orders/clear`) | ✅ | ❌ 403 |
| Download Rule 6 allocation CSV / machine-wise CSV | ✅ | ✅ |
| Capture Actuals — submit production (`POST /actuals`) | ✅ | ✅ |
| "Mark order complete" (`mark_complete=true`) | ✅ | ✅ |
| Read endpoints (`/orders`, `/gantt`, `/items`, `/report`, `/trace`) | ✅ | ✅ |

**Enforcement is server-side** (not just hidden in the UI). UI hiding is polish on
top so users never see controls they can't use.

## Architecture

### New: `api/auth.py`
A small, focused module — the only place that knows about accounts and sessions.

- `ACCOUNTS`: the baked-in credential block at the top of the file:
  ```python
  ACCOUNTS = {
      "anvitech":      {"password": "1930rail",        "role": "admin"},
      "anvitech_user": {"password": "anvitech12345678", "role": "user"},
  }
  ```
  Each account may be overridden by env vars if present (back-door, no code edit):
  `ADMIN_USERNAME` / `ADMIN_PASSWORD`, `USER_USERNAME` / `USER_PASSWORD`.
  (Legacy `APP_USERNAME`/`APP_PASSWORD`, if set, override the admin account too,
  so an existing Render deploy keeps working through the transition.)
- `authenticate(username, password) -> role | None` — constant-time compare
  (`secrets.compare_digest`); returns the role on success, else `None`.
- **Session secret** `_secret() -> bytes`:
  1. `SESSION_SECRET` env var if set, else
  2. a value persisted in the durable store (`anvitech:session_secret`), else
  3. generate a random one (`secrets.token_hex`), persist it, and use it.
  This keeps sessions stable across restarts with **zero required env vars** — no
  third-party dependency — while allowing an explicit override.
- `authenticate` is **constant-time even for an unknown username** — it compares
  against a fixed dummy hash when the user doesn't exist, so response timing never
  reveals whether a username is valid.
- `make_token(username, role) -> str` — base64 of `{u, role, iat}` + an
  HMAC-SHA256 signature, joined by `.`.
- `verify_token(token) -> {u, role} | None` — re-computes the HMAC
  (constant-time), rejects tampered/garbage tokens, tokens with a missing/garbled
  payload, and tokens older than `MAX_AGE_DAYS` (**7**, the single authoritative
  expiry). Returns the payload or `None`.
- The secret is resolved **once at startup** (FastAPI startup event), cached
  in-process keyed by store-config (so per-test `STORE_DIR` swaps stay isolated),
  avoiding both a per-request store round-trip and a first-request generate race.

The cookie is named `anvitech_session`, set `HttpOnly`, `SameSite=Lax`,
`Secure` when the request is HTTPS, `Path=/`, and `Max-Age = 7 days` (matching the
server-side `iat` check, which is authoritative). `Lax` (not `Strict`) avoids the
"appears logged-out on first click from a bookmark/link" surprise while still not
sending the cookie on cross-site POSTs; CSRF is further covered by the Origin
check below.

### Changed: `api/main.py`

- **Replace** the `basic_auth` middleware with a `session_auth` middleware:
  - Public paths are an **exact `(method, path)` allowlist** — exactly
    `{(GET, /login), (POST, /login), (POST, /logout)}`. **Never** matched by prefix
    or file extension. The login page is fully self-contained (inline CSS, no JS),
    so no static asset needs to be public. Everything else — `/`, `/index.html`,
    `/app.js`, `/style.css`, every other static path and API route — requires a
    valid session.
  - Otherwise: read + verify the session cookie.
    - Invalid/missing **and** it's a browser navigation (`GET` with
      `Accept: text/html`) → `302` redirect to `/login`.
    - Invalid/missing otherwise (API/XHR) → `401`.
    - Valid → stash `request.state.user` / `request.state.role` and continue.
- **New routes** (declared before the static mount so they take precedence; the
  static mount must stay last so `/login` etc. resolve, avoiding a redirect loop):
  - `GET  /login` → serve `web/login.html`.
  - `POST /login` (HTML form post: `username`, `password`) → on success set cookie
    + `303` redirect to `/`; on failure re-serve the login page with a generic
    error. **Native form post — no JavaScript on the login page** (keeps CSP
    `script-src 'self'` strict with no extra allowlisted asset). The login flow
    **never** reflects a user-supplied `next`/return URL — the redirect target is
    the hardcoded `/` (no open-redirect).
  - `POST /logout` → clear the cookie (same attributes), `303` redirect to `/login`.
  - `GET  /me` → `{ "username": ..., "role": ... }` for the signed-in user (drives
    the front-end's role-aware UI).
- **Admin-only guard:** a tiny helper `require_admin(request)` raising `403`,
  applied in `/upload`, `/orders/delete`, `/orders/clear`.
- **`/actuals`:** available to both roles, including `mark_complete` — a user may
  log production and mark an SO complete (a shop-floor signal). No role stripping.
- **`/run`, `/rerun`, `/gantt` — persisted plan config:**
  - New store key `anvitech:plan_config` (kv: `json.dumps(config.to_dict())`).
  - `load_plan_config()` returns a validated `Config`, **falling back to `Config()`
    on a missing, unparseable, or invalid stored value** (try/except + `validate()`)
    — a corrupt stored config must never 500 a read endpoint.
  - `RunRequest` gains an explicit `persist: bool = False` flag.
  - On `POST /run`:
    - effective config = (caller is **admin** and a config was sent) ?
      `Config.from_dict(sent)` : `load_plan_config()`.
    - **persist only when caller is admin AND `persist is True`** (the admin's
      explicit Plan-button click). The **auto-load on page open sends
      `persist=False`**, so merely opening the app never overwrites the saved plan.
      Users never persist, regardless of the flag.
  - The `/run` response includes the **effective config** so the admin's config bar
    can reflect the last-saved plan.
  - `GET /gantt` uses `load_plan_config()` instead of a bare `Config()`.
  - Net effect: a single shared "current plan" everyone sees — the worker
    downloads exactly what the planner planned, and it isn't clobbered by page loads.

### New: `web/login.html`
Self-contained page (inline CSS in the app's visual style, **no JavaScript**): a
native `<form method="post" action="/login">` with username + password fields, a
Sign-in button, and (on failure) a generic error line rendered by the server. The
browser does the post; the server sets the cookie and `303`-redirects to `/`. No
inline script, so CSP `script-src 'self'` stays strict.

### Changed: `web/app.js` + `web/index.html`
- On load: `GET /me`. Store `currentRole`. **If `/me` is non-200 or the role is
  missing/garbled, default to `"user"`** (least privilege) — never default to admin.
  (Server enforcement is the real gate; this just avoids accidentally showing admin
  controls.)
- Add a top **session bar**: "Signed in as `<username>` · `<role>`" + a **Logout**
  button (posts to `/logout`).
- Role gating is done by **hiding, not removing** elements — add a
  `class="role-user"` to `<body>` for the user role and hide admin-only controls via
  CSS. The existing handler wiring (`$("run-btn").onclick`, `$("upload-btn")`, the
  Orders delete buttons) must be **null-guarded** so nothing throws if an element is
  hidden/absent. For the user role:
  - hide the `.controls` config block **and** the Plan button,
  - hide the `.datasource` (upload) section,
  - hide the Orders per-row select checkboxes and the "Delete selected" / "Delete
    ALL data" buttons (gate both the HTML emission **and** the wiring),
  - keep the Rule 6 download buttons and the **full** Capture Actuals form,
    **including** the "Mark this order complete" checkbox.
- **Auto-load the current plan on login** for both roles (call `/run` once on
  startup **with `persist:false`**) so the schedule / Gantt / rule tabs populate
  without a Plan click and **without overwriting the saved plan**. The admin Plan
  button sends **`persist:true`**. On load, the admin's config bar is populated from
  the effective config returned by `/run`.

## Data flow

```
Browser → GET /  (no cookie)
        → middleware: browser nav, not authed → 302 /login
        → GET /login → login.html
        → POST /login {username,password}
            → auth.authenticate() → role
            → Set-Cookie anvitech_session=<signed token>; 200
        → redirect to /
        → GET /me → {username, role}
        → app.js hides admin-only controls for role=user
        → GET-equivalent: POST /run (admin config persisted; user uses persisted)
            → trace + gantt + orders rendered
```

Admin-only call by a user (e.g. `POST /orders/delete`) → `require_admin` → `403`.

## Error handling

- Wrong username/password → `POST /login` returns `401` + message; page shows
  "Incorrect username or password." (No detail on which field — standard practice.)
- Tampered/expired cookie → treated as not-signed-in (redirect or `401`).
- A user hitting an admin endpoint directly → `403` (defensive; UI already hides it).
- No `SESSION_SECRET` and an empty store → a secret is generated and persisted on
  first need; never fatal.

## Security hardening

This change is treated as a security feature. The goal is that there is **no easy
loophole** — no way to reach an admin action without being admin, no way to forge
a session, and no obvious web-app vulnerability class left open. Each item below is
a concrete, testable control, using only the Python standard library (no new
dependency = smaller supply-chain surface).

### 1. Session integrity (no forgery)
- The session cookie is **signed with HMAC-SHA256** over the payload `{u, role,
  iat}` using the server secret. The server **never trusts an unsigned value** —
  role is read only from a signature-verified payload. A user cannot flip their own
  cookie to `role=admin` because they cannot produce a valid HMAC without the
  secret.
- Verification uses `hmac.compare_digest` (**constant-time**) to defeat timing
  attacks; password checks use `secrets.compare_digest`.
- The secret is **≥32 bytes of CSPRNG output** (`secrets.token_hex(32)`), persisted
  once and reused. Rotating it (change the env var or the stored value)
  **invalidates every existing session** automatically (HMAC no longer matches).
- The secret is **cached in-process** after first resolution (no per-request store
  round-trip — preserves the latency win from the perf work).

### 2. Cookie flags
- `HttpOnly` — JavaScript cannot read the cookie, so an XSS bug cannot steal the
  session.
- `SameSite=Lax` — the browser will not send the cookie on cross-site **POST**
  requests (the state-changing ones), which blocks the main CSRF vector while
  avoiding the "first click from a bookmark looks logged-out" surprise of `Strict`.
  The Origin/Referer check (§3) is the second, independent CSRF layer.
- `Secure` — set whenever the request is HTTPS (detected via scheme /
  `X-Forwarded-Proto`, which Render sets), so the cookie never travels over plain
  HTTP in production. Left off only for `http://localhost` dev so login still works.
- `Path=/`, and `Max-Age = 7 days` — **the same 7 days as the authoritative
  server-side `iat` check**, so the cookie and the server agree on expiry (no
  longer-lived server acceptance of a captured cookie).

### 3. CSRF defense-in-depth
Beyond `SameSite=Lax`, every **state-changing** request (`POST`/`PUT`/`PATCH`/
`DELETE`) is checked against the app's own host using `Origin` (falling back to
`Referer`): **reject (`403`) only when the header is present AND its host does not
match**; when **both are absent, allow**. This is the correct posture because CSRF
is a browser-only attack — browsers always attach `Origin` on cross-site POSTs, so
a foreign `Origin` is caught, while header-less non-browser clients (`curl`, the
test client, server-to-server) carry no ambient session cookie to abuse and so are
not a CSRF vector. The app host is taken from the request's own `Host`/forwarded
host (which a real CSRF attacker cannot align with their foreign `Origin`). This
layers a second, independent CSRF control on top of the cookie flag, and keeps the
test client and same-origin XHR working.

### 4. Brute-force / credential-stuffing resistance
- An **in-memory rate limiter** on `POST /login`, **keyed by the attempted
  username** (not client IP): after **5 failed attempts** within a rolling
  **15-minute** window, further attempts for that username are refused with `429
  Too Many Requests` until the window passes.
  - *Why username, not IP:* `X-Forwarded-For` is fully client-controlled, so an
    IP-keyed limiter is both **bypassable** (rotate the header) and a **memory-DoS**
    (unbounded keys). Keying on the username is spoof-proof and naturally bounded to
    the two real accounts, so the limiter dict can never grow. An unknown username
    is bucketed under a single `"<unknown>"` key (also bounded).
  - *Accepted tradeoff:* an attacker hammering a real username can temporarily
    `429` the legitimate shared user for that 15-minute window. Acceptable: it's a
    throttle, not a permanent lock, and it stops brute force. Documented.
- A **small async delay** on failed login (~0.4 s, via `asyncio.sleep` so it never
  blocks the worker) to slow automated guessing; applied **uniformly** whether the
  username is unknown or the password is wrong (no timing oracle).
- Failed-login events are logged (sanitized username, never the password) for
  visibility. Single-instance app → in-memory counters suffice (documented). A
  test-only `reset_auth_state()` clears the limiter between tests.

### 5. Security response headers (applied to every response)
The current `no_cache` middleware is widened into a `security_headers` middleware
that adds:
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'
  'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none';
  frame-ancestors 'none'; form-action 'self'` — blocks injected/inline **script**
  execution (the main XSS lever) and framing. (`style-src` keeps `'unsafe-inline'`
  because the Gantt positions bars with inline `style=` attributes — low risk; the
  script lock is the important one.)
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY` (clickjacking; pairs with `frame-ancestors`)
- `Referrer-Policy: no-referrer`
- `Strict-Transport-Security: max-age=63072000; includeSubDomains` — **only on
  HTTPS** — forces HTTPS for future visits.
- The existing `Cache-Control: no-store` is kept (so authed pages aren't cached).

To keep `script-src 'self'` strict (no `'unsafe-inline'`), the **login page uses a
native HTML form and has no JavaScript at all** — no inline `<script>`, no extra
allowlisted asset. The main app already uses an external `app.js` and wires
handlers in JS (no inline `on*=` handlers), so it is CSP-compatible as-is. CSP also
sets `form-action 'self'` so the login form can only post back to this origin.

### 6. Cross-Site Scripting (stored/reflected)
- All user-supplied values rendered into the DOM continue to go through
  `escapeHtml`; the implementation audits the render paths for any value (SO No,
  item name, remarks, process) inserted without escaping and fixes gaps.
- The strict `script-src 'self'` CSP is the safety net: even a missed escape cannot
  execute injected `<script>` or inline `onerror=` handlers.

### 7. Authorization (the core) — no privilege escalation
- Every admin-only action is enforced **on the server** (`require_admin` → `403`),
  independent of the UI. A user calling `POST /upload`, `/orders/delete`, or
  `/orders/clear` directly (e.g. via `curl` with their own valid session) is
  refused. Hiding the buttons is cosmetic only.
- The role is taken **only** from the verified session, never from a request body,
  query param, or header the client controls.
- An explicit test asserts each admin endpoint returns `403` for a user session.

### 8. Upload safety (admin-only, but still defended)
- `POST /upload` enforces a **max body size** (e.g. **10 MB**); larger uploads are
  rejected (`413`) before parsing — a cheap guard against memory-exhaustion DoS and
  decompression ("zip-bomb") attempts.
- The workbook is opened **read-only** (already the case) and parse errors return a
  **generic** message (no stack trace / internal paths leaked to the client).
- Because upload is now **admin-only**, the realistic attacker surface here is small.
- `POST /login` also caps its request body small (a few KB) so the unauthenticated
  login endpoint can't be used to buffer large bodies.

### 9. Information-disclosure hygiene
- Login failures return a single generic message ("Incorrect username or
  password") — no hint about which field was wrong or whether a username exists.
- FastAPI's interactive docs are **disabled in the deployed app**
  (`docs_url=None`, `redoc_url=None`, `openapi_url=None`) to shrink the surface
  (they were behind auth anyway — this is defense-in-depth).
- Error responses avoid echoing internal exception detail to the client.

### 10. Transport
- Render terminates TLS (HTTPS) already; HSTS (above) pins it. The cookie's
  `Secure` flag ensures the session is never sent in cleartext in production.

### What is explicitly NOT claimed
- Passwords are **baked into the code** at the user's request; their presence in
  the private git history is an accepted residual risk (mitigated: not reused
  elsewhere; rotatable via env override). This is the one deliberate deviation from
  "store secrets outside code".
- This is hardening to a strong, standard baseline for a small single-tenant app —
  not a formal pentest or compliance certification.
- **Logout is best-effort:** because sessions are stateless signed cookies (no
  server-side session store, to avoid a per-request store round-trip), a previously
  captured cookie remains valid until its 7-day `iat` expiry even after logout.
  Acceptable for this app; rotating the secret invalidates everything immediately.
- `/trace/{run_id}` exposes the full plan (schedule, utilization, downtime) to any
  logged-in user — consistent with "user sees everything read-only." Run IDs are
  not treated as secrets.

## Testing (test-first)

New `tests/test_auth.py`:
- `authenticate` accepts each correct pair, rejects wrong password / unknown user.
- `make_token`/`verify_token` round-trip; a tampered token fails; an expired token
  fails.
- `POST /login` (admin) sets a cookie; `/me` then reports `role=admin`.
- `POST /login` (user) → `/me` reports `role=user`.
- Wrong password → `401`.
- No cookie: a JSON endpoint → `401`; `GET /` (Accept: text/html) → `302 /login`.
- **User is refused** server-side: `/upload` → `403`, `/orders/delete` → `403`,
  `/orders/clear` → `403`.
- **User is allowed**: `/actuals` → `200`; a user's `mark_complete=true` **does
  archive** the order (allowed for both roles).
- **User Plan config**: a user `POST /run` ignores submitted config and uses the
  admin-persisted one.
- `POST /logout` clears the cookie (subsequent protected call → `401`/redirect).

Security-focused tests (`tests/test_auth.py`):
- **No privilege escalation:** a hand-crafted cookie claiming `role=admin` but
  signed with the wrong key is rejected (treated as not-signed-in); a user session
  cannot reach any admin endpoint (`403`).
- **Tamper/replay:** flipping any byte of a valid token fails verification; a token
  with an `iat` older than `MAX_AGE` is rejected.
- **CSRF:** a `POST` with a foreign `Origin` header is rejected (`403`) even with a
  valid session cookie; a same-origin `POST` passes.
- **Rate limit:** 6 rapid wrong-password logins from one client → the later ones
  return `429`.
- **Security headers:** a normal response carries `Content-Security-Policy`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`.
- **Upload size cap:** an over-limit `/upload` body is rejected (`413`) before
  parsing.
- **Docs disabled:** `GET /openapi.json` / `/docs` are not served (`404`).

Update `tests/test_api.py`:
- Replace the Basic-Auth header with a login helper (POST `/login` as admin once;
  `TestClient` keeps the cookie). All existing assertions must still pass.
- Keep a `test_requires_login`-style check (no cookie → blocked).

Golden trace: **unchanged** — no rule logic is touched.

## Docs to update (same PR)

- `CLAUDE.md` — Commands/Login section: two roles, login page, where credentials
  live, the env back-door.
- `HANDOFF.md` — replace the Basic-Auth notes; document the two logins, the
  role split, the persisted plan config, and the local login (`anvitech` /
  `1930rail`).
- `render.yaml` — note the optional override env vars (kept `sync:false`); the app
  works with none set.
- `README` — update the login line.

## Out of scope (unchanged)

- More than two roles, per-user accounts, password reset/self-service, account
  management UI. (Two shared logins only.)
- Any change to the scheduling rules or the order-book logic.

## Risks / notes

- **Passwords in git history** (user's accepted tradeoff): use passwords not reused
  elsewhere; they can be rotated via the env back-door without a code edit.
- **Cookie `Secure` flag**: must be set only on HTTPS or local http login breaks;
  detect via request scheme / `X-Forwarded-Proto`.
- **Existing tests import `APP_USERNAME`/`APP_PASSWORD`** — update those imports
  when the constants move to `auth.py` (keep a compatible admin alias).
- **First deploy after this lands**: anyone currently logged in via the old popup
  will be sent to the new login page (expected). Tell the user to hard-refresh.
