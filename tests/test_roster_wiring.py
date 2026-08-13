"""Task 10a — the roster engine becomes SELECTABLE and can produce a plan.

The central hazard this file exists for: a dispatch site that does not know
``"roster"`` does **not** crash. ``getattr(config, "scheduler", "classic")``
falls through every branch and the classic Rule 6 engine runs instead — a
perfectly valid-looking plan, every screen green, the wrong engine. So each
site is pinned twice: by BEHAVIOUR where the value is observable, and by a
source check where the correct behaviour is deliberately "do nothing"
(``optimize_service.run_candidate``), which no behavioural assertion can tell
apart from a forgotten branch.

Left for Task 10b (deliberately NOT asserted here): ``optimizer.optimize`` and
``optimizer.sweep_optimize``, the cloud payload's crew genome, and the apply
path.
"""
import dataclasses
import inspect
import io
from datetime import date

import pytest

from engine import optimize_service, optimizer, pipeline, roster_adapter
from engine.config import Config
from engine.loaders import load_all
from engine.models import Operator, PlanRun
from engine.rules import rule6_allocate
from tests.sample_workbook import build_sample_bytes

WED = date(2025, 3, 1)


def _cfg(scheduler="roster", **kw):
    return Config(plan_start_date=WED, scheduler=scheduler, **kw)


# --------------------------------------------------------------------------- #
# Blocker 1: Config.validate() rejected the value outright
# --------------------------------------------------------------------------- #

def test_config_validate_accepts_roster():
    """Nothing can plan on an engine whose name the config refuses. Before this,
    tests/test_roster_entries_contract.py had to monkeypatch validate away."""
    _cfg("roster").validate()          # must not raise


@pytest.mark.parametrize("sched", ["classic", "flow", "new"])
def test_config_validate_still_accepts_every_other_engine(sched):
    _cfg(sched).validate()


@pytest.mark.parametrize("bad", ["", "Roster", "rooster", "ppc", None, 5])
def test_config_validate_still_rejects_a_bad_scheduler(bad):
    """Widening the whitelist must not turn it into a rubber stamp."""
    with pytest.raises(ValueError):
        _cfg(bad).validate()


# --------------------------------------------------------------------------- #
# Blocker 2: DEFAULT_SCHEDULER could not select the engine
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sched", ["roster", "new", "classic", "flow"])
def test_default_scheduler_env_selects_the_engine(monkeypatch, sched):
    """DEFAULT_SCHEDULER is the deploy-level selector and is authoritative. A
    value missing from its whitelist is silently ignored — the deploy runs the
    saved/​default engine and nothing anywhere says so."""
    import api.main as main
    monkeypatch.setenv("DEFAULT_SCHEDULER", sched)
    assert main._load_plan_config().scheduler == sched


def test_default_scheduler_env_tolerates_whitespace_and_case(monkeypatch):
    import api.main as main
    monkeypatch.setenv("DEFAULT_SCHEDULER", "  ROSTER ")
    assert main._load_plan_config().scheduler == "roster"


def test_a_saved_roster_config_is_not_thrown_away(monkeypatch):
    """_load_plan_config falls back to a bare Config() when the stored config
    fails validation. Before validate() knew 'roster', an admin's saved choice
    was silently downgraded to classic."""
    import json

    import api.main as main
    from engine import book_store
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_plan_config(json.dumps(_cfg("roster").to_dict()))
    assert main._load_plan_config().scheduler == "roster"


# --------------------------------------------------------------------------- #
# The dispatch sites this task wires
# --------------------------------------------------------------------------- #

def test_scheduler_for_dispatches_roster():
    assert pipeline.scheduler_for(_cfg("roster")) is roster_adapter.run


def test_scheduler_for_is_unchanged_for_every_other_engine():
    """Constraint: nothing may move for another engine."""
    from engine import flow_scheduler, new_engine
    assert pipeline.scheduler_for(None) is rule6_allocate.run
    assert pipeline.scheduler_for(_cfg("classic")) is rule6_allocate.run
    assert pipeline.scheduler_for(_cfg("flow")) is flow_scheduler.run
    assert pipeline.scheduler_for(_cfg("new")) is new_engine.run


def test_knob_for_roster_is_the_overlap_percent():
    knob, candidates = optimizer.knob_for(_cfg("roster"))
    assert knob == "overlap_percent"
    assert candidates is optimizer.ROSTER_OVERLAP_CANDIDATES
    # Falling through to the classic lineup is the silent failure mode: same
    # knob NAME, a different (and much narrower) band.
    assert tuple(candidates) != tuple(optimizer.OVERLAP_CANDIDATES)


def test_knob_for_is_unchanged_for_every_other_engine():
    assert optimizer.knob_for(_cfg("flow")) == ("flow_chunks",
                                                optimizer.FLOW_CHUNK_CANDIDATES)
    for sched in ("classic", "new"):
        assert optimizer.knob_for(_cfg(sched)) == ("overlap_percent",
                                                   optimizer.OVERLAP_CANDIDATES)


def test_cloud_candidates_for_roster_is_the_fine_grid():
    got = optimize_service.cloud_candidates(_cfg("roster"))
    assert got is optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES
    assert tuple(got) != tuple(optimize_service.CLOUD_OVERLAP_CANDIDATES)
    assert tuple(got) != tuple(optimize_service.CLOUD_NEW_OVERLAP_CANDIDATES)


def test_cloud_candidates_is_unchanged_for_every_other_engine():
    assert (optimize_service.cloud_candidates(_cfg("flow"))
            is optimize_service.CLOUD_FLOW_CHUNK_CANDIDATES)
    assert (optimize_service.cloud_candidates(_cfg("new"))
            is optimize_service.CLOUD_NEW_OVERLAP_CANDIDATES)
    assert (optimize_service.cloud_candidates(_cfg("classic"))
            is optimize_service.CLOUD_OVERLAP_CANDIDATES)


def test_the_overlap_band_searched_is_50_to_100():
    """The owner's band: 50 = a successor may start once half the pieces have
    cleared, 100 = fully sequential."""
    for candidates in (optimizer.ROSTER_OVERLAP_CANDIDATES,
                       optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES):
        assert candidates, "an empty lineup searches nothing"
        assert min(candidates) >= 50
        assert max(candidates) <= 100
        assert all(isinstance(v, int) for v in candidates)
        assert len(set(candidates)) == len(candidates)
    # The cloud grid is the FINER one over the same band.
    assert (len(optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES)
            > len(optimizer.ROSTER_OVERLAP_CANDIDATES))


def _payload(scheduler):
    return {"config": _cfg(scheduler).to_dict(), "candidates": [70, 80], "seed": 1}


def test_roster_contest_does_not_double_for_machine_sets():
    """flexible_machines is a NEW-engine dimension: roster resolves its machine
    options from the routing and never searches the Allotted/Suggested axis, so
    opening the gate would cost every GitHub Actions run twice for nothing."""
    jobs = optimize_service.contest_jobs(_payload("roster"))
    assert jobs
    assert {flex for _ov, flex, _sd in jobs} == {False}


def test_contest_jobs_still_doubles_for_the_new_engine():
    jobs = optimize_service.contest_jobs(_payload("new"))
    assert {flex for _ov, flex, _sd in jobs} == {False, True}


@pytest.mark.parametrize("sched", ["classic", "flow"])
def test_contest_jobs_is_unchanged_for_the_retired_engines(sched):
    jobs = optimize_service.contest_jobs(_payload(sched))
    assert {flex for _ov, flex, _sd in jobs} == {False}


def test_run_candidate_records_that_roster_needs_no_masters_priming():
    """``run_candidate``'s correct roster behaviour is to do NOTHING — the
    adapter reads the ``Masters`` object ``prepare_contest`` builds, never the
    workbook bytes the new engine needs primed. An omission and a forgotten
    branch are indistinguishable at runtime, so the intent is pinned in the
    source instead."""
    src = inspect.getsource(optimize_service.run_candidate)
    assert "roster" in src


def test_every_site_this_task_wired_still_names_roster():
    """One test that fails if ANY wired site loses the value — the guard against
    a future refactor quietly restoring the classic fallback."""
    for fn in (pipeline.scheduler_for, optimizer.knob_for,
               optimize_service.cloud_candidates, optimize_service.contest_jobs,
               optimize_service.run_candidate, Config.validate):
        assert "roster" in inspect.getsource(fn), fn.__qualname__
    import api.main as main
    assert "roster" in inspect.getsource(main._load_plan_config)


# --------------------------------------------------------------------------- #
# The payoff: a book plans end to end, no monkeypatching
# --------------------------------------------------------------------------- #

def _fully_staffed(masters):
    """The repo's sample workbook staffs only CNC1/CNC2/BS1 — MI1 and the
    provisional CNC9 have nobody. Operators are the SETTINGS table, not the
    workbook sheet (CLAUDE.md), so adding one here is exactly what an admin
    does in Settings, not a fixture fudge."""
    ops = list(masters.operators) + [
        Operator(name="Operator Four", preferred_machines_raw="MI1/MW1/CNC9",
                 machines=["MI1", "MW1", "CNC9"], shift="First shift")]
    return dataclasses.replace(masters, operators=ops)


@pytest.fixture(scope="module")
def book():
    return load_all(io.BytesIO(build_sample_bytes()))


def test_a_book_plans_end_to_end_on_the_roster_engine(book):
    """The proof both blockers are gone: no monkeypatch anywhere in this test."""
    so_lines, masters = book
    cfg = _cfg("roster", apply_operator_logic=True)
    pr = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(pr, cfg, _fully_staffed(masters))
    assert trace["rule6"]["error"] is None
    assert pr.schedule, "the roster engine produced no schedule"
    # It really was the roster engine: only roster_adapter emits these lanes on
    # a plan whose ops all have machines, and only it names an operator on every
    # real-machine entry with at least one op_segment.
    for e in pr.schedule:
        if e.machine not in (roster_adapter.OS_LANE, roster_adapter.OFF_LANE):
            assert e.operator_label, e
            assert e.op_segments


def test_run_forward_really_reaches_the_roster_adapter(book, monkeypatch):
    """The companion to the payoff test above, and the reason it cannot stand
    alone: the classic engine plans that same book to a valid schedule with
    operators and segments on every entry, so a successful plan proves the
    pipeline RAN — never WHICH engine ran. This one counts the calls."""
    calls = []
    real = roster_adapter.run
    monkeypatch.setattr(roster_adapter, "run",
                        lambda *a, **kw: (calls.append(1), real(*a, **kw))[1])
    so_lines, masters = book
    pr = PlanRun(so_lines=list(so_lines))
    pipeline.run_forward(pr, _cfg("roster", apply_operator_logic=True),
                         _fully_staffed(masters))
    assert calls, "run_forward planned WITHOUT the roster adapter"


def test_the_classic_engine_still_plans_the_same_book(book):
    """Guards the constraint that nothing moved for the other engines."""
    so_lines, masters = book
    pr = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(pr, _cfg("classic", apply_operator_logic=True),
                                 _fully_staffed(masters))
    assert trace["rule6"]["error"] is None and pr.schedule


def test_an_unstaffable_step_is_a_typed_rule_error_not_an_exception(book):
    """The sample workbook AS SHIPPED leaves MI1 and CNC9 unstaffed. The adapter
    translates roster_engine's own Unschedulable into a typed RuleError, so
    run_forward records it in the trace and every earlier rule's tab survives —
    instead of a 500 with nothing to show the planner."""
    so_lines, masters = book
    pr = PlanRun(so_lines=list(so_lines))
    trace = pipeline.run_forward(pr, _cfg("roster", apply_operator_logic=True),
                                 masters)
    err = trace["rule6"]["error"]
    assert err and err["rule"] == "rule6"
    assert "roster" in err["message"]
    assert trace["rule1"]["error"] is None and trace["rule1"]["output"]["rows"]
