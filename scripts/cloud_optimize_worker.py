"""Cloud Optimize worker — runs on a GitHub Actions runner (2 vCPU).

Thin HTTP shell around engine/optimize_service.run_contest: fetch the contest
payload from the app, run the full fair contest (contenders fanned out across
the runner's cores), stream progress back (the response carries the admin's
Stop request), and post the result. Stdlib only — no new dependencies.

Env: APP_URL (e.g. https://anvitech-ppc.onrender.com), OPTIMIZE_WORKER_SECRET,
JOB_ID (from the workflow_dispatch input).

Every network call retries with backoff — the free Render instance may be
asleep and needs ~60 s to wake. On any fatal error the worker posts an error
result so the app falls back to local compute immediately instead of waiting
out its watchdog.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import optimize_service  # noqa: E402

APP_URL = os.environ["APP_URL"].rstrip("/")
SECRET = os.environ["OPTIMIZE_WORKER_SECRET"]
JOB_ID = os.environ["JOB_ID"]

PROGRESS_EVERY_S = 5.0


def _shard_env():
    """(shard_index, shard_total) from the matrix; (0, 1) = whole contest."""
    try:
        idx = int(os.environ.get("SHARD_INDEX", "0"))
    except ValueError:
        idx = 0
    try:
        total = int(os.environ.get("SHARD_TOTAL", "1"))
    except ValueError:
        total = 1
    return idx, max(1, total)


def _call(method, path, body=None, *, tries=5, timeout=120):
    """One authenticated JSON call to the app, with wake-up-tolerant retries."""
    url = f"{APP_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Worker-Secret": SECRET,
            "Content-Type": "application/json",
            "User-Agent": "anvitech-cloud-optimize-worker",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):     # job gone/superseded — do not retry
                raise
            last = e
        except Exception as e:  # noqa: BLE001 — network blips, sleeping Render
            last = e
        time.sleep(min(15, 2 ** attempt))
    raise RuntimeError(f"{method} {path} failed after {tries} tries: {last}")


def main() -> int:
    job = _call("GET", f"/optimize/job/{JOB_ID}")
    payload = job["payload"]
    n_procs = max(1, os.cpu_count() or 1)
    print(f"worker: job {JOB_ID}: {len(payload['orders'])} orders, "
          f"{len(payload['candidates'])} overlap candidates x "
          f"{payload['budget_per_candidate']} plans, {n_procs} processes",
          flush=True)

    state = {"evals": 0, "best": None, "cancel": bool(job.get("cancel")), "done": False}

    def poster():
        """Progress heartbeat (plans tried + best score so far); the response carries the
        admin's Stop click."""
        while not state["done"]:
            time.sleep(PROGRESS_EVERY_S)
            if state["done"]:
                return
            try:
                body = {"job_id": JOB_ID, "evals": state["evals"],
                        "shard_index": _shard_env()[0]}
                b = state["best"]
                if b is not None:
                    body["best"] = {"score": round(b)} if isinstance(b, (int, float)) else b
                r = _call("POST", "/optimize/progress", body, tries=2, timeout=30)
                if r.get("cancel"):
                    state["cancel"] = True
            except Exception as e:  # noqa: BLE001 — a missed beat is fine
                print(f"worker: progress post failed (non-fatal): {e}", flush=True)

    def _on_prog(evals, best):
        state["evals"] = evals
        if best is not None:
            state["best"] = best

    shard_index, shard_total = _shard_env()
    threading.Thread(target=poster, daemon=True).start()
    try:
        if shard_total > 1:
            out = optimize_service.run_contest_slice(
                payload, shard_index, shard_total, processes=n_procs,
                on_progress=_on_prog, should_cancel=lambda: state["cancel"])
            state["done"] = True
            _call("POST", "/optimize/shard-result", {
                "job_id": JOB_ID, "shard_index": shard_index,
                "shard_total": shard_total, "rows": out["rows"],
                "evals": out["evals"], "cancelled": out["cancelled"]})
            print(f"worker: shard {shard_index}/{shard_total} done — "
                  f"{len(out['rows'])} candidates, {out['evals']} plans", flush=True)
            return 0
        out = optimize_service.run_contest(
            payload, processes=n_procs, on_progress=_on_prog,
            should_cancel=lambda: state["cancel"])
        state["done"] = True
        _call("POST", "/optimize/result", {
            "job_id": JOB_ID, "winner_overlap": out["winner_overlap"],
            "winner_flexible": out.get("winner_flexible", False),
            "ranks": out["ranks"], "best": out["best"], "rows": out["rows"],
            "evals": out["evals"], "cancelled": out["cancelled"]})
        print(f"worker: done — winner overlap {out['winner_overlap']}, "
              f"{out['evals']} plans", flush=True)
        return 0
    except Exception as e:  # noqa: BLE001 — tell the app so it can finalize/fall back
        state["done"] = True
        # Print only the exception TYPE, never its message: a scheduler
        # RuleError carries a record_id (an SO#/item code) in str(e), which
        # would otherwise leak an order identifier into the public GitHub
        # Actions log. The full message still goes to the app over the
        # authenticated HTTPS POST below.
        print(f"worker: FAILED: {type(e).__name__}", flush=True)
        try:
            if shard_total > 1:
                _call("POST", "/optimize/shard-result",
                      {"job_id": JOB_ID, "shard_index": shard_index,
                       "shard_total": shard_total, "rows": [], "evals": 0,
                       "cancelled": False, "error": str(e)[:500]},
                      tries=2, timeout=30)
            else:
                _call("POST", "/optimize/result",
                      {"job_id": JOB_ID, "error": str(e)[:500]}, tries=2, timeout=30)
        except Exception:
            pass                          # the app's watchdog still covers us
        return 1


if __name__ == "__main__":
    sys.exit(main())
