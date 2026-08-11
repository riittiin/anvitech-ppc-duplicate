"""Cloud Optimize must never dispatch into the repo this deployment mirrors.

Clause 2 is usually discussed as "no database writes to the live site", but the
cloud tier opens a second road to production: a workflow_dispatch into the
original's repo runs on its Actions minutes, and that repo's secrets point
APP_URL at the live app — so its workers would post contest results into
production. This guards that road.
"""
import pytest

pytest.importorskip("fastapi")

from api import main  # noqa: E402


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch):
    """Both credentials present, so only the repo choice is under test."""
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "fake-secret")
    monkeypatch.delenv("GITHUB_REPO", raising=False)


def test_defaults_to_this_deployments_own_repo():
    assert main._cloud_config()["repo"] == main.OWN_GITHUB_REPO


def test_the_default_is_not_the_upstream_repo():
    assert main.OWN_GITHUB_REPO != main.UPSTREAM_GITHUB_REPO


def test_an_explicit_repo_is_honoured(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "someone/else")
    assert main._cloud_config()["repo"] == "someone/else"


def test_dispatching_into_the_mirrored_repo_is_refused(monkeypatch):
    """The whole point: even asked explicitly, it will not fire."""
    monkeypatch.setenv("GITHUB_REPO", main.UPSTREAM_GITHUB_REPO)
    assert main._cloud_config() is None


def test_refusal_degrades_to_local_compute_not_a_crash(monkeypatch):
    """None means 'no cloud tier', which the caller already handles by
    computing locally — so Optimize still works, just slower."""
    monkeypatch.setenv("GITHUB_REPO", main.UPSTREAM_GITHUB_REPO)
    assert main._cloud_config() is None  # not an exception


def test_blank_repo_falls_back_to_own_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "   ")
    assert main._cloud_config()["repo"] == main.OWN_GITHUB_REPO


def test_whitespace_around_the_upstream_repo_is_still_refused(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", f"  {main.UPSTREAM_GITHUB_REPO}  ")
    assert main._cloud_config() is None


def test_cloud_stays_off_without_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    assert main._cloud_config() is None


def test_keep_warm_workflow_does_not_ping_the_live_site():
    """The inherited workflow hardcoded the live URL; a mirror pinging
    production is pointless and misleading in its logs."""
    from pathlib import Path
    wf = (Path(main.__file__).resolve().parent.parent
          / ".github" / "workflows" / "keep-warm.yml").read_text()
    assert "anvitech-ppc-duplicate.onrender.com" in wf
    assert "https://anvitech-ppc.onrender.com" not in wf
