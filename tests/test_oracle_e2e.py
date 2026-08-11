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
               ORACLE_CLAIM_TIMEOUT_MIN="5", AUTO_OPTIMIZE="0",
               # Tiny per-candidate budget so the E2E's real contest finishes in
               # well under a minute instead of the production-sized 150/candidate
               # (new-engine cloud mode) x 12 candidates.
               OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE="5")
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
    st = None
    for _ in range(30):
        st = json.loads(opener.open(base + "/optimize/status", timeout=10).read().decode())
        if st["state"] == "done":
            break
        time.sleep(1)
    assert st is not None and st["state"] == "done" and st.get("best")
