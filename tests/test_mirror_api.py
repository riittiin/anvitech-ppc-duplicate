"""The /mirror endpoints: status badge and 'Re-attach to live'.

Both roles can read the status (the badge must warn the user role too). Only an
admin, re-entering their password, can re-attach — it discards local data.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import auth, main  # noqa: E402
from api.main import app  # noqa: E402
from engine import book_store, storage  # noqa: E402
from engine.overlay_store import OverlayStore, ReadOnlyStore  # noqa: E402
from engine.storage import LocalStore  # noqa: E402

_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]
_USER = next(u for u, a in _ACCTS.items() if a["role"] == auth.USER)
_USER_PWD = _ACCTS[_USER]["password"]


@pytest.fixture(autouse=True)
def _fast_login(monkeypatch):
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0)


@pytest.fixture
def mirrored(tmp_path, monkeypatch):
    """Make this deployment behave as a mirror. Returns (upstream, overlay)."""
    up = LocalStore(tmp_path / "upstream")
    overlay = OverlayStore(ReadOnlyStore(up), LocalStore(tmp_path / "local"))
    monkeypatch.setattr(storage, "get_store", lambda: overlay)
    monkeypatch.setattr(book_store, "get_store", lambda: overlay)
    return up, overlay


def _client_as(username, password):
    c = TestClient(app)
    assert c.post("/login", data={"username": username, "password": password}).status_code == 200
    return c


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def test_status_reports_disabled_on_a_normal_deployment():
    r = _client_as(_ADMIN, _ADMIN_PWD).get("/mirror/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "detached": []}


def test_status_reports_enabled_when_mirroring(mirrored):
    r = _client_as(_ADMIN, _ADMIN_PWD).get("/mirror/status")
    assert r.json()["enabled"] is True


def test_the_user_role_can_see_the_badge_too(mirrored):
    """A read-only user must also be able to tell this isn't the real site."""
    r = _client_as(_USER, _USER_PWD).get("/mirror/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_status_requires_a_session():
    assert TestClient(app).get("/mirror/status").status_code in (401, 403, 302, 307)


def test_detached_keys_are_listed_with_friendly_labels(mirrored):
    _, overlay = mirrored
    overlay.delete_key(book_store.ACTUALS_KEY)

    detached = _client_as(_ADMIN, _ADMIN_PWD).get("/mirror/status").json()["detached"]

    assert detached == [{"key": book_store.ACTUALS_KEY, "label": "Daily entries"}]


def test_internal_keys_are_never_offered(mirrored):
    _, overlay = mirrored
    overlay.delete_key("anvitech:last_searched")
    assert _client_as(_ADMIN, _ADMIN_PWD).get("/mirror/status").json()["detached"] == []


# --------------------------------------------------------------------------- #
# Re-attach
# --------------------------------------------------------------------------- #

def test_reattach_restores_the_live_view(mirrored):
    up, overlay = mirrored
    up.list_append(book_store.ACTUALS_KEY, '{"id": "prod-1"}')
    overlay.delete_key(book_store.ACTUALS_KEY)
    assert overlay.list_all(book_store.ACTUALS_KEY) == []

    r = _client_as(_ADMIN, _ADMIN_PWD).post(
        "/mirror/reattach",
        json={"key": book_store.ACTUALS_KEY, "password": _ADMIN_PWD})

    assert r.status_code == 200
    assert overlay.list_all(book_store.ACTUALS_KEY) == ['{"id": "prod-1"}']


def test_reattach_needs_the_admin_password(mirrored):
    r = _client_as(_ADMIN, _ADMIN_PWD).post(
        "/mirror/reattach", json={"key": book_store.ACTUALS_KEY, "password": "wrong"})
    assert r.status_code == 403


def test_reattach_is_refused_for_the_user_role(mirrored):
    r = _client_as(_USER, _USER_PWD).post(
        "/mirror/reattach",
        json={"key": book_store.ACTUALS_KEY, "password": _USER_PWD})
    assert r.status_code == 403


def test_reattach_rejects_an_unknown_key(mirrored):
    r = _client_as(_ADMIN, _ADMIN_PWD).post(
        "/mirror/reattach", json={"key": "anvitech:session_secret",
                                  "password": _ADMIN_PWD})
    assert r.status_code == 400


def test_reattach_404s_on_a_normal_deployment():
    r = _client_as(_ADMIN, _ADMIN_PWD).post(
        "/mirror/reattach",
        json={"key": book_store.ACTUALS_KEY, "password": _ADMIN_PWD})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# The overlay is found through the per-request cache wrapper
# --------------------------------------------------------------------------- #

def test_mirror_store_sees_through_the_request_cache(mirrored):
    """get_store() wraps the backend in a per-request read cache during a
    request; _mirror_store must still find the overlay underneath it."""
    _, overlay = mirrored
    token = storage.begin_request_cache()
    try:
        assert main._mirror_store() is overlay
    finally:
        storage.end_request_cache(token)
