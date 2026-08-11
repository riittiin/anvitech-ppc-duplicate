"""The cloud worker's shard branch: with SHARD_TOTAL>1 it runs a slice and posts
to /optimize/shard-result; unset/1 keeps the legacy /optimize/result path. We
drive main() with a fake _call so no network/app is needed."""
import importlib
import sys


def _load_worker(monkeypatch, tmp_path, shard_index=None, shard_total=None):
    monkeypatch.setenv("APP_URL", "http://app.test")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s")
    monkeypatch.setenv("JOB_ID", "job-xyz")
    if shard_index is not None:
        monkeypatch.setenv("SHARD_INDEX", str(shard_index))
    if shard_total is not None:
        monkeypatch.setenv("SHARD_TOTAL", str(shard_total))
    sys.modules.pop("scripts.cloud_optimize_worker", None)
    return importlib.import_module("scripts.cloud_optimize_worker")


def _fake_payload():
    # Minimal payload contest_jobs/run_contest_slice can consume for the sample book
    # (mirrors tests/test_optimize_shard.py::_classic_payload).
    import io
    from datetime import date
    from tests.sample_workbook import build_sample_bytes
    from engine.loaders import load_all
    from engine.config import Config
    from engine import optimize_service as osvc
    from engine.models import Order
    so_lines, _m = load_all(io.BytesIO(build_sample_bytes()))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    # overlap_percent pinned to a candidate value so the contest stays at
    # exactly len(candidates) contenders (mirrors test_optimize_shard.py).
    cfg = Config(scheduler="classic", plan_start_date=date(2025, 3, 1),
                overlap_percent=60)
    cfg.validate()
    return osvc.build_payload(orders, [], build_sample_bytes(), cfg,
                             seed=1, candidates=(60, 80), budget_per_candidate=4)


def test_worker_shard_posts_shard_result(monkeypatch, tmp_path):
    w = _load_worker(monkeypatch, tmp_path, shard_index=0, shard_total=2)
    posts = []
    payload = _fake_payload()

    def fake_call(method, path, body=None, **kw):
        if path.startswith("/optimize/job/"):
            return {"payload": payload, "cancel": False}
        posts.append((path, body))
        return {"ok": True, "cancel": False}

    monkeypatch.setattr(w, "_call", fake_call)
    assert w.main() == 0
    result_posts = [p for p in posts if p[0] == "/optimize/shard-result"]
    assert len(result_posts) == 1
    body = result_posts[0][1]
    assert body["job_id"] == "job-xyz" and body["shard_index"] == 0
    assert body["shard_total"] == 2 and isinstance(body["rows"], list)
    # a 2-shard slice of 2 classic candidates → exactly 1 candidate this shard
    assert len(body["rows"]) == 1


def test_worker_no_shard_uses_legacy_result(monkeypatch, tmp_path):
    w = _load_worker(monkeypatch, tmp_path)  # no SHARD_* env → legacy
    posts = []
    payload = _fake_payload()

    def fake_call(method, path, body=None, **kw):
        if path.startswith("/optimize/job/"):
            return {"payload": payload, "cancel": False}
        posts.append((path, body))
        return {"ok": True, "cancel": False}

    monkeypatch.setattr(w, "_call", fake_call)
    assert w.main() == 0
    assert any(p[0] == "/optimize/result" for p in posts)
    assert not any(p[0] == "/optimize/shard-result" for p in posts)
