"""One number, lower is better — deliberately the SAME formula as the live engine.

Identical scoring is what makes the A/B honest: any difference between the two
plans is then the scheduling, not the yardstick. Written fresh rather than
imported, because this package stands alone and never imports the engine it is
being compared against.

    score = w_ontime  x  sum( (|miss| - band, capped)^2 )     # the whole objective
          + w_ceiling x  sum( (late - ceiling)^2 )            # a no-regression bar
          + w_makespan x  makespan_days                       # a strict tie-break

`abs()` is the owner's rule that early and late are equally bad. Squaring is the
owner's rule that misses must be SPREAD: ten orders 6 days out (10 x 2^2 = 40)
beats one order 30 days out ((30-4)^2 = 676). The cap stops one hopeless order
swamping the plan and the search chasing it instead of the orders it can still
save.

The defaults below are the live engine's own (ontime_band_days 4.0,
ontime_cap_days 60.0, ontime_weight 1.0, makespan_weight 0.1). They are read off
`config` when it carries them, so a future knob reaches both engines; today's
`engine.config.Config` has none of these fields, so the defaults are what
production uses. A numeric differential test scores the same lateness maps
through both implementations and pins them equal
(`tests/test_roster_search.py::test_the_score_matches_the_incumbent_engines_formula`).

The worst-order CEILING is a live term, not a dormant one, and it is reproduced
here. Its default IS None — but the path this engine is measured against never
runs on the default: the API sets the ceiling to the CURRENT plan's worst
lateness on every optimize run, auto and manual alike, at weight 100.0 (a
thousand times the makespan tie-break). It is a no-regression barrier — a
re-optimization may not push any order past today's worst case — so a search
without it can propose plans the live search is structurally forbidden to
propose, and the A/B would be measuring two different questions. It is read off
`config.worst_ceiling_days`, the field the live config carries.

Unlike the on-time term the ceiling is ONE-SIDED (`late - ceiling`, not
`abs(late) - ceiling`): finishing early can never breach a lateness barrier.

ONE term of the live score is deliberately not reproduced: the COMMITTED-PROMISE
penalty. That one is genuinely dormant — it is driven by per-order promise dates,
which exist only on a COMMITTED order, and the whole commitment feature is
switched off in production (`api.main.COMMITMENT_FEATURE_ENABLED = False`), so
its input map is empty and it contributes exactly 0. It also has no input this
engine's `Job` carries. If commitments are ever switched back on, it belongs here
too.
"""

from __future__ import annotations

from dataclasses import dataclass

# ONE definition of when the plan clock starts. Re-deriving it here is exactly the
# defect this repo keeps paying for (2026-08-07: four reporting features rebuilt
# the shop's working hours and disagreed with the engine that built the plan by
# 9,470 minutes). The makespan must be measured from the same instant the
# scheduler actually started from, floor and all.
from roster_engine.scheduler import _plan_start

_ONTIME_WEIGHT = 1.0
_MAKESPAN_WEIGHT = 0.1
_BAND_DAYS = 4.0
_CAP_DAYS = 60.0
_CEILING_WEIGHT = 100.0


@dataclass(frozen=True)
class Metrics:
    """What the objective needs, plus the two numbers the owner reads directly.

    ``lateness_by_order`` is SIGNED days — negative means the order finished
    early, which this objective penalises exactly as hard as finishing late.
    ``total_late_days`` and ``max_late_days`` are TARDINESS only (early orders
    contribute nothing), matching the live engine's ``total_tardiness_days`` /
    ``max_tardiness_days``, because those two are reported to the floor as "late
    days" and an early order is not a late day.
    """

    lateness_by_order: dict
    makespan_days: float
    total_late_days: float
    max_late_days: float


def compute_metrics(plan, jobs, config) -> Metrics:
    """Measure ``plan`` against the jobs' delivery dates.

    A job with no completion is not in this plan and a job with no delivery date
    has no date to miss; neither gets a lateness entry. Recording 0.0 for them —
    the brief's version — would claim they landed exactly on their date, which is
    the one value the objective treats as perfect, so an undated order would have
    silently improved every score it appeared in.
    """
    lateness: dict = {}
    total = 0.0
    worst = 0.0
    for job in jobs:
        end = plan.completion.get(job.key)
        if end is None or job.due is None:
            continue
        days = float((end.date() - job.due).days)
        lateness[job.key] = days
        if days > 0:
            total += days
            if days > worst:
                worst = days
    return Metrics(lateness, _makespan_days(plan, config), total, worst)


def score(metrics: Metrics, config) -> float:
    """Score a plan from its metrics. Lower is better."""
    band = _knob(config, "ontime_band_days", _BAND_DAYS)
    cap = _knob(config, "ontime_cap_days", _CAP_DAYS)
    breach = 0.0
    for late in metrics.lateness_by_order.values():
        over = abs(late) - band
        if over > 0:
            if over > cap:
                over = cap
            breach += over * over
    # Term order matches the live score's, deliberately: float addition is not
    # associative, and the differential test asserts EXACT equality.
    return (_knob(config, "ontime_weight", _ONTIME_WEIGHT) * breach
            + _knob(config, "ceiling_weight", _CEILING_WEIGHT)
            * _ceiling_breach(metrics, config)
            + _knob(config, "makespan_weight", _MAKESPAN_WEIGHT)
            * metrics.makespan_days)


def _ceiling_breach(metrics: Metrics, config) -> float:
    """Squared lateness beyond the worst-order ceiling; 0.0 when there is none.

    The barrier that stops a re-optimization pushing any order past the plan the
    floor already has. One-sided on purpose — an order that finishes EARLY has not
    breached a lateness ceiling, so this reads the SIGNED lateness and never
    `abs()`. The knob is `worst_ceiling_days` on the live config; `ceiling_days` is
    accepted as the same thing under the name the pure engine layer uses.
    """
    ceiling = None
    for name in ("worst_ceiling_days", "ceiling_days"):
        value = getattr(config, name, None)
        if value is not None:
            ceiling = float(value)
            break
    if ceiling is None:
        return 0.0
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = late - ceiling
        if over > 0:
            total += over * over
    return total


def _knob(config, name: str, default: float) -> float:
    """A tunable, read off the config when it has one.

    An explicit ``0.0`` must survive — the brief's ``getattr(...) or default``
    would have replaced a deliberately-zero band or weight with 4.0 / 1.0 and
    silently scored a differently-configured plan.
    """
    value = getattr(config, name, None)
    return default if value is None else float(value)


def _makespan_days(plan, config) -> float:
    """Days from the plan's start to its last completion.

    Measured from the PLAN START, not from the first placement: an idle first
    morning is real elapsed time, and this has to agree with the live engine's
    ``(schedule.makespan_end() - plan_start)`` or the tie-break term would differ
    between the two scores being compared. Rounded to 4 decimals for the same
    reason — the live engine's own metrics module rounds there.
    """
    if not plan.completion:
        return 0.0
    end = max(plan.completion.values())
    return round((end - _plan_start(config)).total_seconds() / 86400.0, 4)
