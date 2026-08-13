"""The objective and the alternating sequence x crew search.

The brief's own version of these tests could not run (``Routing`` takes six
arguments, not three) and its headline test — "the crew genome actually changes a
plan" — was VACUOUS-FALSE on the brief's own fixture: two operators for two
machines means both machines are manned whatever the crew genome says, so the
plans are byte-identical and the assertion fails. The crew lever only bites when
operators are SCARCER than machines, which is the whole reason the lever exists.
Both crew tests here are built on scarce crews and were checked against the
mutation "make crew_rank a constant".
"""

import os
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta

from engine.config import Config
from engine.models import Machine, Masters, Operator, Process, Routing, WorkCalendar
from roster_engine import objective, scheduler, search
from roster_engine.domain import build_jobs, build_shop

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _B:
    def __init__(self, key, qty, due, item="ITEM"):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs, self.delivery_date = [key], due
        self.process_remaining = None


def _cfg(**kw):
    return Config(plan_start_date=date(2026, 8, 12), setup_time_min=90.0, **kw)


def _routing(item, allotted, cycle=10.0):
    # engine.models.Routing is (item_code, description, customer, rm_type, moq,
    # processes) — the brief's three-argument call put the process LIST in the
    # `customer` slot, leaving `processes` empty, so build_jobs would have skipped
    # every batch for "no routing" and every test below would have run on no jobs.
    return Routing(item, "d", "", "", None,
                   [Process(1, "CNC FIRST SIDE", cycle, None, None, allotted)])


def _fixture(n=6):
    """Two machines, two operators — the brief's fixture, with a routing that
    constructs. Fine for budget/determinism/genome-shape checks; deliberately NOT
    used for the crew-lever tests (see the module docstring)."""
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5),
                  "CNC4": Machine("CNC4", "CNC 4", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"ITEM": _routing("ITEM", "CNC1/CNC4")},
        operators=[Operator("Narayan", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift"),
                   Operator("Sidhu", "CNC1/CNC4", ["CNC1", "CNC4"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30 + 25 * i, date(2026, 8, 20) + timedelta(days=i))
               for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


def _scarce_fixture():
    """ONE operator, two machines, one job locked to each. Which machine the single
    operator mans is decided by nothing but the crew genome, and it decides which
    of the two orders is built first — so the lever moves real completion dates,
    not just a label."""
    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in ("CNC1", "CNC2")}
    masters = Masters(
        machines=machines,
        routings={"A": _routing("A", "CNC1"), "B": _routing("B", "CNC2")},
        operators=[Operator("Narayan", "", ["CNC1", "CNC2"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B("JA", 300, date(2026, 8, 26), item="A"),
               _B("JB", 300, date(2026, 8, 18), item="B")]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


def _shared_machine_fixture():
    """ONE operator, THREE interchangeable machines. Every job could run anywhere,
    so the crew genome alone decides where the work physically lands."""
    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in ("CNC1", "CNC2", "CNC3")}
    masters = Masters(
        machines=machines,
        routings={"ITEM": _routing("ITEM", "CNC1/CNC2/CNC3")},
        operators=[Operator("Narayan", "", ["CNC1", "CNC2", "CNC3"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30, date(2026, 8, 20) + timedelta(days=i))
               for i in range(4)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


# --------------------------------------------------------------------------- #
# The objective
# --------------------------------------------------------------------------- #

def test_metrics_are_measured_for_every_job_in_the_plan():
    jobs, shop, cfg = _fixture()
    plan = scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=1.0)
    m = objective.compute_metrics(plan, jobs, cfg)
    assert m.makespan_days > 0
    assert set(m.lateness_by_order) == {j.key for j in jobs}
    assert objective.score(m, cfg) >= 0


def test_finishing_early_is_free_and_finishing_late_is_not():
    """The owner's LATE-ONLY on-time rule (2026-08-13, reversing 2026-08-06).

    An order 20 days early must cost exactly what an order dead on time costs —
    nothing beyond the makespan tie-break — while 20 days LATE still costs
    (20-4)^2. Held at the same makespan so the tie-break cannot mask either side.
    """
    cfg = _cfg()
    early = objective.Metrics({"A": -20.0}, 1.0, 0.0, 0.0)
    on_time = objective.Metrics({"A": 0.0}, 1.0, 0.0, 0.0)
    late = objective.Metrics({"A": 20.0}, 1.0, 20.0, 20.0)
    assert objective.score(early, cfg) == objective.score(on_time, cfg)
    assert objective.score(early, cfg) < objective.score(late, cfg)
    assert objective.score(late, cfg) - objective.score(early, cfg) == (20.0 - 4.0) ** 2


def test_earliness_is_free_at_every_magnitude_and_in_bulk():
    """Not one lucky value: nothing early ever costs, alone or piled up — and the
    cap is not what is hiding it (-64 and -900 are both past the 60-day cap)."""
    cfg = _cfg()
    baseline = objective.score(objective.Metrics({}, 1.0, 0.0, 0.0), cfg)
    for d in (-4.5, -5.0, -20.0, -60.0, -64.0, -900.0):
        m = objective.Metrics({"A": d}, 1.0, 0.0, 0.0)
        assert objective.score(m, cfg) == baseline, f"{d} days early must be free"
    bulk = objective.Metrics({f"O{i}": -30.0 for i in range(25)}, 1.0, 0.0, 0.0)
    assert objective.score(bulk, cfg) == baseline


def test_the_late_side_of_the_term_is_untouched_by_the_reversal():
    """Band 4, cap 60, squared, weight 1.0 — the numbers the 2026-08-06 term had.
    Only the earliness half was removed."""
    cfg = _cfg()
    tie = 0.1 * 1.0                                   # the makespan tie-break at 1 day
    for days, expected in ((4.0, 0.0), (5.0, 1.0), (20.0, 256.0),
                           (30.0, 676.0), (64.0, 3600.0), (900.0, 3600.0)):
        m = objective.Metrics({"A": days}, 1.0, days, days)
        assert objective.score(m, cfg) - tie == expected, days


def test_misses_spread_across_orders_beat_one_hopeless_order():
    cfg = _cfg()
    spread = objective.Metrics({f"O{i}": 6.0 for i in range(10)}, 1.0, 60.0, 6.0)
    concentrated = objective.Metrics({"O0": 30.0}, 1.0, 30.0, 30.0)
    assert objective.score(spread, cfg) < objective.score(concentrated, cfg)


def test_one_hopeless_order_is_capped():
    """Beyond the cap an order stops getting worse, so the search keeps working on
    the orders it can still save instead of chasing a lost one."""
    cfg = _cfg()
    hopeless = objective.Metrics({"O0": 400.0}, 1.0, 400.0, 400.0)
    ruined = objective.Metrics({"O0": 4000.0}, 1.0, 4000.0, 4000.0)
    assert objective.score(hopeless, cfg) == objective.score(ruined, cfg)
    # And the cap is the incumbent's 60 free-of-band days, not something else.
    assert objective.score(hopeless, cfg) - 0.1 * 1.0 == 60.0 ** 2


def test_a_miss_inside_the_band_is_free():
    cfg = _cfg()
    inside = objective.Metrics({"O0": 4.0, "O1": -4.0}, 0.0, 4.0, 4.0)
    assert objective.score(inside, cfg) == 0.0
    # The band is 4 days wide, not "everything is free": one more day LATE costs.
    # Without this the test would pass on an objective that scored nothing at all.
    assert objective.score(objective.Metrics({"O0": 5.0}, 0.0, 5.0, 5.0), cfg) == 1.0


def test_makespan_is_only_a_tie_break():
    """A whole extra day of makespan must never outweigh one day of miss beyond
    the band, or the objective would trade deliveries for a shorter plan."""
    cfg = _cfg()
    long_plan = objective.Metrics({"O0": 0.0}, 40.0, 0.0, 0.0)
    short_but_late = objective.Metrics({"O0": 6.0}, 1.0, 6.0, 6.0)
    assert objective.score(long_plan, cfg) < objective.score(short_but_late, cfg)


def test_the_score_matches_the_incumbent_engines_formula():
    """The A/B is only honest if both engines are measured with the same yardstick.

    roster_engine may not import ppc_engine, so the formula is written fresh — and
    that is exactly the kind of duplication that drifts. This pins them together
    numerically instead: the same lateness map, scored by both, over zero,
    inside-band, beyond-band and beyond-CAP values — and at every worst-order
    CEILING the live path actually sets, which is not only None (see
    `test_the_worst_order_ceiling_is_reproduced_not_assumed_dormant`).

    **Restricted to NON-EARLY books on 2026-08-13, deliberately.** The owner made
    earliness free in `roster_engine/objective.py` and in `engine/optimizer.py`,
    the two scorers that pick and report the live contest winner. `ppc_engine/` is
    a vendored package carrying its own SYMMETRIC mirror, and it was left alone —
    it is reachable only from the retired `new` engine's internal search, not from
    the roster path this engine is measured on. So the two formulas now genuinely
    diverge on early orders, and pinning them equal there would pin a bug. The
    divergence is asserted explicitly rather than merely dodged, in
    `test_earliness_diverges_from_the_vendored_ppc_mirror_on_purpose`.
    """
    import random

    from ppc_engine.config import PlanConfig
    from ppc_engine.objective import objective as ppc_objective
    from ppc_engine.objective.metrics import PlanMetrics

    rng = random.Random(11)
    cases = [{"A": 0.0}, {"A": 4.0}, {"A": 4.5}, {"A": 5.0},
             {"A": 64.0}, {"A": 64.1}, {"A": 900.0}, {}]
    for _ in range(40):
        cases.append({f"O{i}": float(rng.randint(0, 90)) for i in range(8)})
    for ceiling in (None, 0.0, 10.0, 46.0):
        cfg = _cfg(worst_ceiling_days=ceiling)
        ppc_cfg = PlanConfig(plan_start=datetime(2026, 8, 12, 8, 0),
                             ceiling_days=ceiling)
        for lateness in cases:
            for makespan in (0.0, 1.0, 37.5):
                mine = objective.Metrics(dict(lateness), makespan, 0.0, 0.0)
                theirs = PlanMetrics(
                    total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=makespan,
                    lateness_by_order=dict(lateness))
                assert objective.score(mine, cfg) == ppc_objective.score(
                    theirs, ppc_cfg), (ceiling, lateness, makespan)
    # The fixture must really exercise the on-time term, or "they agree" would be
    # the trivial truth that both scored 0.
    assert ppc_objective.score(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order={"A": 30.0}),
        PlanConfig(plan_start=datetime(2026, 8, 12, 8, 0))) == 676.0


def test_earliness_diverges_from_the_vendored_ppc_mirror_on_purpose():
    """The one place the two formulas are now KNOWN to disagree, pinned so nobody
    "fixes" it back by re-symmetrising this engine.

    `ppc_engine/` was deliberately left symmetric on 2026-08-13: it is vendored,
    and its copy of this term drives only the retired `new` engine's internal
    search. The roster engine — the one under test — is late-only.
    """
    from ppc_engine.config import PlanConfig
    from ppc_engine.objective import objective as ppc_objective
    from ppc_engine.objective.metrics import PlanMetrics

    cfg = _cfg()
    ppc_cfg = PlanConfig(plan_start=datetime(2026, 8, 12, 8, 0))
    early = {"A": -30.0}
    mine = objective.score(objective.Metrics(dict(early), 0.0, 0.0, 0.0), cfg)
    theirs = ppc_objective.score(
        PlanMetrics(total_tardiness_days=0.0, max_tardiness_days=0.0,
                    late_order_count=0, makespan_days=0.0,
                    lateness_by_order=dict(early)), ppc_cfg)
    assert mine == 0.0, "roster_engine must charge nothing for an early order"
    assert theirs == 676.0, "ppc_engine's vendored mirror stays symmetric"


def test_the_worst_order_ceiling_is_reproduced_not_assumed_dormant():
    """The ceiling is NOT dormant in the path this engine is compared against:
    `api.main` sets `worst_ceiling_days` to the incumbent's `max_late_days` on
    EVERY optimize run, auto and manual. Without this term the roster search would
    happily propose a plan the live search is structurally forbidden to propose —
    one that pushes an order past today's worst case.
    """
    late = objective.Metrics({"A": 20.0}, 0.0, 20.0, 20.0)
    unbarred = objective.score(late, _cfg())
    barred = objective.score(late, _cfg(worst_ceiling_days=10.0))
    # The live weight is 100.0 — a thousand times the makespan tie-break.
    assert barred - unbarred == 100.0 * (20.0 - 10.0) ** 2
    # Under the ceiling costs nothing extra...
    inside = objective.Metrics({"A": 9.0}, 0.0, 9.0, 9.0)
    assert objective.score(inside, _cfg(worst_ceiling_days=10.0)) == objective.score(
        inside, _cfg())
    # ...and so does finishing EARLY: the barrier is one-sided, unlike the on-time
    # term. `late - ceiling`, never `abs(late) - ceiling`.
    early = objective.Metrics({"A": -20.0}, 0.0, 0.0, 0.0)
    assert objective.score(early, _cfg(worst_ceiling_days=10.0)) == objective.score(
        early, _cfg())


def test_makespan_is_measured_from_the_plan_start_like_the_incumbent():
    """Not from the first placement: an idle first morning is real makespan, and
    ppc's compute_metrics measures (last completion - plan_start).

    The fixture is deliberately one whose first placement is NOT at plan start —
    every other fixture here starts cutting at 08:00 on day one, which makes the
    two formulas agree and the test vacuous (measured: reverting to
    `min(p.start ...)` passed all 26 tests).
    """
    jobs, shop, cfg = _night_shift_fixture()
    plan = scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=1.0)
    start = datetime(2026, 8, 12, 8, 0)
    first = min(p.start for p in plan.placements)
    assert first > start, "fixture no longer idles before its first placement"
    m = objective.compute_metrics(plan, jobs, cfg)
    expected = (max(plan.completion.values()) - start).total_seconds() / 86400.0
    assert m.makespan_days == round(expected, 4)
    # And the two formulas really do disagree on this plan.
    assert m.makespan_days != round(
        (max(plan.completion.values()) - first).total_seconds() / 86400.0, 4)


def _night_shift_fixture():
    """One machine, one SECOND-shift operator: nothing can run before 19:00, so
    the plan's first placement is 11 hours after the plan starts."""
    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"ITEM": _routing("ITEM", "CNC1")},
        operators=[Operator("Sidhu", "", ["CNC1"], "Second shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 30, date(2026, 8, 20) + timedelta(days=i))
               for i in range(2)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


def test_tardiness_totals_count_only_late_orders():
    cfg = _cfg()
    plan = scheduler.Plan((), {"A": datetime(2026, 8, 30, 9, 0),
                               "B": datetime(2026, 8, 1, 9, 0)})
    jobs = [_job("A", date(2026, 8, 20)), _job("B", date(2026, 8, 20))]
    m = objective.compute_metrics(plan, jobs, cfg)
    assert m.lateness_by_order == {"A": 10.0, "B": -19.0}
    assert m.total_late_days == 10.0
    assert m.max_late_days == 10.0


def test_a_job_with_no_delivery_date_is_not_scored_as_on_time():
    """Scoring it 0.0 would claim it landed exactly on a date it does not have."""
    cfg = _cfg()
    plan = scheduler.Plan((), {"A": datetime(2026, 8, 30, 9, 0)})
    m = objective.compute_metrics(plan, [_job("A", None)], cfg)
    assert m.lateness_by_order == {}
    assert objective.score(m, cfg) == 0.1 * m.makespan_days


def _job(key, due):
    from roster_engine.domain import Job
    return Job(key=key, item_code="ITEM", qty=1, due=due, so_refs=(), ops=(),
               remaining=None)


# --------------------------------------------------------------------------- #
# The search
# --------------------------------------------------------------------------- #

def test_the_search_never_returns_worse_than_its_starting_point():
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert res.score <= res.baseline_score


def test_the_search_actually_improves_on_its_seed():
    """Otherwise "never worse than the seed" passes on a search that does nothing."""
    jobs, shop, cfg = _scarce_fixture()
    res = search.optimize(jobs, shop, cfg, overlap=1.0, budget_evals=60, seed=7)
    assert res.score < res.baseline_score


def test_the_search_is_deterministic_for_a_seed():
    jobs, shop, cfg = _fixture()
    a = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    b = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert (a.sequence, a.crew_rank, a.score, a.evaluations) == (
        b.sequence, b.crew_rank, b.score, b.evaluations)


def test_the_search_returns_both_genomes():
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert sorted(res.sequence) == sorted(j.key for j in jobs)
    assert set(res.crew_rank) == {"CNC1", "CNC4"}
    assert sorted(res.crew_rank.values()) == [0, 1]


def test_the_crew_rank_is_a_permutation_of_the_machining_machines():
    """Every machining machine, each rank exactly once — a rank map with a hole or
    a duplicate is one the roster cannot order machines by."""
    jobs, shop, cfg = _shared_machine_fixture()
    res = search.optimize(jobs, shop, cfg, overlap=1.0, budget_evals=30, seed=3)
    assert set(res.crew_rank) == set(shop.machining_ids)
    assert sorted(res.crew_rank.values()) == list(range(len(shop.machining_ids)))


def test_the_budget_is_respected():
    jobs, shop, cfg = _fixture(n=8)
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=25, seed=7)
    assert res.evaluations <= 25
    # Binding, not merely satisfied: the search wants more than 25 here, so a
    # version that ignored the budget would sail past it.
    assert res.evaluations == 25


def test_cancellation_keeps_the_best_so_far():
    jobs, shop, cfg = _fixture()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 5

    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=500, seed=7,
                          should_cancel=stop)
    assert res.cancelled
    assert res.evaluations < 500
    assert res.score <= res.baseline_score
    assert res.score < float("inf")
    assert sorted(res.sequence) == sorted(j.key for j in jobs)


def test_stopping_before_any_feasible_plan_is_not_reported_as_a_broken_book():
    """A Stop press must stop the search, never diagnose the data.

    On this book the seed crew genome is infeasible (see `_inverted_serial_fixture`),
    so until the crew phase gets a turn every candidate scores +inf. Cancelling in
    that window used to re-raise `Unschedulable` — a typed error whose message
    reads as a missing operator qualification and would send the owner hunting for
    a data problem that does not exist.
    """
    jobs, shop, cfg = _inverted_serial_fixture()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1

    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=500, seed=7,
                          should_cancel=stop)
    assert res.cancelled
    assert res.evaluations < 500


def test_a_budget_too_small_to_reach_the_crew_phase_is_not_a_broken_book():
    """Same class, no cancellation: a budget of 1 or 2 is spent entirely on seeds
    that share the infeasible seed crew, so the search never reaches the dimension
    that could fix it. That is a budget too small, not a book that cannot be run —
    and with a budget of 3 the very same call succeeds."""
    jobs, shop, cfg = _inverted_serial_fixture()
    for budget in (1, 2):
        res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=budget,
                              seed=7)
        assert res.evaluations <= budget
    assert search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=3,
                           seed=7).score < float("inf")


def test_an_infeasible_seed_crew_does_not_burn_the_sequence_share():
    """When the incumbent crew cannot be run, every sequence neighbour is +inf
    too — nothing is ever accepted, but each draw is a real evaluation, so the
    sequence phase used to draw its whole 60% share against a genome it could not
    fix. Measured before: 30 of 50, 90 of 150, 180 of 300 — a flat 60% at every
    budget. Feasibility lives in the CREW genome, so that phase goes first.
    """
    jobs, shop, cfg = _inverted_serial_fixture(n=8)
    wasted = {}
    for budget in (50, 150):
        seen = []
        res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=budget,
                              seed=7, on_eval=lambda n, m: seen.append(m))
        wasted[budget] = sum(1 for metrics in seen if metrics is None)
        assert res.evaluations == budget, budget      # the budget IS spent
        assert res.score < float("inf")
        assert wasted[budget] <= 5, (budget, wasted)  # was 30 and 90
    # And what waste is left is the unavoidable seeds, not a SHARE: tripling the
    # budget must not triple it.
    assert wasted[150] <= wasted[50] + 2, wasted


class _FakeEvaluator:
    """A landscape with a known global minimum and no scheduler in sight.

    `score = sum(position x item)` is minimised, by the rearrangement inequality,
    exactly when the items descend — so from that genome EVERY neighbour is
    strictly worse, which is the only state in which an acceptance rule can be
    caught misbehaving.
    """

    def __init__(self, budget):
        self.count, self._budget = 0, budget

    @property
    def exhausted(self):
        return self.count >= self._budget

    def __call__(self, sequence, _crew):
        self.count += 1
        value = float(sum(pos * item for pos, item in enumerate(sequence)))
        return value, value, False


def test_a_climb_never_accepts_a_worse_genome():
    """Pins the acceptance rule where it lives, not where its effect is visible.

    Keeping the best across restarts means the RESULT is protected by the ledger
    even if the climb wandered — measured: mutating `cand_value < value` to accept
    everything left all 32 tests green once restarts existed. A climb that accepts
    anything is a random walk wearing a hill-climber's name, and it would quietly
    waste the whole budget, so it is asserted directly.
    """
    import random

    ev = _FakeEvaluator(60)
    start = list(range(7, -1, -1))        # the global minimum of this landscape
    floor = float(sum(pos * item for pos, item in enumerate(start)))
    genome, value, _metrics = search._climb(
        start, floor, floor, ev, random.Random(0), lambda g: (g, ()), None)
    assert ev.count == 60, "the climb must actually have tried neighbours"
    assert value == floor
    assert genome == start


def test_leftover_budget_is_spent_instead_of_being_handed_back():
    """The alternating descent converges long before a real budget is gone — this
    6-job book spent exactly 60 evaluations of 150, 400 or 1000 alike. That
    remainder is free, and at a small budget on a big book restarts are what pay.
    So on convergence the search restarts from a fresh genome and keeps the GLOBAL
    best, under the same strict-improvement gate.
    """
    jobs, shop, cfg = _fixture()
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=400, seed=7)
    assert res.evaluations > 300, res.evaluations
    assert res.evaluations <= 400
    # And the restarts may never hand back something worse than the seed.
    assert res.score <= res.baseline_score


def test_on_eval_is_called_once_per_real_evaluation():
    jobs, shop, cfg = _fixture()
    seen = []
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=20, seed=7,
                          on_eval=lambda n, m: seen.append((n, m)))
    assert [n for n, _m in seen] == list(range(1, res.evaluations + 1))


def test_no_genome_is_ever_decoded_twice():
    """The search revisits genomes constantly; re-deciding a plan it has already
    decided is pure waste at ~35 ms a go.

    Asserting `schedule() calls == res.evaluations` — the obvious check — is an
    IDENTITY that holds with or without the cache, because a cache miss counts an
    evaluation and a cache hit does neither. Measured: deleting the memo entirely
    left 26 of 26 tests green. What only the memo can deliver is that no genome is
    decoded a SECOND time, so that is what is asserted here, together with the
    non-vacuity fact that the evaluator really was asked the same question twice.
    """
    jobs, shop, cfg = _fixture()
    genomes = []
    real_sched = scheduler.schedule
    real_call = search._Evaluator.__call__
    asked = {"n": 0}

    def counting_schedule(jobs_, sequence, shop_, config_, **kw):
        genomes.append((tuple(sequence),
                        tuple(sorted((kw.get("crew_rank") or {}).items()))))
        return real_sched(jobs_, sequence, shop_, config_, **kw)

    def counting_call(self, sequence, crew):
        asked["n"] += 1
        return real_call(self, sequence, crew)

    search.scheduler.schedule = counting_schedule
    search._Evaluator.__call__ = counting_call
    try:
        res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    finally:
        search.scheduler.schedule = real_sched
        search._Evaluator.__call__ = real_call

    assert len(genomes) == res.evaluations
    assert len(set(genomes)) == len(genomes), (
        f"{len(genomes) - len(set(genomes))} genomes were decoded more than once")
    # Non-vacuous: the search DID ask for genomes it had already seen, and the
    # memo is what served them. (Measured on this fixture: 73 asks, 40 decodes.)
    assert asked["n"] > res.evaluations


def test_an_empty_book_is_not_a_crash():
    _jobs, shop, cfg = _fixture()
    res = search.optimize([], shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert res.sequence == [] and res.evaluations == 0


# --------------------------------------------------------------------------- #
# The crew genome is a real lever, not a decoration
# --------------------------------------------------------------------------- #

def test_the_crew_genome_actually_changes_a_plan():
    """If it cannot move a plan it is a decorative lever, like the operator pin
    that turned out never to reach the planner (2026-08-05).

    One operator, three interchangeable machines: the genome decides which machine
    the scarce person mans, and therefore where every job physically runs.
    """
    jobs, shop, cfg = _shared_machine_fixture()
    seq = [j.key for j in jobs]
    a = scheduler.schedule(jobs, seq, shop, cfg, overlap=1.0,
                           crew_rank={"CNC1": 0, "CNC2": 1, "CNC3": 2})
    b = scheduler.schedule(jobs, seq, shop, cfg, overlap=1.0,
                           crew_rank={"CNC3": 0, "CNC1": 1, "CNC2": 2})
    sig = lambda p: sorted((x.job_key, x.machine) for x in p.placements)
    assert sig(a) != sig(b)
    assert {x.machine for x in a.placements} == {"CNC1"}
    assert {x.machine for x in b.placements} == {"CNC3"}


def test_the_crew_genome_changes_the_delivery_dates_and_the_score():
    """Moving a machine LABEL would be worthless. This moves the number the owner
    is judged on."""
    jobs, shop, cfg = _scarce_fixture()
    seq = [j.key for j in jobs]
    a = scheduler.schedule(jobs, seq, shop, cfg, overlap=1.0,
                           crew_rank={"CNC1": 0, "CNC2": 1})
    b = scheduler.schedule(jobs, seq, shop, cfg, overlap=1.0,
                           crew_rank={"CNC2": 0, "CNC1": 1})
    assert a.completion != b.completion
    sa = objective.score(objective.compute_metrics(a, jobs, cfg), cfg)
    sb = objective.score(objective.compute_metrics(b, jobs, cfg), cfg)
    assert sb < sa


def test_the_search_finds_the_better_crew_when_the_sequence_cannot_help():
    """The scarce fixture's two jobs are locked to one machine each, so no job
    order can change which one is built first — only the crew genome can. A
    constant crew_rank returns the identity order and this fails.
    """
    jobs, shop, cfg = _scarce_fixture()
    res = search.optimize(jobs, shop, cfg, overlap=1.0, budget_evals=60, seed=7)
    assert res.crew_rank == {"CNC2": 0, "CNC1": 1}


def _serial_fixture():
    """One operator; step 1 runs only on CNC1 and step 2 only on CNC2. Ranking the
    DOWNSTREAM machine first mans an empty chuck every shift while the machine
    that feeds it stays dark, and the plan never places anything — a genome the
    shop genuinely cannot run."""
    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in ("CNC1", "CNC2")}
    masters = Masters(
        machines=machines,
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                  [Process(1, "CNC FIRST SIDE", 4.0, None, None, "CNC1"),
                                   Process(2, "CNC SECOND SIDE", 3.0, None, None, "CNC2")])},
        operators=[Operator("Narayan", "", ["CNC1", "CNC2"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 40, date(2026, 9, 1) + timedelta(days=i))
               for i in range(3)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


def _inverted_serial_fixture(n=3):
    """`_serial_fixture` with the machine NAMES inverted: step 1 runs only on CNC9
    and step 2 only on CNC1, so `sorted(machining_ids)` — the crew genome the
    search starts from — ranks the DOWNSTREAM machine first and the SEED itself is
    infeasible. That is the state in which a Stop press, or a budget too small to
    reach the crew phase, must not be reported as a broken book."""
    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in ("CNC1", "CNC9")}
    masters = Masters(
        machines=machines,
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
                                  [Process(1, "CNC FIRST SIDE", 4.0, None, None, "CNC9"),
                                   Process(2, "CNC SECOND SIDE", 3.0, None, None, "CNC1")])},
        operators=[Operator("Narayan", "", ["CNC1", "CNC9"], "First shift")],
        calendar=WorkCalendar())
    batches = [_B(f"B{i}", 40 + 5 * i, date(2026, 9, 1) + timedelta(days=i))
               for i in range(n)]
    jobs, _by, _sk = build_jobs(batches, masters)
    return jobs, build_shop(masters), _cfg()


def test_the_seed_crew_genome_really_can_be_infeasible():
    """Pins the fixture the three tests above depend on: if `sorted()` ever stopped
    ranking the downstream machine first, they would all pass vacuously."""
    import pytest

    jobs, shop, cfg = _inverted_serial_fixture()
    with pytest.raises(scheduler.Unschedulable):
        scheduler.schedule(jobs, [j.key for j in jobs], shop, cfg, overlap=0.8,
                           crew_rank={mid: i for i, mid
                                      in enumerate(sorted(shop.machining_ids))})


def test_a_crew_genome_the_shop_cannot_run_is_scored_out_not_crashed():
    """Found while measuring: the crew genome can make a plan IMPOSSIBLE, and the
    scheduler rightly fails loud. One such candidate must not take the whole
    contest down with it — it is infinitely bad, not fatal.
    """
    jobs, shop, cfg = _serial_fixture()
    bad = scheduler.schedule
    try:
        bad(jobs, [j.key for j in jobs], shop, cfg, overlap=0.8,
            crew_rank={"CNC2": 0, "CNC1": 1})
    except scheduler.Unschedulable:
        pass
    else:                       # pragma: no cover - the fixture would be vacuous
        raise AssertionError("fixture no longer has an infeasible crew genome")
    res = search.optimize(jobs, shop, cfg, overlap=0.8, budget_evals=40, seed=7)
    assert res.score < float("inf")
    assert res.crew_rank == {"CNC1": 0, "CNC2": 1}


def test_a_book_the_shop_cannot_run_at_all_still_fails_loud():
    """Swallowing an infeasible candidate must not swallow an infeasible BOOK:
    the caller needs the typed error that names the blocking step."""
    import pytest

    masters = Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"ITEM": _routing("ITEM", "CNC1")},
        operators=[Operator("Narayan", "", ["CNC9"], "First shift")],
        calendar=WorkCalendar())
    jobs, _by, _sk = build_jobs([_B("B1", 10, date(2026, 8, 20))], masters)
    with pytest.raises(scheduler.Unschedulable) as caught:
        search.optimize(jobs, build_shop(masters), _cfg(), overlap=0.8,
                        budget_evals=20, seed=7)
    assert caught.value.blocked[0][0] == "B1"


# --------------------------------------------------------------------------- #
# Determinism across processes
# --------------------------------------------------------------------------- #

_HASH_SEED_SCRIPT = textwrap.dedent("""
    from datetime import date, timedelta
    from engine.config import Config
    from engine.models import (Machine, Masters, Operator, Process, Routing,
                               WorkCalendar)
    from roster_engine import search
    from roster_engine.domain import build_jobs, build_shop

    class B:
        def __init__(self, key, qty, due):
            self.batch_id, self.item_code, self.qty = key, "ITEM", qty
            self.so_refs, self.delivery_date = [key], due
            self.process_remaining = None

    machines = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5)
                for m in ("CNC1", "CNC2", "CNC3")}
    masters = Masters(
        machines=machines,
        routings={"ITEM": Routing("ITEM", "d", "", "", None,
            [Process(1, "CNC FIRST SIDE", 10.0, None, None, "CNC1/CNC2/CNC3")])},
        operators=[Operator("Narayan", "", ["CNC1", "CNC2"], "First shift"),
                   Operator("Sidhu", "", ["CNC2", "CNC3"], "First shift")],
        calendar=WorkCalendar())
    batches = [B("B%d" % i, 30 + 20 * i, date(2026, 8, 20) + timedelta(days=i))
               for i in range(6)]
    jobs, _by, _sk = build_jobs(batches, masters)
    res = search.optimize(jobs, build_shop(masters),
                          Config(plan_start_date=date(2026, 8, 12),
                                 setup_time_min=90.0),
                          overlap=0.8, budget_evals=60, seed=7)
    print(res.sequence, sorted(res.crew_rank.items()), round(res.score, 9),
          res.evaluations, round(res.baseline_score, 9))
""")


def test_the_search_is_deterministic_across_hash_seeds():
    """What you Apply is what you get. Within one interpreter a frozenset iterates
    the same way every time, so an unsorted iteration over shop.machining_ids
    passes an in-process check trivially; only a fresh interpreter with a
    different string hash seed re-orders it."""
    outputs = set()
    for seed in ("0", "1", "2", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run([sys.executable, "-c", _HASH_SEED_SCRIPT],
                                cwd=REPO_ROOT, env=env, capture_output=True,
                                text=True, check=True)
        outputs.add(result.stdout)
    assert len(outputs) == 1, f"search varied across PYTHONHASHSEED: {outputs}"
