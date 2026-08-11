"""Plan-result cache (api.main._PLAN_CACHE + _plan_fingerprint).

`_plan` is CPU-heavy, so its result is cached and served instantly when EVERY input
is unchanged (the common login/refresh case). The cache must be *bulletproof against
staleness*: a cache hit is byte-identical to a fresh compute, and ANY change to the
plan's inputs (orders, actuals, absences, config, operators, applied ranks, or a new
day) must force a recompute. These tests pin exactly that."""
import importlib
import json
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store
from engine.models import Order, Actual
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _fresh_api():
    import api.main as m
    importlib.reload(m)          # fresh _PLAN_CACHE / _MASTERS_CACHE per test
    return m


def _seed():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])


def _plan(m):
    return m._plan(m._load_plan_config())


def _recomputes(m, mutate) -> bool:
    """True iff the plan is recomputed (fresh run_id) after `mutate` runs."""
    before = _plan(m)["run_id"]
    mutate()
    return _plan(m)["run_id"] != before


# --------------------------------------------------------------------------- #
# A hit is instant AND identical
# --------------------------------------------------------------------------- #
def test_second_identical_call_is_a_cache_hit():
    m = _fresh_api(); _seed()
    a = _plan(m)
    b = _plan(m)
    # NOT `a is b`: since 2026-08-08 a hit returns a shallow copy whose Orders tab
    # table is rebuilt live from the store, so a change the fingerprint cannot see
    # (archiving an already-finished order) can never be published stale. `run_id` is
    # a fresh uuid per compute, so an equal run_id is the proof nothing was recomputed.
    assert a["run_id"] == b["run_id"]
    assert a == b
    assert a["trace"] is b["trace"]               # the heavy payload is still shared


def test_cache_hit_is_byte_identical_to_a_fresh_recompute():
    m = _fresh_api(); _seed()
    hit = _plan(m)                                # computes + caches
    m._PLAN_CACHE.update(key=None, result=None)   # force a real recompute of the SAME inputs
    fresh = _plan(m)
    assert hit["run_id"] != fresh["run_id"]       # genuinely recomputed
    for k in ("trace", "gantt", "orders", "report", "expected_end",
              "optimize_meta", "resolved_plan_start", "config"):
        assert hit[k] == fresh[k], f"cached and fresh plan differ on {k!r}"


# --------------------------------------------------------------------------- #
# Every input change must invalidate (no stale plan, ever)
# --------------------------------------------------------------------------- #
def test_new_order_invalidates():
    m = _fresh_api(); _seed()
    assert _recomputes(m, lambda: book_store.add_orders(
        [Order("SO9", ITEM_A, ITEM_A, 3, date(2025, 4, 1))]))


def test_captured_actual_invalidates():
    m = _fresh_api(); _seed()
    assert _recomputes(m, lambda: book_store.append_actual(
        Actual(so_no="SO1", item_code=ITEM_A, entry_date=date(2025, 3, 19),
               qty_produced=2, qty_rejected=0, shift="1st shift",
               process="CNC", operator="", item_name=ITEM_A)))


def test_net_zero_good_actual_invalidates():
    """A downtime-only / all-reject actual produces NO good qty, so it doesn't change
    the quantity-derived book signature — but it DOES advance the latest-actual date
    (→ the plan's effective start → the schedule) and the Rule 7 downtime tab. The
    plan cache must still recompute, not serve the pre-punch schedule."""
    m = _fresh_api(); _seed()
    # A prior real punch so the order already has actuals (process_qty already a dict).
    book_store.append_actual(Actual(so_no="SO1", item_code=ITEM_A, entry_date=date(2025, 3, 19),
                                    qty_produced=2, qty_rejected=0, shift="1st shift",
                                    process="CNC", operator="", item_name=ITEM_A))
    # Now a downtime-only entry on a LATER date: 0 good produced.
    assert _recomputes(m, lambda: book_store.append_actual(
        Actual(so_no="SO1", item_code=ITEM_A, entry_date=date(2025, 3, 24),
               qty_produced=0, qty_rejected=0, shift="1st shift", process="CNC",
               operator="", item_name=ITEM_A, machine_breakdown_min=180)))


def test_absence_invalidates():
    m = _fresh_api(); _seed()
    assert _recomputes(m, lambda: book_store.save_absence(
        {"operator": "Op One", "from_date": "2025-03-25", "to_date": "2025-03-26"}))


def test_config_change_invalidates():
    m = _fresh_api(); _seed()

    def mut():
        # consolidation is engine-decided now (normalized out of the signature); mutate a
        # real config input instead — setup time shapes the schedule and must invalidate.
        cfg = m._load_plan_config().to_dict()
        cfg["setup_time_min"] = (cfg.get("setup_time_min") or 90) + 5
        book_store.save_plan_config(json.dumps(cfg))
    assert _recomputes(m, mut)


def test_operator_change_invalidates():
    m = _fresh_api(); _seed()
    _plan(m)                                       # warm (seeds the operator table)

    def mut():
        t = book_store.load_operator_table() or {"week_anchor": "2025-03-07", "operators": []}
        t.setdefault("operators", []).append(
            {"id": "zz", "name": "Brand New Op", "machines_raw": "CNC1",
             "shift": "First shift", "pinned": False})
        book_store.save_operator_table(t)
    assert _recomputes(m, mut)


def test_applying_ranks_invalidates():
    m = _fresh_api(); _seed()
    assert _recomputes(m, lambda: book_store.save_plan_priority(
        {f"SO1\x1f{ITEM_A}": 0, f"SO2\x1f{ITEM_B}": 1}, {"saved_at": "t"}))


def test_new_day_invalidates(monkeypatch):
    m = _fresh_api(); _seed()
    before = _plan(m)["run_id"]
    monkeypatch.setattr(m, "_ist_today", lambda: date(2099, 1, 1))
    assert _plan(m)["run_id"] != before           # 'today' moved -> recompute
