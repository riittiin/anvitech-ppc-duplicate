"""engine/optimizer.py — the Optimize feature's pure sequence search.

Invariants that make the feature trustworthy:
  * deterministic — same book + config + budget + seed → identical result;
  * never worse — the best plan's score is <= the Rule-3 baseline's score;
  * replayable — feeding the returned ranks back through run_forward(priority_rank=)
    reproduces exactly the metrics the search reported (what you Apply is what you get);
  * bounded — it performs at most budget_evals evaluations.
"""
from datetime import date

from engine.config import Config
from engine.models import SOLine, Process, Routing, Machine, WorkCalendar, Masters, PlanRun, Batch
from engine.pipeline import run_forward, KEY_SEP
from engine import optimizer


def _masters(n_items=6):
    ms = {"M": Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                       available_hrs_per_day=19.5),
          "N": Machine(machine_no="N", display_name="N", machine_type="VMC",
                       available_hrs_per_day=19.5)}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    for i in range(n_items):
        it = f"IT{i}"
        # Alternate routings + uneven cycle times so the sequence genuinely matters.
        mm.routings[it] = Routing(item_code=it, description=it, customer="", rm_type="",
                                  moq=None, processes=[
                                      Process(1, "CNC", 3 + 4 * (i % 3), 0, "M", None),
                                      Process(2, "VMC", 2 + 3 * ((i + 1) % 3), 0, "N", None)])
    return mm


def _lines(n_items=6):
    # A mix of tight and loose due dates: EDD/slack is NOT optimal here, so the
    # search has real room to improve.
    return [SOLine(so_no=f"S{i}", item_code=f"IT{i}", item_name=f"IT{i}",
                   qty=40 + 25 * (i % 4),
                   delivery_date=date(2025, 3, 10 + (i * 3) % 9))
            for i in range(n_items)]


def _cfg():
    return Config(plan_start_date=date(2025, 3, 5))


def test_work_excludes_outsourced_turnaround():
    """The ATC seed's 'work' estimate is in-house machine work only. A flat OS
    turnaround block (machine cell 'OS') must not be charged as cycle x qty, or an
    outsourced order looks like an enormous machining job and the seed mis-ranks it."""
    mm = Masters(machines={"M": Machine(machine_no="M", display_name="M",
                                        machine_type="CNC lathe")},
                 calendar=WorkCalendar())
    mm.routings["Z"] = Routing(item_code="Z", description="", customer="", rm_type="",
                               moq=None, processes=[
                                   Process(1, "CNC", 10, 0, "M", "M"),
                                   Process(2, "PAINTING OS", 5000, 0, "OS", "OS")])
    b = Batch(batch_id="Z", item_code="Z", item_name="Z", qty=50,
              so_delivery_date=date(2025, 6, 20), source_so_refs=["Z"])
    assert optimizer._work(b, mm) == 10 * 50  # not (10 + 5000) * 50


def test_deterministic_same_inputs_same_result():
    a = optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=40, seed=7)
    b = optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=40, seed=7)
    assert a.ranks == b.ranks
    assert a.best == b.best and a.baseline == b.baseline
    assert a.evals == b.evals


def test_best_never_worse_than_baseline():
    r = optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=60, seed=3)
    assert optimizer.score(r.best) <= optimizer.score(r.baseline)


def test_respects_eval_budget():
    r = optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=25, seed=1)
    assert r.evals <= 25


def test_ranks_cover_exactly_the_planned_orders():
    lines = _lines()
    r = optimizer.optimize(lines, _cfg(), _masters(), budget_evals=20, seed=1)
    expected = {f"{l.so_no}{KEY_SEP}{l.item_code}" for l in lines}
    assert set(r.ranks) == expected
    assert sorted(set(r.ranks.values())) == list(range(1, len(set(r.ranks.values())) + 1))


def test_replaying_the_ranks_reproduces_the_reported_metrics():
    # THE Apply guarantee: what the search reported is exactly what a Plan with
    # priority_rank= produces.
    lines, cfg, masters = _lines(), _cfg(), _masters()
    r = optimizer.optimize(lines, cfg, masters, budget_evals=60, seed=5)
    pr = PlanRun(so_lines=list(lines))
    run_forward(pr, cfg, masters, priority_rank=r.ranks)
    replay = optimizer.plan_metrics(pr.schedule, lines, cfg.plan_start_date)
    assert replay == r.best


def test_progress_callback_fires():
    seen = []
    optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=30, seed=2,
                       on_progress=lambda evals, best: seen.append((evals, dict(best))))
    assert seen and seen[-1][0] <= 30


def test_empty_book_returns_empty_result():
    r = optimizer.optimize([], _cfg(), _masters(), budget_evals=10, seed=1)
    assert r.ranks == {} and r.evals == 0 and not r.improved


def test_should_cancel_stops_early_and_keeps_best_so_far():
    # Cancel after 3 evaluations: the search stops well short of the 500 budget but
    # still returns a valid best plan (ranks + metrics), flagged cancelled.
    calls = {"n": 0}
    def cancel():
        calls["n"] += 1
        return calls["n"] > 3
    r = optimizer.optimize(_lines(), _cfg(), _masters(), budget_evals=500, seed=1,
                           should_cancel=cancel)
    assert r.cancelled is True
    assert r.evals < 500            # stopped early
    assert r.ranks                  # best-so-far is a usable result
    assert optimizer.score(r.best) <= optimizer.score(r.baseline)


def test_reserved_intervals_are_respected():
    # With machine M fully reserved for a window, the optimized plan's ops on M
    # must not start inside it (Rule 6's reserved semantics carried through).
    from datetime import datetime
    lines, cfg, masters = _lines(3), _cfg(), _masters(3)
    hold = (datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 6, 8, 0))
    r = optimizer.optimize(lines, cfg, masters, budget_evals=15, seed=1,
                           reserved={"M": [hold]})
    assert r.best  # search ran and returned metrics
    pr = PlanRun(so_lines=list(lines))
    run_forward(pr, cfg, masters, priority_rank=r.ranks, reserved={"M": [hold]})
    for e in pr.schedule:
        if e.machine == "M" and e.occupancy_min > 0:
            assert not (hold[0] <= e.start < hold[1])
