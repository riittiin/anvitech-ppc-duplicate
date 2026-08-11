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
                run([sys.executable, "-m", "pip", "install", "--user", "-r",
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
