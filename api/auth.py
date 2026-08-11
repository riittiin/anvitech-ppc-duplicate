"""Authentication, sessions, and login-hardening for the PPC app.

The single place that knows about accounts and sessions. Design goals (see
docs/superpowers/specs/2026-06-25-two-role-login-design.md):

* Two roles — ``admin`` (full control) and ``user`` (read-only + download +
  capture actuals). Usernames have readable defaults; PASSWORDS come only from
  env vars — the repo is public, so no password is baked into the source.
* Stateless **signed session cookie** — HMAC-SHA256 over a small payload, using
  only the Python standard library (no extra dependency = smaller supply chain).
* Constant-time comparisons everywhere; no timing oracle on unknown usernames.
* A username-keyed login rate limiter (spoof-proof and naturally bounded).

Nothing here ever returns the secret to a caller or logs a password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from engine.storage import get_store

# Role constants — the only two roles.
ADMIN = "admin"
USER = "user"

# Cookie + expiry. The server-side ``iat`` check is the authoritative expiry and
# matches the cookie Max-Age, so a captured cookie can't outlive it.
COOKIE_NAME = "anvitech_session"
MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days

# Small delay added to a FAILED login to slow automated guessing. Exposed so
# tests can set it to 0 (kept as a module attribute, read live in api.main).
FAILED_LOGIN_DELAY = 0.4

# Login rate limit: N failures per rolling window, keyed by attempted username.
_MAX_FAILS = 5
_WINDOW_SECONDS = 15 * 60

_SECRET_STORE_KEY = "anvitech:session_secret"

# In-process caches (module-level; reset between tests via reset_auth_state()).
_secret_cache: dict = {}     # store-env-key -> bytes
_rate: dict = {}             # rate-key -> list[failure timestamps]

# A fixed digest used to equalize timing when the username is unknown, so login
# response time never reveals whether a username exists.
_DUMMY_DIGEST = hashlib.sha256(b"anvitech-nonexistent-account").digest()


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
# The repo is PUBLIC — a hardcoded password here would be a live credential for
# anyone who reads this file. So there is NO baked default password. When a
# password env var is unset, the account falls back to a fresh random secret
# generated once at import: unknowable, and different on every boot, so an
# unconfigured deployment simply has no working login rather than a public
# default. Real deployments MUST set ADMIN_PASSWORD / USER_PASSWORD (render.yaml
# documents these). Usernames are not secret and keep readable defaults.
_ADMIN_NO_DEFAULT_PWD = secrets.token_hex(32)
_USER_NO_DEFAULT_PWD = secrets.token_hex(32)


def _accounts() -> dict:
    """username -> {password, role}. Usernames have readable defaults; passwords
    come only from env vars (no baked default — see the note above).

    Legacy ``APP_USERNAME`` / ``APP_PASSWORD`` still override the admin account so
    an existing deployment keeps working through the transition.
    """
    admin_user = (os.environ.get("APP_USERNAME")
                  or os.environ.get("ADMIN_USERNAME") or "anvitech")
    admin_pwd = (os.environ.get("APP_PASSWORD")
                 or os.environ.get("ADMIN_PASSWORD") or _ADMIN_NO_DEFAULT_PWD)
    user_user = os.environ.get("USER_USERNAME") or "anvitech_user"
    user_pwd = os.environ.get("USER_PASSWORD") or _USER_NO_DEFAULT_PWD
    return {
        admin_user: {"password": admin_pwd, "role": ADMIN},
        user_user: {"password": user_pwd, "role": USER},
    }


def authenticate(username: str, password: str) -> Optional[str]:
    """Return the role for valid credentials, else None. Constant-time, and
    equal-time for unknown usernames (no username-enumeration oracle)."""
    acct = _accounts().get(username or "")
    pwd_digest = hashlib.sha256((password or "").encode()).digest()
    expected_digest = (hashlib.sha256(acct["password"].encode()).digest()
                       if acct else _DUMMY_DIGEST)
    ok = hmac.compare_digest(pwd_digest, expected_digest)
    return acct["role"] if (ok and acct) else None


# --------------------------------------------------------------------------- #
# Session secret (resolved once, cached; never sent to a client)
# --------------------------------------------------------------------------- #
def _store_env_key():
    return (os.environ.get("MONGODB_URI"),
            os.environ.get("UPSTASH_REDIS_REST_URL"),
            os.environ.get("STORE_DIR"))


def get_secret() -> bytes:
    """The HMAC signing secret. ``SESSION_SECRET`` env wins; else a value
    persisted in the durable store; else a fresh CSPRNG secret, persisted once.
    Cached in-process (keyed by store config) so there's no per-request store
    round-trip. Rotating it invalidates every existing session."""
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    key = _store_env_key()
    cached = _secret_cache.get(key)
    if cached is not None:
        return cached
    store = get_store()
    val = store.kv_get(_SECRET_STORE_KEY)
    if not val:
        val = secrets.token_hex(32)
        store.kv_set(_SECRET_STORE_KEY, val)
    raw = val.encode()
    _secret_cache[key] = raw
    return raw


# --------------------------------------------------------------------------- #
# Signed token (stateless session)
# --------------------------------------------------------------------------- #
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(username: str, role: str, iat: Optional[int] = None) -> str:
    """A signed session token: ``<base64(payload)>.<base64(hmac)>``."""
    iat = int(iat if iat is not None else time.time())
    payload = {"u": username, "role": role, "iat": iat}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(get_secret(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_token(token: Optional[str]) -> Optional[dict]:
    """Return ``{u, role}`` for a valid, unexpired, untampered token, else None.

    Rejects bad signatures (constant-time), garbled payloads, unknown roles,
    future-dated tokens (beyond small skew), and tokens older than MAX_AGE."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    try:
        expected = hmac.new(get_secret(), body.encode(), hashlib.sha256).digest()
        got = _b64d(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    u, role, iat = payload.get("u"), payload.get("role"), payload.get("iat")
    if not u or role not in (ADMIN, USER) or not isinstance(iat, int):
        return None
    now = int(time.time())
    if iat > now + 60:               # future-dated → reject
        return None
    if now - iat > MAX_AGE_SECONDS:   # expired → reject
        return None
    return {"u": u, "role": role}


# --------------------------------------------------------------------------- #
# Login rate limiting (username-keyed → spoof-proof and bounded)
# --------------------------------------------------------------------------- #
def _rate_key(username: str) -> str:
    """Bucket by real username; all unknown usernames share one bucket, so the
    rate table can never grow beyond (#accounts + 1) keys."""
    return username if username in _accounts() else "<unknown>"


def _recent_fails(key: str) -> list:
    now = time.time()
    fails = [t for t in _rate.get(key, []) if now - t < _WINDOW_SECONDS]
    _rate[key] = fails
    return fails


def is_rate_limited(username: str) -> bool:
    return len(_recent_fails(_rate_key(username))) >= _MAX_FAILS


def record_failed_login(username: str) -> None:
    key = _rate_key(username)
    fails = _recent_fails(key)
    fails.append(time.time())
    _rate[key] = fails


def reset_auth_state() -> None:
    """Test hook: clear the in-process rate limiter and secret cache."""
    _rate.clear()
    _secret_cache.clear()
