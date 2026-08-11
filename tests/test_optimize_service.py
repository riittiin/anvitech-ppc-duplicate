"""engine/optimize_service.py — the shared cloud/local Optimize service.

The contract that matters: a cloud worker rebuilding the book from a payload
and running the contest must produce BYTE-IDENTICAL results to the local
sweep on the same inputs (fixed seed, shared code path, shared winner rule).
"""
import io
import json
from dataclasses import replace
from datetime import date

from engine import optimize_service as svc
from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import Order, PlanRun
from engine.pipeline import run_forward
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B
from tests.new_sample_workbook import build_new_sample_bytes


def _book(overlap=80):
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    orders = {}
    for i, so in enumerate(so_lines):
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return orders, [], raw, masters, cfg


def _payload(per_candidate=10, **kw):
    orders, actuals, raw, masters, cfg = _book(**kw)
    return svc.build_payload(orders, actuals, raw, cfg, seed=42,
                             budget_per_candidate=per_candidate), masters, cfg


def test_payload_round_trip_reconstructs_the_same_plan():
    payload, masters, cfg = _payload()
    # Simulate the network hop: the worker sees only JSON.
    payload = json.loads(json.dumps(payload))
    orders2, actuals2, masters2, cfg2, absences2, optable2, frozen2 = svc.parse_payload(payload)

    setup = svc.prepare_contest(orders2, actuals2, masters2, cfg2)
    pr = PlanRun(so_lines=list(setup.target))
    run_forward(pr, setup.config, masters2)
    m2 = optimizer.plan_metrics(pr.schedule, setup.target, setup.config.plan_start_date)

    orders, actuals, raw, masters, cfg = _book()
    setup1 = svc.prepare_contest(orders, actuals, masters, cfg)
    pr1 = PlanRun(so_lines=list(setup1.target))
    run_forward(pr1, setup1.config, masters)
    m1 = optimizer.plan_metrics(pr1.schedule, setup1.target,
                                setup1.config.plan_start_date)
    assert m1 == m2


def test_run_contest_matches_the_local_sweep_byte_for_byte():
    """Cloud (run_contest from a payload) == local (sweep_optimize on live
    objects) for the same contenders, per-candidate depth, and seed. The
    machine-set outer loop in run_contest is gated to scheduler=="new" (the
    only scheduler flexible_machines affects — see engine/new_engine._new_masters
    and Task 3's identical gate in optimizer.sweep_optimize), so a classic-mode
    cloud contest stays single-pass and byte-identical to the local sweep,
    exactly as before this feature."""
    payload, masters, cfg = _payload(per_candidate=10)
    payload = json.loads(json.dumps(payload))
    payload["candidates"] = list(optimizer.OVERLAP_CANDIDATES)
    n = len(optimizer.sweep_contenders(cfg.overlap_percent,
                                       optimizer.OVERLAP_CANDIDATES))
    cloud = svc.run_contest(payload, processes=1)

    orders, actuals, raw, masters, cfg = _book()
    setup = svc.prepare_contest(orders, actuals, masters, cfg)
    local = optimizer.sweep_optimize(setup.target, setup.search_config, masters,
                                     budget_evals=10 * n, seed=42)
    assert cloud["winner_overlap"] == local.overlap_percent
    assert cloud["winner_flexible"] is False   # classic ignores the flag; tie keeps current
    assert cloud["best"] == local.result.best
    assert cloud["ranks"] == local.result.ranks
    assert cloud["evals"] == local.evals


def test_run_contest_parallel_equals_sequential():
    payload, _, _ = _payload(per_candidate=6)
    payload = json.loads(json.dumps(payload))
    a = svc.run_contest(payload, processes=1)
    b = svc.run_contest(payload, processes=2, poll_seconds=0.2)
    assert (a["winner_overlap"], a["best"], a["ranks"]) == \
           (b["winner_overlap"], b["best"], b["ranks"])


def test_pick_winner_tie_keeps_the_current_setting():
    best = {"makespan_days": 1.0, "late_orders": 0, "total_late_days": 0,
            "max_late_days": 0, "orders": 1, "ontime_breach": 0.0}
    rows = [{"overlap": 50, "flexible": False, "eligible": True, "best": dict(best)},
            {"overlap": 80, "flexible": False, "eligible": True, "best": dict(best)}]
    assert svc.pick_winner(80, False, rows)["overlap"] == 80
    assert svc.pick_winner(50, False, rows)["overlap"] == 50
    # Ineligible/failed rows never win.
    rows[1]["eligible"] = False
    assert svc.pick_winner(80, False, rows)["overlap"] == 50


def test_pick_winner_tie_also_prefers_current_machine_set():
    best = {"makespan_days": 1.0, "late_orders": 0, "total_late_days": 0,
            "max_late_days": 0, "orders": 1, "ontime_breach": 0.0}
    rows = [{"overlap": 80, "flexible": False, "eligible": True, "best": dict(best)},
            {"overlap": 80, "flexible": True, "eligible": True, "best": dict(best)}]
    # Same score, same overlap — the current machine-set wins the tie.
    assert svc.pick_winner(80, False, rows)["flexible"] is False
    assert svc.pick_winner(80, True, rows)["flexible"] is True


def test_prepare_contest_raises_when_nothing_to_optimize():
    orders, actuals, raw, masters, cfg = _book()
    import pytest
    with pytest.raises(ValueError):
        svc.prepare_contest({}, [], masters, cfg)


def test_run_candidate_progress_and_cancel():
    payload, _, _ = _payload(per_candidate=8)
    seen = []
    row = svc.run_candidate(payload, 80, on_progress=lambda e, b: seen.append(e))
    assert row["eligible"] and row["evals"] <= 8 and row["ranks"]
    assert seen == sorted(seen) and seen[-1] == row["evals"]
    stopped = svc.run_candidate(payload, 80, should_cancel=lambda: True)
    assert stopped["cancelled"] or stopped["evals"] <= 1


def test_lanes_have_zero_scheduling_effect():
    """The pivot's core promise (2026-07-15): commitment lanes are pure status
    labels. A book with committed lines (each with a promised date) must produce
    the SAME prepare_contest target membership and a BYTE-IDENTICAL run_forward
    schedule as the identical book with every line open."""
    # All-open baseline.
    orders_o, actuals_o, raw_o, masters_o, cfg_o = _book()
    setup_o = svc.prepare_contest(orders_o, actuals_o, masters_o, cfg_o)
    pr_o = PlanRun(so_lines=list(setup_o.target))
    run_forward(pr_o, setup_o.config, masters_o)

    # Same book, half the lines committed with promises.
    orders_c, actuals_c, raw_c, masters_c, cfg_c = _book()
    for i, o in enumerate(orders_c.values()):
        if i % 2 == 0:
            o.commitment = "committed"
            o.promised_date = o.delivery_date
    setup_c = svc.prepare_contest(orders_c, actuals_c, masters_c, cfg_c)
    pr_c = PlanRun(so_lines=list(setup_c.target))
    run_forward(pr_c, setup_c.config, masters_c)

    # Target is EVERY active line either way (no lane gates the pool).
    assert {(l.so_no, l.item_code, l.qty) for l in setup_c.target} == \
           {(l.so_no, l.item_code, l.qty) for l in setup_o.target}
    # Byte-identical schedule — lanes have zero scheduling effect.
    assert [e.as_row() for e in pr_c.schedule] == [e.as_row() for e in pr_o.schedule]


def test_book_signature_tracks_material_changes():
    orders, actuals, raw, masters, cfg = _book()
    from engine import orderbook
    lines = orderbook.active_so_lines(orders, actuals, masters)
    s0 = svc.book_signature(lines)
    assert s0 == svc.book_signature(list(reversed(lines)))   # order-insensitive
    lines[0].qty -= 1                                        # production happened
    assert svc.book_signature(lines) != s0
    lines[0].qty += 1
    lines[0].commitment = "committed"                        # lane change
    lines[0].promised_date = date(2025, 4, 1)
    assert svc.book_signature(lines) != s0
    lines[0].commitment, lines[0].promised_date = "open", None
    assert svc.book_signature(lines) == s0                   # restored ⇒ same sig
    assert svc.book_signature(lines, absences=[{"operator": "X",
        "from_date": "2025-03-02", "to_date": "2025-03-03"}]) != s0


def test_book_signature_changes_when_a_delivery_date_changes():
    """A director's date edit must count as a material book change, or the daily
    auto-optimize skips it and the job order never reflects the new date."""
    import datetime
    from engine.models import SOLine
    from engine import optimize_service

    a = SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=datetime.date(2025, 8, 1))
    b = SOLine(so_no="SO1", item_code="A", item_name="A", qty=10,
               delivery_date=datetime.date(2025, 9, 15))

    assert optimize_service.book_signature([a]) != optimize_service.book_signature([b])
    # Same book, same signature — still deterministic.
    assert optimize_service.book_signature([a]) == optimize_service.book_signature([a])


def _new_engine_payload(candidates=(70, 80), budget_per_candidate=20, seed=42):
    """A cloud-contest payload for the NEW (ppc_engine-backed) scheduler — mirrors
    ``_payload()`` above but on the fully-staffed new-engine sample workbook, so
    ``run_contest`` actually exercises the flexible_machines (machine-set) dimension
    end to end."""
    raw = build_new_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                apply_operator_logic=True)
    cfg.validate()
    return svc.build_payload(orders, [], raw, cfg, seed=seed,
                             candidates=list(candidates),
                             budget_per_candidate=budget_per_candidate)


def test_run_contest_returns_winner_flexible(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    payload = _new_engine_payload(candidates=[70, 80], budget_per_candidate=20)
    out = svc.run_contest(payload, processes=1)
    assert "winner_flexible" in out
    assert isinstance(out["winner_flexible"], bool)
    # Both machine-sets were actually tried (rows carry the "flexible" field).
    assert {row["flexible"] for row in out["rows"]} == {False, True}


def test_flow_mode_cloud_contest_uses_chunk_candidates():
    """Flow mode must send the CHUNK candidates in the payload — not the default
    overlap %. Sending overlap values (60..95) would make the worker replace
    flow_chunks with numbers the config rejects (validate caps at 50), erroring
    the whole cloud contest. Regression for the api build_payload candidates fix.
    """
    orders, actuals, raw, masters, cfg = _book()
    cfg = replace(cfg, scheduler="flow", flow_chunks=4)
    cands = svc.cloud_candidates(cfg)
    assert cands == svc.CLOUD_FLOW_CHUNK_CANDIDATES        # chunk list, not overlaps
    assert all(1 <= c <= 50 for c in cands)                # all valid flow_chunks
    payload = svc.build_payload(orders, actuals, raw, cfg, seed=42,
                                candidates=cands, budget_per_candidate=6)
    payload = json.loads(json.dumps(payload))
    res = svc.run_contest(payload, processes=1)            # must NOT raise
    assert res["knob"] == "flow_chunks"
    assert res["best"] is not None
    # the winning value is a real chunk count, and every contender was valid
    allowed = set(cands) | {cfg.flow_chunks}
    assert res["winner_overlap"] in allowed
    assert {row["overlap"] for row in res["rows"]} <= allowed
