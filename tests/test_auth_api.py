"""API-level auth + security tests: login flow, role enforcement, CSRF, rate
limiting, security headers, docs-off, and the user permission model."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api import auth  # noqa: E402
from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()

_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]
_USER = next(u for u, a in _ACCTS.items() if a["role"] == auth.USER)
_USER_PWD = _ACCTS[_USER]["password"]


@pytest.fixture(autouse=True)
def _fast_login(monkeypatch):
    # Don't actually sleep on failed logins during tests.
    monkeypatch.setattr(auth, "FAILED_LOGIN_DELAY", 0)


def _client_as(username, password):
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 200
    return c


def _admin():
    return _client_as(_ADMIN, _ADMIN_PWD)


def _user():
    return _client_as(_USER, _USER_PWD)


def _upload(client):
    return client.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})


# --- login flow + identity --- #
def test_login_sets_cookie_and_me_reports_role():
    admin = _admin()
    assert auth.COOKIE_NAME in admin.cookies
    me = admin.get("/me").json()
    assert me["role"] == auth.ADMIN and me["username"] == _ADMIN

    user = _user()
    assert user.get("/me").json()["role"] == auth.USER


def test_wrong_password_is_401_no_cookie():
    c = TestClient(app)
    r = c.post("/login", data={"username": _ADMIN, "password": "nope"})
    assert r.status_code == 401
    assert auth.COOKIE_NAME not in c.cookies


def test_logout_clears_session():
    admin = _admin()
    assert admin.get("/me").status_code == 200
    admin.post("/logout")
    # Cookie cleared → subsequent protected JSON call is rejected.
    assert admin.get("/me").status_code == 401


# --- gate behaviour for anonymous --- #
def test_anonymous_json_is_401_and_browser_nav_redirects():
    anon = TestClient(app)
    assert anon.post("/run", json={}).status_code == 401
    assert anon.get("/orders").status_code == 401
    # A browser navigation (Accept: text/html) is redirected to the login page.
    r = anon.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    # The login page itself is reachable without a session.
    assert anon.get("/login").status_code == 200


def test_static_shell_requires_session():
    anon = TestClient(app)
    # No static asset leaks past the gate (exact-match allowlist only).
    for path in ("/", "/index.html", "/app.js", "/style.css"):
        assert anon.get(path).status_code in (302, 401)


# --- role enforcement (server-side) --- #
def test_user_is_refused_on_admin_endpoints():
    user = _user()
    assert _upload(user).status_code == 403
    assert user.post("/orders/delete", json={"orders": [["X", "A"]]}).status_code == 403
    assert user.post("/orders/clear", json={}).status_code == 403


def test_admin_allowed_on_admin_endpoints():
    admin = _admin()
    assert _upload(admin).status_code == 200
    # Deletes also require the admin to re-enter their password.
    assert admin.post("/orders/delete",
                      json={"orders": [["nope", "A"]], "password": _ADMIN_PWD}).status_code == 200
    assert admin.post("/orders/clear", json={"password": _ADMIN_PWD}).status_code == 200


def test_role_in_body_does_not_escalate():
    # A user cannot become admin by sending role/admin fields in the body.
    user = _user()
    r = user.post("/orders/clear", json={"role": "admin", "is_admin": True})
    assert r.status_code == 403


# --- user permissions: actuals incl. mark-complete; plan uses saved config --- #
def test_user_can_capture_actuals_and_mark_complete():
    admin = _admin()
    _upload(admin)
    user = _user()
    r = user.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "operator": "Operator One",
        "entry_date": "2025-03-10", "qty_produced": 5, "mark_complete": True,
    })
    assert r.status_code == 200 and r.json()["completed_order"] is True


def test_user_run_uses_persisted_admin_config_not_their_own():
    admin = _admin()
    _upload(admin)
    # Admin saves a plan with a distinctive setup_time (persist=True).
    admin.post("/run", json={"config": {"setup_time_min": 123}, "persist": True})
    # User sends a different config; server must ignore it and use the saved 123.
    out = _user().post("/run", json={"config": {"setup_time_min": 999}}).json()
    assert out["config"]["setup_time_min"] == 123


def test_admin_autoload_does_not_clobber_saved_plan():
    admin = _admin()
    _upload(admin)
    admin.post("/run", json={"config": {"setup_time_min": 77}, "persist": True})
    # An auto-load style call (persist defaults False) must NOT overwrite it,
    # even though a config is present in the body.
    admin.post("/run", json={"config": {"setup_time_min": 5}, "persist": False})
    assert _user().post("/run", json={}).json()["config"]["setup_time_min"] == 77


# --- CSRF --- #
def test_csrf_foreign_origin_rejected_same_origin_ok():
    admin = _admin()
    bad = admin.post("/run", json={}, headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403
    ok = admin.post("/run", json={}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


# --- rate limiting --- #
def test_login_rate_limited_after_repeated_failures():
    c = TestClient(app)
    codes = [c.post("/login", data={"username": _ADMIN, "password": "x"}).status_code
             for _ in range(6)]
    assert 429 in codes
    assert codes[-1] == 429


# --- security headers + docs disabled --- #
def test_security_headers_present():
    h = _admin().get("/me").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"


def test_interactive_docs_disabled():
    admin = _admin()
    assert admin.get("/openapi.json").status_code == 404
    assert admin.get("/docs").status_code == 404


# --- upload size cap --- #
def test_oversize_upload_rejected():
    admin = _admin()
    big = b"x" * (10 * 1024 * 1024 + 1)
    r = admin.post("/upload", files={"file": ("big.xlsx", big, XLSX_MIME)})
    assert r.status_code == 413
