# tests/test_oracle_poller.py
import sys, types
sys.path.insert(0, "scripts")
import oracle_optimize_worker as w


def test_check_pending_returns_id_or_none():
    assert w.check_pending(lambda m, p: {"job_id": "abc"}) == "abc"
    assert w.check_pending(lambda m, p: {"job_id": None}) is None
    def boom(m, p): raise RuntimeError("app asleep")
    assert w.check_pending(boom) is None                   # never raises


def test_refresh_code_runs_git_and_conditional_pip(tmp_path):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0; stdout = b"deadbeef\n"
        # simulate: HEAD unchanged -> no requirements diff path taken
        return R()
    w.refresh_code(str(tmp_path), fake_run)
    joined = [" ".join(map(str, c)) for c in calls]
    assert any("fetch" in c for c in joined)
    assert any("reset" in c and "origin/main" in c for c in joined)
    assert not any("pip" in c for c in joined)             # unchanged HEAD -> no pip


def test_run_job_passes_job_id_env(tmp_path):
    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd; seen["env"] = kw.get("env") or {}
        class R: returncode = 0
        return R()
    rc = w.run_job("job-42", str(tmp_path), {"APP_URL": "http://x"}, fake_run)
    assert rc == 0
    assert seen["env"]["JOB_ID"] == "job-42"
    assert any("cloud_optimize_worker.py" in str(c) for c in seen["cmd"])
