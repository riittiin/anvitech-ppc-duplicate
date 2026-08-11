"""Shared fixtures: build the generated sample workbook once per session (Test4
format — there is no bundled real workbook), and isolate the durable store to a
fresh temp dir per test (so the order book / actuals don't leak between tests or
touch the real ./data/store)."""
import io
import os

import pytest

# The repo is public → api/auth ships NO baked default password. Test modules
# that read auth._accounts() at import time (e.g. test_auth_api, test_rollback)
# need a known password present before collection, so set the fixture creds at
# conftest load. setdefault lets a real CI env override win; the per-test
# _isolate_store fixture also sets them so they survive any monkeypatch teardown.
os.environ.setdefault("ADMIN_PASSWORD", "1930rail")
os.environ.setdefault("USER_PASSWORD", "anvitech12345678")

from engine.loaders import load_all
from engine.config import Config
from tests.sample_workbook import build_sample_bytes


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    # Login credentials for the test client. Production ships NO baked default
    # password (the repo is public — see api/auth._accounts), so the API-level
    # suites supply the fixture creds they log in with via env vars here.
    monkeypatch.setenv("ADMIN_PASSWORD", "1930rail")
    monkeypatch.setenv("USER_PASSWORD", "anvitech12345678")
    # Clear in-process auth state (rate limiter + cached session secret) so each
    # test starts clean and resolves its secret from its own isolated store.
    try:
        from api import auth
        auth.reset_auth_state()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session")
def sample_bytes():
    """The generated sample workbook as .xlsx bytes (for upload tests)."""
    return build_sample_bytes()


@pytest.fixture(scope="session")
def loaded(sample_bytes):
    so_lines, masters = load_all(io.BytesIO(sample_bytes))
    return so_lines, masters


@pytest.fixture
def config():
    # The LIVE default plan_start_date is now None ("auto: start from today");
    # the pure rules must never see None (the API resolves it). Rule tests that
    # drive the engine directly pin the historical fixed date so their assertions
    # (and the golden window) stay byte-identical to the pre-live-mode behaviour.
    #
    # scheduler="classic": the new operator-stable engine is now the app default, but the
    # pure-engine rule/golden/optimizer tests were written against the classic engine (and
    # its byte-identical golden trace). They keep validating that KEPT engine explicitly;
    # the new engine has its own suite on a fully-staffed fixture (tests/test_new_engine.py).
    from datetime import date
    return Config(plan_start_date=date(2025, 3, 1), scheduler="classic")


@pytest.fixture(autouse=True)
def _no_auto_optimize(monkeypatch):
    """The self-tuning trigger must never fire spontaneously inside tests —
    endpoints bump the book-changed state, and without this the api module
    would spawn background contest threads mid-suite. Dedicated auto tests
    re-enable with monkeypatch.setenv('AUTO_OPTIMIZE', '1')."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
