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
from tests.sample_workbook import build_sample_bytes, ITEM_A

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


# =========================================================================== #
# Task 10b — the optimizer, the cloud contest and the apply path
#
# Everything below pins wiring that FAILS SILENTLY when it is missing: the
# search runs, the panel fills in, the plan looks fine — it is just not the
# plan that will run. Source checks are avoided on purpose (Task 10a proved
# two mutations survived one, because the mutated line's COMMENT still said
# "roster"); every assertion below is behavioural.
# =========================================================================== #

import json as _json
from dataclasses import replace as _replace

from engine import operator_coverage
from engine.models import Order
from roster_engine import search as roster_search


def _spy(monkeypatch, module, name):
    """Record every call's kwargs while still running the real function."""
    seen = []
    real = getattr(module, name)

    def wrapper(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(module, name, wrapper)
    return seen


def _frozen_row(so_lines):
    """One in-progress row in the shape ``engine.freeze.compute_frozen_set`` emits
    (per SO LINE, ISO string start, no order_key) — the shape the adapter has to
    translate. Item A's first step is BANDSAW on BS1."""
    line = next(l for l in so_lines if l.item_code == ITEM_A)
    return {"so_no": line.so_no, "item_code": line.item_code,
            "process": "BANDSAW", "op_seq": 1, "machine": "BS1",
            "operator": "Operator Three", "remaining_qty": 1,
            "prev_start": "2025-03-01T08:00:00"}


# --------------------------------------------------------------------------- #
# FINDING 1 — the contest must search the plan it will actually run
# --------------------------------------------------------------------------- #

def test_the_roster_search_is_given_the_frozen_set(book, monkeypatch):
    """``optimizer.optimize`` forwarded ``frozen`` on the ``"new"`` branch only,
    and the classic fall-through evaluated without it. That was harmless while
    classic and flow IGNORED frozen — but ``roster_adapter.run`` HONOURS it, so a
    roster contest scored every candidate with in-progress work unpinned while the
    applied plan pins it. The ranks would be chosen against a plan nobody runs."""
    so_lines, masters = book
    seen = _spy(monkeypatch, roster_search, "optimize")
    optimizer.optimize(list(so_lines), _cfg("roster", apply_operator_logic=True),
                       _fully_staffed(masters), budget_evals=4, seed=1,
                       frozen=[_frozen_row(so_lines)])
    assert seen, "the roster search was never reached"
    pins = seen[0].get("frozen")
    assert pins, "the frozen set never reached the roster search"
    # ...and it arrived TRANSLATED: batch key, normalised machine, no quantity
    # (a per-SO-line remainder must never reach a batch operation — 2026-08-11).
    assert pins[0]["order_key"] and pins[0]["machine_id"] == "BS1"
    assert "remaining_qty" not in pins[0]


def test_the_roster_sweep_gives_every_candidate_the_frozen_set(book, monkeypatch):
    """``sweep_optimize`` dropped ``frozen`` entirely when it delegated to
    ``_sweep_optimize_classic`` (which has no such parameter). Every overlap
    candidate must pin the same in-progress work, or the winner is picked from
    plans that differ from the one applied."""
    so_lines, masters = book
    seen = _spy(monkeypatch, roster_search, "optimize")
    optimizer.sweep_optimize(list(so_lines), _cfg("roster", apply_operator_logic=True),
                             _fully_staffed(masters), budget_evals=8, seed=1,
                             frozen=[_frozen_row(so_lines)])
    assert len(seen) >= 2, "the sweep searched fewer than two overlap candidates"
    assert all(kw.get("frozen") for kw in seen), \
        "at least one overlap candidate searched with the frozen set dropped"


# --------------------------------------------------------------------------- #
# FINDING 2 — a report may not model a shop the engine does not plan
# --------------------------------------------------------------------------- #

def test_a_single_shift_station_is_reported_on_the_window_roster_plans():
    """``roster_engine.worktime.machine_runs_shift`` runs a single-shift station
    across the WHOLE first shift, 08:00–19:00. ``_day_window`` gave roster the
    legacy manual 09:00–18:00, so Analytics, the delay report, the shift-wise
    export and the Rule-6 machine table each described a shop 2 h/day narrower
    than the one the plan was built on (the 2026-08-07 incident, 158 hours)."""
    roster = operator_coverage._day_window(_cfg("roster"))
    assert roster == (8 * 60, 19 * 60)
    assert roster == operator_coverage._day_window(_cfg("new"))


@pytest.mark.parametrize("sched", ["classic", "flow"])
def test_the_retired_engines_keep_the_manual_window(sched):
    """A golden trace and ~500 tests ride on classic's 09:00–18:00."""
    assert operator_coverage._day_window(_cfg(sched)) == (9 * 60, 18 * 60)


def test_every_planned_minute_on_a_single_shift_station_is_inside_the_reported_window(book):
    """The end-to-end form of the same defect: plan the book on roster, then ask
    the reporting rule where those stations were open. Under the manual window
    every minute the roster engine plans between 08:00–09:00 is invisible."""
    so_lines, masters = book
    masters = _fully_staffed(masters)
    cfg = _cfg("roster", apply_operator_logic=True)
    pr = PlanRun(so_lines=list(so_lines))
    pipeline.run_forward(pr, cfg, masters)
    lo, hi = operator_coverage._day_window(cfg)
    checked = 0
    for e in pr.schedule:
        m = masters.machines.get(e.machine)
        if m is None or m.is_two_shift(cfg.two_shift_threshold_hours):
            continue
        for s, en, _who in (e.op_segments or []):
            checked += 1
            assert lo <= s.hour * 60 + s.minute, (e.machine, s)
            assert en.hour * 60 + en.minute <= hi or (en.hour, en.minute) == (0, 0), (e.machine, en)
    assert checked, "the fixture planned nothing on a single-shift station"


# --------------------------------------------------------------------------- #
# FINDING 3 — the staleness fingerprint
# --------------------------------------------------------------------------- #

def _sig(scheduler, **kw):
    import api.main as main
    return main._inputs_signature(_cfg(scheduler, **kw))


def test_inputs_signature_folds_in_the_roster_fingerprint(monkeypatch):
    """Without it a deploy that changes roster semantics replays ranks searched
    under the old engine behind a green 'up to date' banner."""
    import roster_engine
    before = _sig("roster")
    monkeypatch.setattr(roster_engine, "SCHEDULER_FINGERPRINT", "roster-engine-vX")
    assert _sig("roster") != before


@pytest.mark.parametrize("sched", ["classic", "flow", "new"])
def test_the_roster_fingerprint_does_not_touch_any_other_engine(monkeypatch, sched):
    """Constraint: nothing may change for classic/flow/new. Folding the roster
    fingerprint in UNCONDITIONALLY would move every engine's signature and flag
    the owner's applied optimization stale on a site nobody has switched."""
    import roster_engine
    before = _sig(sched)
    monkeypatch.setattr(roster_engine, "SCHEDULER_FINGERPRINT", "roster-engine-vX")
    assert _sig(sched) == before


def test_an_unset_crew_genome_is_invisible_to_the_inputs_signature():
    """Adding the field must not, by itself, move any signature: an absent and an
    empty crew genome are the same 'not set' and must hash identically."""
    for sched in ("classic", "flow", "new", "roster"):
        base = _sig(sched)
        assert _sig(sched, crew_rank=None) == base
        assert _sig(sched, crew_rank={}) == base


def test_a_real_crew_genome_IS_a_plan_input():
    assert _sig("roster", crew_rank={"CNC1": 0, "CNC2": 1}) != _sig("roster")


# --------------------------------------------------------------------------- #
# 4 + 5 — the adapter's search entry points, and the optimizer's delegation
# --------------------------------------------------------------------------- #

def test_optimize_delegates_to_the_roster_adapter(book, monkeypatch):
    so_lines, masters = book
    seen = _spy(monkeypatch, roster_adapter, "optimize_sequence")
    optimizer.optimize(list(so_lines), _cfg("roster", apply_operator_logic=True),
                       _fully_staffed(masters), budget_evals=4, seed=1)
    assert seen, "optimizer.optimize did not reach roster_adapter.optimize_sequence"


def test_sweep_optimize_delegates_to_the_roster_adapter(book, monkeypatch):
    so_lines, masters = book
    seen = _spy(monkeypatch, roster_adapter, "sweep_optimize")
    optimizer.sweep_optimize(list(so_lines), _cfg("roster", apply_operator_logic=True),
                             _fully_staffed(masters), budget_evals=8, seed=1)
    assert seen, "optimizer.sweep_optimize did not reach roster_adapter.sweep_optimize"


@pytest.mark.parametrize("sched", ["classic", "flow", "new"])
def test_no_other_engine_is_routed_through_the_roster_adapter(book, monkeypatch, sched):
    so_lines, masters = book

    def boom(*a, **kw):
        raise AssertionError("the roster adapter must not see a %r plan" % sched)

    monkeypatch.setattr(roster_adapter, "optimize_sequence", boom)
    monkeypatch.setattr(roster_adapter, "sweep_optimize", boom)
    # The new engine loads its masters from the STORE (isolated per test by
    # conftest). Deliberately not new_engine.set_masters_bytes(): that is a
    # process-global override with no teardown, and leaking it poisons every later
    # test in the session.
    from engine import book_store
    book_store.save_masters_bytes(build_sample_bytes())
    optimizer.optimize(list(so_lines), _cfg(sched), _fully_staffed(masters),
                       budget_evals=2, seed=1)


def test_optimize_sequence_returns_the_apps_own_optimize_result(book):
    """The contest and apply machinery are unchanged, so the roster search has to
    hand back exactly what they already consume — ranks the pipeline can replay
    and a metrics dict ``optimizer.score`` can read."""
    so_lines, masters = book
    res = roster_adapter.optimize_sequence(
        list(so_lines), _cfg("roster", apply_operator_logic=True),
        _fully_staffed(masters), budget_evals=6, seed=1)
    assert isinstance(res, optimizer.OptimizeResult)
    assert res.ranks and all(pipeline.KEY_SEP in k for k in res.ranks)
    assert res.evals > 0
    optimizer.score(res.best)                       # must not KeyError
    assert res.crew_rank, "the winning crew genome was not returned"
    assert sorted(res.crew_rank.values()) == list(range(len(res.crew_rank)))


def test_sweep_optimize_returns_the_apps_own_sweep_result(book):
    so_lines, masters = book
    sw = roster_adapter.sweep_optimize(
        list(so_lines), _cfg("roster", apply_operator_logic=True),
        _fully_staffed(masters), budget_evals=12, seed=1)
    assert isinstance(sw, optimizer.SweepResult)
    # The knob must be a REAL Config field: _optimize_apply does
    # replace(config, **{knob: value}) and a wrong name raises there.
    assert sw.knob == "overlap_percent" and hasattr(Config(), sw.knob)
    assert sw.overlap_percent in set(optimizer.ROSTER_OVERLAP_CANDIDATES) | {50}
    assert sw.flexible_machines is False
    assert sw.result.ranks and sw.crew_rank
    assert sw.table, "the sweep reported no per-candidate table"


def test_the_roster_search_is_deterministic(book):
    so_lines, masters = book
    masters = _fully_staffed(masters)
    cfg = _cfg("roster", apply_operator_logic=True)
    a = roster_adapter.optimize_sequence(list(so_lines), cfg, masters,
                                         budget_evals=8, seed=7)
    b = roster_adapter.optimize_sequence(list(so_lines), cfg, masters,
                                         budget_evals=8, seed=7)
    assert a.ranks == b.ranks and a.crew_rank == b.crew_rank and a.best == b.best


def test_the_winning_metrics_are_measured_on_the_winning_crew(book):
    """"What you Apply is what you get": the reported metrics must come from a
    replay at the winning crew genome, not at whatever crew the config carried in."""
    so_lines, masters = book
    masters = _fully_staffed(masters)
    cfg = _cfg("roster", apply_operator_logic=True)
    res = roster_adapter.optimize_sequence(list(so_lines), cfg, masters,
                                           budget_evals=8, seed=3)
    from engine.rules import rule1_consolidate
    batches = rule1_consolidate.run(list(so_lines), config=cfg, masters=masters)
    ordered, _n = pipeline.apply_priority_rank(batches, res.ranks)
    sched = roster_adapter.run(ordered, config=_replace(cfg, crew_rank=dict(res.crew_rank)),
                               masters=masters)
    assert optimizer.plan_metrics(sched, so_lines, cfg.plan_start_date,
                                  with_distribution=True,
                                  promise_slack_days=3) == res.best


# --------------------------------------------------------------------------- #
# 6 — the crew genome through the contest, and onto the saved plan config
# --------------------------------------------------------------------------- #

def test_the_crew_genome_rides_the_config_through_the_cloud_payload():
    """It must NOT join parse_payload's return tuple — four existing tests unpack
    that positionally. Config already round-trips through the payload, and the
    crew genome is a plan input like the overlap, so that is where it belongs."""
    cfg = _cfg("roster", crew_rank={"CNC1": 0, "CNC2": 1})
    payload = optimize_service.build_payload({}, [], None, cfg, seed=1,
                                             candidates=(50, 80),
                                             budget_per_candidate=5)
    payload = _json.loads(_json.dumps(payload))          # the network hop
    parsed = optimize_service.parse_payload(payload)
    assert len(parsed) == 7, "parse_payload's tuple shape changed"
    assert parsed[3].crew_rank == {"CNC1": 0, "CNC2": 1}


# The Settings operator table a cloud payload carries. It mirrors the workbook's
# own sheet plus the one extra person ``_fully_staffed`` adds, because the crew is
# SPECIALISED on purpose: one operator mans one machine for a whole shift (the
# roster engine's Rule 1), so a table of generalists simply leaves the band saw and
# the inspection bench dark and the book cannot be built at all.
_OPERATOR_TABLE = {"operators": [
    {"id": "1", "name": "Operator One", "machines_raw": "CNC1/CNC2",
     "shift": "First shift"},
    {"id": "2", "name": "Operator Two", "machines_raw": "CNC1",
     "shift": "Second shift"},
    {"id": "3", "name": "Operator Three", "machines_raw": "BS1",
     "shift": "First shift"},
    {"id": "4", "name": "Operator Four", "machines_raw": "MI1/MW1/CNC9",
     "shift": "First shift"}]}


def _roster_payload(per_candidate=4):
    raw = build_sample_bytes()
    so_lines, _masters = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    cfg = _cfg("roster", apply_operator_logic=True)
    return optimize_service.build_payload(
        orders, [], raw, cfg, seed=1,
        candidates=optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES,
        budget_per_candidate=per_candidate, operator_table=_OPERATOR_TABLE)


def test_a_contest_row_carries_the_crew_genome_it_won_with():
    """One shard's row is all the app ever sees of a cloud candidate. Without the
    crew genome on it, the ranks are applied and replayed against a DIFFERENT
    roster — the same 'shown is not applied' gap the 2026-07-25 fix closed."""
    row = optimize_service.run_candidate(_roster_payload(), 80)
    assert row["ranks"]
    assert row["crew_rank"], "the winning crew genome never left run_candidate"


def test_merge_shard_rows_reports_the_winning_crew_genome():
    payload = _roster_payload()
    rows = [optimize_service.run_candidate(payload, ov) for ov in (50, 100)]
    merged = optimize_service.merge_shard_rows(payload, rows, 8, False)
    winner = next(r for r in rows if r["overlap"] == merged["winner_overlap"])
    assert merged["winner_crew_rank"] == winner["crew_rank"]
    # ...and it survives the STRIPPED table the non-sharded worker posts back.
    assert optimize_service.crew_rank_of_winner(
        merged["rows"], merged["winner_overlap"],
        merged["winner_flexible"], merged["best"]) == winner["crew_rank"]


def test_a_non_roster_contest_row_carries_no_crew_genome():
    raw = build_sample_bytes()
    so_lines, _m = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    payload = optimize_service.build_payload(orders, [], raw, _cfg("classic"),
                                             seed=1, candidates=(70, 80),
                                             budget_per_candidate=3)
    assert optimize_service.run_candidate(payload, 80)["crew_rank"] == {}


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _stage_apply(monkeypatch, crew_rank):
    from engine import book_store
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(_json.dumps(_cfg("roster").to_dict()))
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
    monkeypatch.setattr(m, "_incumbent_metrics",
                        lambda: {"total_late_days": 5, "makespan_days": 5.0,
                                 "max_late_days": 1, "max_committed_slip": 0})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {
            "best": {"total_late_days": 1, "makespan_days": 4.0,
                     "max_late_days": 1, "max_committed_slip": 0},
            "ranks": {"k": 1}, "budget": 5, "seed": 1, "baseline": {},
            "best_overlap": 90, "current_overlap": 50, "knob": "overlap_percent",
            "crew_rank": crew_rank}
    return m, book_store


def test_apply_persists_the_winning_crew_genome(monkeypatch):
    """The floor needs a STABLE crew. Without this the ranks replay on a fresh
    roster every plan, so the machine a part is on changes on a page refresh."""
    m, book_store = _stage_apply(monkeypatch, {"CNC1": 1, "CNC2": 0})
    meta = m._optimize_apply()
    assert meta["crew_rank"] == {"CNC1": 1, "CNC2": 0}
    saved = Config.from_dict(_json.loads(book_store.load_plan_config()))
    assert saved.crew_rank == {"CNC1": 1, "CNC2": 0}
    assert saved.overlap_percent == 90        # the existing knob still lands


def test_apply_without_a_crew_genome_leaves_the_saved_config_alone(monkeypatch):
    """Every other engine returns no crew genome; applying one of those plans must
    not write an empty roster over whatever is on file."""
    m, book_store = _stage_apply(monkeypatch, {})
    m._optimize_apply()
    saved = Config.from_dict(_json.loads(book_store.load_plan_config()))
    assert saved.crew_rank is None
