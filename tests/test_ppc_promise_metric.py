from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.order import Order
from ppc_engine.objective.metrics import PlanMetrics, compute_metrics
from ppc_engine.objective.objective import _ceiling_breach, _ontime_breach, score
from ppc_engine.scheduler.schedule import Schedule


def test_promise_slip_by_order():
    o = Order(so_no="SO1", item_code="IT-A", item_name="A", qty=10,
              due_date=date(2026, 8, 20), promise_date=date(2026, 8, 5))
    sched = Schedule(segments=tuple(),
                     completion={("SO1", "IT-A"): datetime(2026, 8, 10, 17, 0)})
    m = compute_metrics(sched, [o], datetime(2026, 7, 29, 8, 0))
    assert m.promise_slip_by_order[("SO1", "IT-A")] == 5   # 10-Aug - 5-Aug

    # order with no promise_date is absent from the map
    o2 = Order(so_no="SO2", item_code="IT-B", item_name="B", qty=10, due_date=date(2026, 8, 20))
    sched2 = Schedule(segments=tuple(), completion={("SO2", "IT-B"): datetime(2026, 8, 30, 17, 0)})
    m2 = compute_metrics(sched2, [o2], datetime(2026, 7, 29, 8, 0))
    assert ("SO2", "IT-B") not in m2.promise_slip_by_order


def _pm(promise_slip):
    """Minimal PlanMetrics for objective-score tests — only promise_slip_by_order
    (plus zeroed everything else) varies between within/over-slack cases."""
    return PlanMetrics(
        total_tardiness_days=0.0,
        max_tardiness_days=0.0,
        late_order_count=0,
        makespan_days=0.0,
        lateness_by_order={},
        promise_slip_by_order=promise_slip,
    )


def test_committed_promise_term_in_score():
    cfg = PlanConfig(
        plan_start=datetime(2026, 7, 29, 8, 0),
        committed_promise_slack_days=3,
        committed_promise_weight=100.0,
    )
    within = score(_pm({("A", "x"): 2.0}), cfg)  # slip 2 <= slack 3 -> no breach
    over = score(_pm({("A", "x"): 6.0}), cfg)  # slip 6, over 3 -> breach 9 -> +900
    assert over > within
    assert abs((over - within) - 100.0 * 9.0) < 1e-6


def test_no_promise_term_contributes_zero():
    """With an empty promise map, the committed-promise term contributes exactly 0 —
    the score equals the pre-existing terms only, so a book with no promise dates is
    byte-identical to before this feature (default slack=3, weight=100 still apply,
    they just have nothing to act on)."""
    cfg = PlanConfig(plan_start=datetime(2026, 7, 29, 8, 0))
    metrics = PlanMetrics(
        total_tardiness_days=1.0,
        max_tardiness_days=1.0,
        late_order_count=1,
        makespan_days=2.0,
        lateness_by_order={("A", "x"): 1.0},
        promise_slip_by_order={},
    )
    pre_existing_terms = (
        cfg.ontime_weight * _ontime_breach(metrics, cfg)
        + cfg.ceiling_weight * _ceiling_breach(metrics, cfg)
        + cfg.makespan_weight * metrics.makespan_days
    )
    assert score(metrics, cfg) == pre_existing_terms
