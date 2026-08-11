from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace as NS

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import score as ppc_score
from engine import optimizer


def _m(latenesses, makespan=40.0):
    lb = {("SO", str(i)): float(v) for i, v in enumerate(latenesses)}
    tard = [max(0.0, v) for v in latenesses]
    return PlanMetrics(total_tardiness_days=sum(tard),
                       max_tardiness_days=max(tard) if tard else 0.0,
                       late_order_count=sum(1 for t in tard if t > 0),
                       makespan_days=makespan, lateness_by_order=lb)


def test_ppc_ceiling_barrier_penalizes_exceeding_the_ceiling():
    # incumbent worst is 46; a plan that pushes an order to 61 breaches by 15.
    cfg = PlanConfig(plan_start=datetime(2025, 3, 1), ceiling_days=46.0)
    within = _m([46.0, 30.0])     # nothing exceeds 46
    breach = _m([61.0, 20.0])     # one order past the ceiling
    assert ppc_score(breach, cfg) > ppc_score(within, cfg)
    # Turning the ceiling OFF must remove exactly ceiling_weight * breach from the
    # score (order 61 is 15 past the ceiling -> 15**2 = 225; order 20 is under it).
    off = replace(cfg, ceiling_days=None)
    assert ppc_score(breach, cfg) - ppc_score(breach, off) == cfg.ceiling_weight * (15 ** 2)


def test_optimizer_plan_metrics_ceiling_breach():
    e = NS(end=datetime(2025, 3, 16, 10, 0), so_refs=["SO1"], item_code="A")  # 15 late
    lines = [NS(so_no="SO1", item_code="A", delivery_date=date(2025, 3, 1))]
    # ceiling 10 -> breach (15-10)^2 = 25
    m = optimizer.plan_metrics([e], lines, date(2025, 3, 1), ceiling_days=10.0)
    assert m["ceiling_breach"] == 25.0
    # no ceiling -> zero breach, and score is unchanged by the term
    m0 = optimizer.plan_metrics([e], lines, date(2025, 3, 1))
    assert m0["ceiling_breach"] == 0.0


def test_optimizer_score_uses_ceiling_breach():
    base = {"total_late_days": 20, "makespan_days": 30.0, "slip_severity": 0.0,
            "ontime_breach": 0.0}
    clean = {**base, "ceiling_breach": 0.0}
    breach = {**base, "ceiling_breach": 25.0}
    assert optimizer.score(breach) > optimizer.score(clean)


from engine.config import Config


def test_inputs_signature_ignores_worst_ceiling():
    import api.main as m
    base = Config(scheduler="new")
    a = m._inputs_signature(base)
    b = m._inputs_signature(replace(base, worst_ceiling_days=46.0))
    assert a == b  # transient per-run value must never change the staleness fingerprint


def test_worst_ceiling_round_trips_through_config_dict():
    c = Config(scheduler="new", worst_ceiling_days=46.0)
    assert Config.from_dict(c.to_dict()).worst_ceiling_days == 46.0
