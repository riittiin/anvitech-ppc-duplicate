# Oracle Always-on Optimize Worker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Optimize's cloud compute to a free always-on Oracle 4-core box via a poll-and-claim tier ahead of GitHub Actions, with a deep-budget env knob — the measured −4.4% lever.

**Architecture:** The box polls a new `GET /optimize/pending` endpoint, claims the waiting job, refreshes its code from `main`, and runs the EXISTING `scripts/cloud_optimize_worker.py` on all cores. App side: a `claimed` flag + a claim window in `cloud_job` before the GitHub dispatch (Oracle → GitHub → local ladder). One env knob deepens the per-candidate budget for every cloud contest.

**Tech Stack:** Python stdlib only (both scripts), FastAPI (one endpoint), bash + systemd (setup), pytest (+ one uvicorn-subprocess E2E).

## Global Constraints

- **Scheduling code untouched:** no changes to the optimizer/search/gates — only WHERE compute runs and the budget knob. Branch is off `main` (B+C stays parked; no pins anywhere in this plan).
- Names fixed by the spec: endpoint `GET /optimize/pending`; env `ORACLE_CLAIM_TIMEOUT_MIN` (default **3**, `0` = today's immediate-GitHub behavior); env `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (unset/invalid → current defaults 150 new / 400 classic); poller `scripts/oracle_optimize_worker.py` (POLL_S = 10); setup `scripts/oracle_worker_setup.sh`; service `anvitech-optimize-worker`; runbook `docs/ORACLE_WORKER.md`; worker env file `/etc/anvitech-worker.env`.
- Worker auth: reuse `OPTIMIZE_WORKER_SECRET` / `X-Worker-Secret` via `_worker_secret_ok` (constant-time). `/optimize/pending` joins the gatekeeper bypass list exactly like the other worker endpoints.
- Every failure keeps the button alive: claim window expiry → GitHub dispatch → existing 40-min watchdog → local. The `/optimize/result` supersede guard stays untouched.
- Scripts are **stdlib-only** (match `cloud_optimize_worker.py`); poller errors never kill its loop.
- Tests: isolated `STORE_DIR`, `monkeypatch.setenv` (never raw os.environ), tiny budgets; reuse the existing test-fixture patterns in `tests/test_optimize_cloud.py` / `tests/test_optimize_endpoints.py`.

---

### Task 1: Deep-budget env knob

**Files:**
- Modify: `engine/optimize_service.py` (`cloud_budget`, ~line 62)
- Test: `tests/test_optimize_cloud.py` (append)

**Interfaces:**
- Produces: `cloud_budget(config) -> int` honoring `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` (positive int → override for BOTH scheduler modes; unset/garbage/≤0 → current defaults 150 new / 400 classic).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_optimize_cloud.py (append)
from dataclasses import replace
from engine import optimize_service
from engine.config import Config


def test_cloud_budget_env_override(monkeypatch):
    new_cfg = Config(scheduler="new")
    classic_cfg = Config(scheduler="classic")
    monkeypatch.delenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", raising=False)
    assert optimize_service.cloud_budget(new_cfg) == 150       # current defaults hold
    assert optimize_service.cloud_budget(classic_cfg) == 400
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "300")
    assert optimize_service.cloud_budget(new_cfg) == 300       # override, both modes
    assert optimize_service.cloud_budget(classic_cfg) == 300
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "garbage")
    assert optimize_service.cloud_budget(new_cfg) == 150       # invalid -> default
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "0")
    assert optimize_service.cloud_budget(new_cfg) == 150       # non-positive -> default
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_cloud.py::test_cloud_budget_env_override -v`
Expected: FAIL (override ignored).

- [ ] **Step 3: Implement**

Replace `cloud_budget`:

```python
def cloud_budget(config) -> int:
    """Plans per candidate for the cloud contest, per scheduler mode.

    OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE (env, positive int) overrides both modes —
    the deep-compute knob (2026-08-01 Oracle-worker spec: 300 ≈ the measured −4.4%
    class on a 4-core box). Unset/invalid/≤0 → the mode defaults below.
    """
    import os
    raw = os.environ.get("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    return (CLOUD_NEW_BUDGET_PER_CANDIDATE
            if getattr(config, "scheduler", "classic") == "new"
            else CLOUD_BUDGET_PER_CANDIDATE)
```

(Use the module's existing import style — if `os` is already imported at module top, drop the local import.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_optimize_cloud.py -q` → all green.

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py tests/test_optimize_cloud.py
git commit -m "feat(optimize): OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE deep-budget knob"
```

---

### Task 2: `/optimize/pending` + the `claimed` flag

**Files:**
- Modify: `api/main.py` (gatekeeper bypass ~line 119; `_OPTIMIZE` init ~line 931; `_start_optimize` job reset ~line 1276; `GET /optimize/job/{job_id}` ~line 2295; new endpoint beside it)
- Test: `tests/test_optimize_endpoints.py` (append)

**Interfaces:**
- Produces: `GET /optimize/pending` (worker-secret) → `{"job_id": <id>|null}` — the id only while `state=="running"`, `cloud_payload` present, and `claimed` is False. Fetching `GET /optimize/job/{id}` sets `_OPTIMIZE["claimed"]=True` (under `_OPTIMIZE_LOCK`). `_OPTIMIZE` gains `"claimed": False`, reset at every job start. Task 3 reads `_OPTIMIZE["claimed"]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_optimize_endpoints.py (append; reuse the file's worker-secret helpers/fixtures)
def test_pending_requires_secret_and_reports_unclaimed_job(new_engine_client_with_book, monkeypatch):
    client = new_engine_client_with_book
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")   # cloud path, no GitHub call
    H = {"X-Worker-Secret": "s3cr3t"}
    assert client.get("/optimize/pending").status_code == 401          # no secret -> session gate
    r = client.get("/optimize/pending", headers=H)
    assert r.status_code == 200 and r.json()["job_id"] is None         # idle -> null
    client.post("/optimize", json={"budget": "quick"})                 # parks a cloud job
    jid = client.get("/optimize/pending", headers=H).json()["job_id"]
    assert jid                                                          # waiting + unclaimed
    client.get(f"/optimize/job/{jid}", headers=H)                       # fetch = claim
    assert client.get("/optimize/pending", headers=H).json()["job_id"] is None  # claimed -> null
```

> Fixture note: the cloud path needs `OPTIMIZE_WORKER_SECRET` + `GITHUB_DISPATCH_TOKEN` set BEFORE the optimize starts — check how `tests/test_optimize_cloud.py` arranges env + app reload for its cloud tests and mirror it (the fixture may need the env set at app-import time; follow the existing cloud-test pattern exactly).

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_optimize_endpoints.py::test_pending_requires_secret_and_reports_unclaimed_job -v`
Expected: FAIL (404 — endpoint missing).

- [ ] **Step 3: Implement**

Gatekeeper bypass (~line 119) — extend the existing condition:

```python
    if ((method == "GET" and path.startswith("/optimize/job/"))
            or (method == "GET" and path == "/optimize/pending")
            or (method == "POST" and path in ("/optimize/progress",
                                              "/optimize/result"))):
```

`_OPTIMIZE` init dict (~line 931): add `"claimed": False,`. In `_start_optimize`'s job-start `_OPTIMIZE.update(...)` (~line 1276): add `claimed=False`.

`GET /optimize/job/{job_id}` (~line 2295): inside the lock, where the payload is returned, add `_OPTIMIZE["claimed"] = True` before building the response.

New endpoint beside it:

```python
@app.get("/optimize/pending")
def optimize_pending_ep(request: Request):
    """Poll point for the always-on (Oracle) worker: the waiting cloud job's id, or
    null. A job is 'pending' only while running, payload stored, and UNCLAIMED —
    a claimed job (any worker fetched its payload) is no longer offered."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if (_OPTIMIZE["state"] == "running" and _OPTIMIZE.get("cloud_payload")
                and not _OPTIMIZE.get("claimed")):
            return {"job_id": _OPTIMIZE.get("job_id")}
    return {"job_id": None}
```

(`_require_worker` exists — the other worker endpoints use it; match their exact auth call.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_optimize_endpoints.py -q` → green; plus `pytest tests/test_optimize_cloud.py -q` (no regression).

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_optimize_endpoints.py
git commit -m "feat(api): GET /optimize/pending + claimed flag (Oracle poll-and-claim)"
```

---

### Task 3: Tiered dispatch — claim window before GitHub

**Files:**
- Modify: `api/main.py` (`cloud_job` inside `_start_optimize`, ~lines 1303-1363)
- Test: `tests/test_optimize_cloud.py` (append)

**Interfaces:**
- Consumes: `_OPTIMIZE["claimed"]` (Task 2).
- Produces: `cloud_job` waits up to `ORACLE_CLAIM_TIMEOUT_MIN` (env, float, default 3; ≤0 ⇒ skip the wait) polling `claimed` every 2 s; claimed → NO GitHub dispatch (proceed straight to the existing watchdog loop); window expires unclaimed → `_dispatch_workflow` + watchdog exactly as today. The overall `OPTIMIZE_CLOUD_TIMEOUT_MIN` watchdog measures from job start, unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_optimize_cloud.py (append; reuse this file's cloud-path fixture + dispatch stub)
def test_claimed_in_window_skips_github_dispatch(cloud_app, monkeypatch):
    client, api_main = cloud_app                       # the file's existing cloud fixture shape
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0.05")   # 3 s window for the test
    calls = []
    monkeypatch.setattr(api_main, "_dispatch_workflow", lambda c, j: calls.append(j) or True)
    client.post("/optimize", json={"budget": "quick"})
    # claim immediately (simulates the Oracle poller)
    with api_main._OPTIMIZE_LOCK:
        jid = api_main._OPTIMIZE["job_id"]
    H = {"X-Worker-Secret": "s3cr3t"}
    client.get(f"/optimize/job/{jid}", headers=H)
    import time; time.sleep(4)                          # let the window elapse
    assert calls == []                                  # GitHub never dispatched


def test_unclaimed_window_falls_through_to_github(cloud_app, monkeypatch):
    client, api_main = cloud_app
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0.02")   # ~1 s window
    calls = []
    monkeypatch.setattr(api_main, "_dispatch_workflow", lambda c, j: calls.append(j) or True)
    client.post("/optimize", json={"budget": "quick"})
    import time; time.sleep(3)
    assert len(calls) == 1                              # dispatched after the window


def test_zero_window_dispatches_immediately(cloud_app, monkeypatch):
    client, api_main = cloud_app
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0")
    calls = []
    monkeypatch.setattr(api_main, "_dispatch_workflow", lambda c, j: calls.append(j) or True)
    client.post("/optimize", json={"budget": "quick"})
    import time; time.sleep(1.5)
    assert len(calls) == 1                              # today's behavior preserved
```

> The `cloud_app` fixture: READ `tests/test_optimize_cloud.py` first — it already builds a client with the cloud env (secret + token) and a reloaded `api.main`; adapt its existing fixture/helpers rather than inventing a new pattern. The background `cloud_job` thread runs during these tests — keep budgets tiny and always `Stop`/reset in the fixture teardown (mirror the file's existing teardown).

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_optimize_cloud.py -k "claim or window" -v`
Expected: FAIL (dispatch happens immediately in all cases today).

- [ ] **Step 3: Implement**

In `cloud_job`, replace the immediate `if not _dispatch_workflow(cloud, job_id): ... return` block with a claim-window phase ahead of it:

```python
            # Oracle tier (2026-08-01): give the always-on poller a window to claim
            # the job before falling back to the GitHub dispatch. 0/negative = no
            # window (today's immediate-GitHub behavior).
            try:
                _claim_min = float(os.environ.get("ORACLE_CLAIM_TIMEOUT_MIN", "3"))
            except ValueError:
                _claim_min = 3.0
            claim_deadline = time.monotonic() + max(0.0, _claim_min) * 60
            claimed = False
            while time.monotonic() < claim_deadline:
                with _OPTIMIZE_LOCK:
                    if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
                        return                    # superseded / already finished
                    claimed = bool(_OPTIMIZE.get("claimed"))
                if claimed:
                    break
                time.sleep(2)
            if not claimed:
                if not _dispatch_workflow(cloud, job_id):
                    ...existing dispatch-failure fallback block, unchanged...
```

The existing watchdog loop that follows stays byte-identical (it already handles "the worker delivered", timeout → local, Stop). Keep the baseline computation ABOVE the claim window (it must be ready whoever computes).

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_optimize_cloud.py -q` → all green (including the pre-existing cloud tests — the manual-token path must still work: with `ORACLE_CLAIM_TIMEOUT_MIN` unset in those tests, the 3-min default window would stall them; CHECK each existing cloud test and set `ORACLE_CLAIM_TIMEOUT_MIN=0` in the shared fixture so pre-existing tests keep today's timing).

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_optimize_cloud.py
git commit -m "feat(api): Oracle claim window before GitHub dispatch (Oracle->GitHub->local)"
```

---

### Task 4: The poller — `scripts/oracle_optimize_worker.py`

**Files:**
- Create: `scripts/oracle_optimize_worker.py`
- Test: `tests/test_oracle_poller.py` (new)

**Interfaces:**
- Consumes: `GET /optimize/pending` (Task 2); the existing `scripts/cloud_optimize_worker.py` (subprocessed per job with `JOB_ID` in env).
- Produces: a stdlib-only module with pure, injectable functions so the loop is testable without a network:
  - `check_pending(call) -> str | None` — `call` is a function `(method, path) -> dict`; returns the job id or None (any exception → None).
  - `refresh_code(repo_dir, run) -> None` — `run` is a `subprocess.run`-like callable; executes `git fetch origin main`, `git reset --hard origin/main`, and `pip install -r requirements.txt` ONLY when the reset changed `requirements.txt` (compare `git rev-parse HEAD` before/after and `git diff --name-only` between them). All failures logged, never raised.
  - `run_job(job_id, repo_dir, env, run) -> int` — subprocess `python3 scripts/cloud_optimize_worker.py` with `JOB_ID=job_id` merged into env; returns the exit code.
  - `main_loop(...)` — wires the real `urllib` call + `subprocess.run`, `POLL_S = 10`, infinite loop, every exception logged + `sleep(POLL_S)` + continue.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_oracle_poller.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement `scripts/oracle_optimize_worker.py`**

```python
"""Always-on Optimize poller (the Oracle box's service; 2026-08-01 spec).

Polls the app for a waiting Optimize job, refreshes the repo to origin/main, and
runs the EXISTING scripts/cloud_optimize_worker.py for that job on every core.
Stdlib only. Errors never kill the loop — log, sleep, poll again.

Env (from /etc/anvitech-worker.env via systemd):
  APP_URL, OPTIMIZE_WORKER_SECRET, REPO_DIR (the box's clone of the repo).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

POLL_S = 10


def check_pending(call):
    """The waiting job's id, or None. Any error (asleep app, network) -> None."""
    try:
        return (call("GET", "/optimize/pending") or {}).get("job_id")
    except Exception as e:  # noqa: BLE001 — polling must survive anything
        print(f"poller: pending check failed (will retry): {e}", flush=True)
        return None


def refresh_code(repo_dir, run):
    """Sync the box to origin/main so it never runs stale engine code; install
    requirements only when the update changed requirements.txt. Never raises."""
    try:
        def git(*args):
            return run(["git", "-C", repo_dir, *args], capture_output=True)
        before = git("rev-parse", "HEAD").stdout.decode().strip()
        git("fetch", "origin", "main")
        git("reset", "--hard", "origin/main")
        after = git("rev-parse", "HEAD").stdout.decode().strip()
        if before and after and before != after:
            diff = git("diff", "--name-only", before, after).stdout.decode()
            if "requirements.txt" in diff:
                run([sys.executable, "-m", "pip", "install", "-r",
                     os.path.join(repo_dir, "requirements.txt")], capture_output=True)
    except Exception as e:  # noqa: BLE001
        print(f"poller: code refresh failed (running current code): {e}", flush=True)


def run_job(job_id, repo_dir, env, run):
    """One job = one run of the existing cloud worker script. Returns its exit code."""
    job_env = dict(env)
    job_env["JOB_ID"] = job_id
    r = run([sys.executable, os.path.join(repo_dir, "scripts", "cloud_optimize_worker.py")],
            env=job_env, cwd=repo_dir)
    return getattr(r, "returncode", 1)


def main_loop():
    app_url = os.environ["APP_URL"].rstrip("/")
    secret = os.environ["OPTIMIZE_WORKER_SECRET"]
    repo_dir = os.environ.get("REPO_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def call(method, path):
        req = urllib.request.Request(app_url + path, method=method, headers={
            "X-Worker-Secret": secret, "User-Agent": "anvitech-oracle-poller"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    print(f"poller: watching {app_url} every {POLL_S}s", flush=True)
    while True:
        try:
            job_id = check_pending(call)
            if job_id:
                print(f"poller: claiming job {job_id}", flush=True)
                refresh_code(repo_dir, subprocess.run)
                rc = run_job(job_id, repo_dir, os.environ, subprocess.run)
                print(f"poller: job {job_id} finished rc={rc}", flush=True)
        except Exception as e:  # noqa: BLE001 — the loop is immortal
            print(f"poller: loop error (continuing): {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main_loop()
```

Note the claim semantics: the poller does NOT fetch `/optimize/job/{id}` itself — the subprocessed `cloud_optimize_worker.py` does that as its first act, which is what flips `claimed` (Task 2). The 10 s poll + subprocess startup fits comfortably inside the 3-minute claim window.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_oracle_poller.py -v` → PASS. `python3 -c "import sys; sys.path.insert(0,'scripts'); import oracle_optimize_worker"` → clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/oracle_optimize_worker.py tests/test_oracle_poller.py
git commit -m "feat(worker): always-on Oracle poller around the existing cloud worker"
```

---

### Task 5: Setup script, runbook, docs, and the manual-mode E2E

**Files:**
- Create: `scripts/oracle_worker_setup.sh`, `docs/ORACLE_WORKER.md`
- Modify: `CLAUDE.md` (Deploy bullet), `README.md` (one line under the deployment section pointing at the runbook)
- Test: `tests/test_oracle_e2e.py` (new)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: The E2E test (no Oracle needed — real HTTP, manual mode)**

```python
# tests/test_oracle_e2e.py
"""End-to-end: a REAL uvicorn app (manual cloud mode) + the poller functions claim
and complete an optimize job over actual HTTP. Marked slow; tiny budget."""
import json, os, socket, subprocess, sys, time, urllib.request
import pytest

pytestmark = pytest.mark.slow


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture()
def live_app(tmp_path):
    port = _free_port()
    env = dict(os.environ,
               STORE_DIR=str(tmp_path / "store"), DEFAULT_SCHEDULER="new",
               OPTIMIZE_WORKER_SECRET="e2e-secret", GITHUB_DISPATCH_TOKEN="manual",
               ORACLE_CLAIM_TIMEOUT_MIN="5", AUTO_OPTIMIZE="0")
    for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app",
                             "--host", "127.0.0.1", "--port", str(port)], env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):                                   # wait for boot
        try:
            urllib.request.urlopen(base + "/login", timeout=2); break
        except Exception:
            time.sleep(0.5)
    yield base, env
    proc.terminate(); proc.wait(timeout=10)


def test_poller_claims_and_completes_a_job(live_app):
    base, env = live_app
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    # login + seed the sample book
    from tests.new_sample_workbook import build_new_sample_bytes
    body = "username=anvitech&password=1930rail".encode()
    opener.open(urllib.request.Request(base + "/login", data=body, method="POST"), timeout=10)
    wb = build_new_sample_bytes()
    boundary = b"----e2e"
    mp = (b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; "
          b"filename=\"s.xlsx\"\r\nContent-Type: application/octet-stream\r\n\r\n"
          + wb + b"\r\n--" + boundary + b"--\r\n")
    req = urllib.request.Request(base + "/upload", data=mp, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=----e2e"})
    opener.open(req, timeout=30)
    # start an optimize (cloud manual mode parks the job)
    opener.open(urllib.request.Request(
        base + "/run", data=json.dumps({}).encode(), method="POST",
        headers={"Content-Type": "application/json"}), timeout=60)
    opener.open(urllib.request.Request(
        base + "/optimize", data=json.dumps({"budget": "quick"}).encode(), method="POST",
        headers={"Content-Type": "application/json"}), timeout=30)
    # the poller side: check_pending over real HTTP, then run the real worker subprocess
    sys.path.insert(0, "scripts")
    import oracle_optimize_worker as w

    def call(method, path):
        r = urllib.request.Request(base + path, method=method,
                                   headers={"X-Worker-Secret": "e2e-secret"})
        with urllib.request.urlopen(r, timeout=15) as resp:
            return json.loads(resp.read().decode())

    jid = None
    for _ in range(20):
        jid = w.check_pending(call)
        if jid: break
        time.sleep(1)
    assert jid, "poller never saw the pending job"
    wenv = dict(os.environ, APP_URL=base, OPTIMIZE_WORKER_SECRET="e2e-secret")
    rc = w.run_job(jid, os.getcwd(), wenv, subprocess.run)
    assert rc == 0
    # the app must have received the result
    for _ in range(30):
        with urllib.request.urlopen(urllib.request.Request(
                base + "/optimize/status", headers={}), timeout=10) as resp:
            pass
        st = json.loads(opener.open(base + "/optimize/status", timeout=10).read().decode())
        if st["state"] == "done": break
        time.sleep(1)
    assert st["state"] == "done" and st.get("best")
```

> Notes for the implementer: (a) use `/usr/bin/python3`-agnostic `sys.executable`; (b) the quick budget must be tiny — check how the app resolves budgets in cloud mode: the payload's `budget_per_candidate` comes from `cloud_budget`, so set `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE=5` in the fixture env to keep the E2E under ~2 min; (c) register the `slow` marker if the repo's pytest config doesn't have it (check `pytest.ini`/`pyproject`); the full-suite run must still include it once for the record, then it may be deselected by default if the suite config already does that for slow tests — follow the repo's existing convention (grep for `pytest.mark.slow`).

- [ ] **Step 2: Run the E2E**

Run: `pytest tests/test_oracle_e2e.py -v`
Expected: PASS (~1-2 min). Fix whatever it exposes — this is the proof the whole ladder works without Oracle.

- [ ] **Step 3: Write `scripts/oracle_worker_setup.sh`**

```bash
#!/usr/bin/env bash
# One-shot setup for the Anvitech always-on Optimize worker (Oracle free ARM VM).
# Run as a user with sudo on a fresh Ubuntu box:  bash oracle_worker_setup.sh
set -euo pipefail

echo "== Anvitech Optimize worker setup =="
read -rp "App URL (e.g. https://anvitech-ppc.onrender.com): " APP_URL
read -rp "OPTIMIZE_WORKER_SECRET (same value as on Render): " SECRET
read -rp "GitHub read-only token (fine-grained PAT, Contents:read): " PAT
read -rp "GitHub repo (default riittiin/anvitech-ppc-engine): " REPO
REPO=${REPO:-riittiin/anvitech-ppc-engine}

sudo apt-get update -y && sudo apt-get install -y python3 python3-pip git

REPO_DIR="$HOME/anvitech-ppc-engine"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "https://x-access-token:${PAT}@github.com/${REPO}.git" "$REPO_DIR"
fi
python3 -m pip install --user -r "$REPO_DIR/requirements.txt"

sudo tee /etc/anvitech-worker.env >/dev/null <<EOF
APP_URL=${APP_URL}
OPTIMIZE_WORKER_SECRET=${SECRET}
REPO_DIR=${REPO_DIR}
EOF
sudo chmod 600 /etc/anvitech-worker.env

sudo tee /etc/systemd/system/anvitech-optimize-worker.service >/dev/null <<EOF
[Unit]
Description=Anvitech Optimize worker (poll-and-claim)
After=network-online.target
Wants=network-online.target

[Service]
User=${USER}
EnvironmentFile=/etc/anvitech-worker.env
ExecStart=/usr/bin/python3 ${REPO_DIR}/scripts/oracle_optimize_worker.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now anvitech-optimize-worker
echo "== done. Check: sudo systemctl status anvitech-optimize-worker =="
```

Sanity-check with `bash -n scripts/oracle_worker_setup.sh`.

- [ ] **Step 4: Write `docs/ORACLE_WORKER.md`** — the owner runbook, covering exactly: (1) create the Oracle Cloud account (always-free; card asked for identity only); (2) create the VM — VM.Standard.A1.Flex, **4 OCPU / 24 GB**, Ubuntu 22.04+, no inbound ports beyond default SSH (the worker is outbound-only); (3) create the fine-grained GitHub PAT (this repo only, **Contents: Read-only**); (4) SSH in, download+run the setup script, paste the three values; (5) verify: `sudo systemctl status anvitech-optimize-worker`, then press "Start deep search" in the app and watch `journalctl -u anvitech-optimize-worker -f` claim the job; (6) Render env to set: `ORACLE_CLAIM_TIMEOUT_MIN=3` (default anyway) and `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE=300` (the deep knob); (7) operations: update = automatic (the box pulls main per job); restart `sudo systemctl restart ...`; retire = set `ORACLE_CLAIM_TIMEOUT_MIN=0` on Render and delete the VM. Plus the failure-mode table from the spec, copied verbatim.

- [ ] **Step 5: Docs touch-ups**
- `CLAUDE.md` Deploy bullet: add — Oracle always-on worker (poll `GET /optimize/pending`, claim window `ORACLE_CLAIM_TIMEOUT_MIN` default 3 min, ladder Oracle→GitHub→local), deep knob `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE=300` on Render, runbook `docs/ORACLE_WORKER.md`.
- `README.md`: one line in the deployment section linking the runbook.

- [ ] **Step 6: Full suite + commit**

Run: `pytest -q` → green (E2E included or marked per repo convention).

```bash
git add scripts/oracle_worker_setup.sh docs/ORACLE_WORKER.md CLAUDE.md README.md tests/test_oracle_e2e.py
git commit -m "feat(worker): Oracle box setup script, owner runbook, manual-mode E2E"
```

---

## Self-Review

**Spec coverage:** budget knob (T1) ✓; pending endpoint + claim + bypass (T2) ✓; tiered dispatch + `ORACLE_CLAIM_TIMEOUT_MIN` incl. `0` behavior (T3) ✓; poller with code-refresh + immortal loop (T4) ✓; setup script + runbook + docs + E2E-without-Oracle (T5) ✓; failure modes exercised: claim-skip-GitHub, unclaimed-dispatch, zero-window (T3), poller-never-raises (T4), supersede guard untouched (no task touches `/optimize/result`) ✓. The spec's "real Test8 deep run on the owner's box" ship-gate item is deliberately post-merge (needs the owner's VM) — the runbook's verify step covers it.

**Placeholder scan:** T3 Step 3 contains "...existing dispatch-failure fallback block, unchanged..." — that is an explicit keep-as-is instruction referencing code the implementer sees at the named lines, not an omission. T5's runbook step describes required content in full detail rather than embedding a second copy of the spec's table (which it tells the implementer to copy verbatim). No TBDs.

**Type consistency:** `check_pending(call)->str|None`, `refresh_code(repo_dir, run)`, `run_job(job_id, repo_dir, env, run)->int` used identically in T4 tests and T5 E2E; env names consistent everywhere (`ORACLE_CLAIM_TIMEOUT_MIN`, `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE`, `/etc/anvitech-worker.env`); `claimed` flag produced in T2, consumed in T3.
