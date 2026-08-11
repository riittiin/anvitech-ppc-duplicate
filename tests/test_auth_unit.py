"""Unit tests for the auth core (pure logic): credentials, signed tokens, and
the login rate limiter. API-level auth (login page, role enforcement, security
headers, CSRF) is covered in test_auth_api.py."""
import time

import pytest

from api import auth


@pytest.fixture(autouse=True)
def _clean():
    auth.reset_auth_state()
    yield
    auth.reset_auth_state()


# --- credentials --- #
def test_authenticate_admin_and_user():
    accts = auth._accounts()
    admin = next(u for u, a in accts.items() if a["role"] == auth.ADMIN)
    user = next(u for u, a in accts.items() if a["role"] == auth.USER)
    assert auth.authenticate(admin, accts[admin]["password"]) == auth.ADMIN
    assert auth.authenticate(user, accts[user]["password"]) == auth.USER


def test_authenticate_rejects_wrong_password_and_unknown_user():
    accts = auth._accounts()
    admin = next(u for u, a in accts.items() if a["role"] == auth.ADMIN)
    assert auth.authenticate(admin, "definitely-wrong") is None
    assert auth.authenticate("nobody", "whatever") is None
    assert auth.authenticate("", "") is None


def test_env_override_changes_admin_credentials(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "boss")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    assert auth.authenticate("boss", "s3cret") == auth.ADMIN


# --- signed token --- #
def test_token_round_trip():
    tok = auth.make_token("anvitech", auth.ADMIN)
    payload = auth.verify_token(tok)
    assert payload == {"u": "anvitech", "role": auth.ADMIN}


def test_token_tamper_fails():
    # A user tries to upgrade their real token to admin without re-signing:
    # change the payload but keep the original (user) signature → must fail.
    import json
    user_tok = auth.make_token("anvitech_user", auth.USER)
    _, _, user_sig = user_tok.partition(".")
    forged_body = auth._b64e(json.dumps(
        {"u": "anvitech_user", "role": "admin", "iat": int(time.time())}).encode())
    assert auth.verify_token(forged_body + "." + user_sig) is None

    # Mutating any byte of a valid token also fails.
    tok = auth.make_token("anvitech", auth.ADMIN)
    body, _, sig = tok.partition(".")
    flipped = ("A" if body[0] != "A" else "B") + body[1:]
    assert auth.verify_token(flipped + "." + sig) is None

    # Garbage / wrong shape.
    assert auth.verify_token("not-a-token") is None
    assert auth.verify_token(body + ".") is None
    assert auth.verify_token("") is None


def test_token_forged_with_wrong_secret_is_rejected():
    import hashlib, hmac, json
    # An attacker who doesn't know the secret signs their own admin payload.
    body = auth._b64e(json.dumps({"u": "x", "role": "admin",
                                  "iat": int(time.time())}).encode())
    bad_sig = hmac.new(b"attacker-guess", body.encode(), hashlib.sha256).digest()
    assert auth.verify_token(body + "." + auth._b64e(bad_sig)) is None


def test_token_expiry_and_future():
    old = auth.make_token("anvitech", auth.ADMIN,
                          iat=int(time.time()) - auth.MAX_AGE_SECONDS - 10)
    assert auth.verify_token(old) is None
    future = auth.make_token("anvitech", auth.ADMIN, iat=int(time.time()) + 9999)
    assert auth.verify_token(future) is None


def test_token_unknown_role_rejected():
    tok = auth.make_token("anvitech", "superuser")  # not admin/user
    assert auth.verify_token(tok) is None


# --- rate limiter --- #
def test_rate_limiter_trips_after_five_failures():
    accts = auth._accounts()
    admin = next(u for u, a in accts.items() if a["role"] == auth.ADMIN)
    for _ in range(5):
        assert auth.is_rate_limited(admin) is False
        auth.record_failed_login(admin)
    assert auth.is_rate_limited(admin) is True


def test_rate_limiter_buckets_unknown_usernames_together():
    # Many distinct unknown usernames must not grow the table (DoS guard).
    for i in range(50):
        auth.record_failed_login(f"bogus-{i}")
    assert auth._rate_key("bogus-123") == "<unknown>"
    assert set(auth._rate.keys()) == {"<unknown>"}
    assert auth.is_rate_limited("any-unknown") is True
