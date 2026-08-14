"""Task 12 — the CP engine becomes SELECTABLE, at every site that dispatches.

THE HAZARD THIS FILE EXISTS FOR IS SILENT. ``config.scheduler`` defaults to
``"classic"`` and every dispatch site is a ``getattr(config, "scheduler",
"classic")`` chain of ``if``s, so a site that does not know ``"cp"`` does not
raise — it falls through to the classic Rule 6 engine and produces a perfectly
VALID plan. The search then optimises one engine's schedule while the app runs
another's, the applied genome means nothing, and every screen is green.

So the load-bearing test here is not any per-site assertion: it is
``test_no_dispatch_site_ships_without_learning_cp``, which COUNTS the sites. It
walks the AST of the four dispatch modules, finds every function that reads
``config.scheduler`` (or ``DEFAULT_SCHEDULER``), and fails if that set differs
from the registry below — so an eighth branch point added by a future edit
cannot ship untaught. A per-site assertion can only guard the sites somebody
already thought of; this one guards the ones nobody has written yet.

The registry found **fourteen** sites, not the seven the plan's W.1 table names.
The extra seven are real and each is wired below; see the registry's own notes.
"""

import ast
import importlib
import inspect
import io
import json
import pathlib
import sys
from dataclasses import replace
from datetime import date, timedelta

import pytest

from engine import book_store, cp_adapter, optimize_service, optimizer, pipeline
from engine.config import Config
from engine.loaders import load_all
from engine.models import (Machine, Masters, Operator, Order, PlanRun, Process,
                           Routing, WorkCalendar)
from engine.rules import rule6_allocate
from tests.sample_workbook import ITEM_A, build_sample_bytes

REPO = pathlib.Path(__file__).resolve().parents[1]
DAY = date(2026, 8, 12)


def _cfg(scheduler="cp", **kw):
    return Config(plan_start_date=DAY, scheduler=scheduler, **kw)


# =========================================================================== #
# THE SITE COUNT — the test this task is really for
# =========================================================================== #

# Every function in the app that branches on which engine is running. The VALUE
# says how it handles "cp"; the KEY is what the scanner must find, and nothing
# else. Adding a site without adding it here fails; adding it here without
# teaching it "cp" fails on the second assertion.
DISPATCH_SITES = {
    # --- the plan's W.1 seven ------------------------------------------------
    ("engine/pipeline.py", "scheduler_for"):      "cp -> cp_adapter.run (REPLAY)",
    ("engine/optimizer.py", "optimize"):          "cp -> cp_adapter.solve",
    ("engine/optimizer.py", "knob_for"):          "cp -> (None, ()) — no knob",
    ("engine/optimizer.py", "sweep_optimize"):    "cp -> cp_adapter.sweep_optimize",
    ("engine/optimize_service.py", "cloud_candidates"): "cp -> (None,) — one job",
    ("engine/optimize_service.py", "run_candidate"):    "cp -> no masters priming",
    ("engine/optimize_service.py", "contest_jobs"):     "cp -> (False,) machine sets",
    # --- NINE MORE the W.1 table does not list, each a real fall-through -----
    # A CP solve is time-boxed, so a per-candidate PLAN budget is meaningless and
    # 400 would size a progress bar against a denominator that can never fill.
    ("engine/optimize_service.py", "cloud_budget"): "cp -> 1 (time-boxed, not eval-boxed)",
    # "better" is per engine: score is symmetric, cp's own objective is late-days.
    ("engine/optimizer.py", "apply_key"):  "cp -> (late-days, spread); else score",
    ("api/main.py", "_inputs_signature"):  "cp fingerprint + clock, genome popped",
    ("api/main.py", "_report_for_book"):   "cp -> plan_violations, breaches only",
    ("api/main.py", "_load_plan_config"):  "DEFAULT_SCHEDULER=cp is accepted",
    ("api/main.py", "_local_search_budget"): "cp -> 0; evals count IMPROVEMENTS",
    ("api/main.py", "_resolve_config"):    "cp -> attach the stored genome",
    ("api/main.py", "_start_optimize"):    "cp -> caps do not apply; no knob",
    # POST /run pins the engine from _load_plan_config and validates the merged
    # config — it needs no branch, but validate() must accept "cp" or an admin's
    # Save 400s. Listed so the scanner's set is exact.
    ("api/main.py", "run"):                "no branch needed; validate() accepts cp",
}

# Everything that asks ``optimizer.knob_for`` for the tuned setting. It returns
# ``(None, ())`` under cp, and BOTH ``getattr(config, None)`` and
# ``replace(config, **{None: v})`` raise TypeError — in production, on the first
# deep search after the cutover, which no plan-time test would reach. These are
# not scheduler-branching (they route the question through ``knob_value`` /
# ``if knob:``), so the scanner above cannot see them; they are pinned here so a
# new consumer cannot be added without meeting the None case.
KNOB_CONSUMERS = {
    ("engine/optimize_service.py", "run_candidate"),
    ("engine/optimize_service.py", "merge_shard_rows"),
    ("engine/optimizer.py", "knob_value"),
    ("engine/optimizer.py", "_sweep_optimize_classic"),   # never reached under cp
    ("api/main.py", "_local_search_budget"),
    ("api/main.py", "_metrics_for_ranks"),
    ("api/main.py", "_finalize_optimize"),
    ("api/main.py", "_optimize_apply"),
}

_ENGINE_NAMES = {"classic", "flow", "new", "roster", "cp"}


def _scan(rel):
    """Every function in ``rel`` that reads which engine is selected."""
    text = (REPO / rel).read_text()
    tree = ast.parse(text)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src = ast.get_source_segment(text, node) or ""
        reads = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "scheduler":
                reads = True
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                  and sub.func.id == "getattr" and len(sub.args) >= 2
                  and isinstance(sub.args[1], ast.Constant)
                  and sub.args[1].value == "scheduler"):
                reads = True
            elif isinstance(sub, ast.Constant) and sub.value == "DEFAULT_SCHEDULER":
                reads = True
        if reads:
            out[(rel, node.name)] = src
    return out


def _all_sites():
    found = {}
    for rel in ("engine/pipeline.py", "engine/optimizer.py",
                "engine/optimize_service.py", "api/main.py"):
        found.update(_scan(rel))
    return found


def test_no_dispatch_site_ships_without_learning_cp():
    """THE test. A missed site does not error — it silently plans on the classic
    Rule 6 engine, which is why counting the sites is worth more than asserting
    on any one of them."""
    found = _all_sites()
    missing = sorted(set(DISPATCH_SITES) - set(found))
    extra = sorted(set(found) - set(DISPATCH_SITES))
    assert not extra, (
        "a NEW site branches on the selected engine and is not in this file's "
        "registry — teach it \"cp\" and list it here: %s" % (extra,))
    assert not missing, ("a registered dispatch site vanished (renamed? "
                         "inlined?): %s" % (missing,))
    # ...and every site that branches on an engine NAME must name "cp". A site
    # that merely READS config.scheduler without comparing it (POST /run pins the
    # value; _resolve_config/_metrics_for_ranks/etc. compare, so they are here)
    # needs no literal.
    for key, src in sorted(found.items()):
        branches = any(f'"{name}"' in src or f"'{name}'" in src
                       for name in _ENGINE_NAMES - {"cp"})
        if branches:
            assert '"cp"' in src or "'cp'" in src, (
                "%s::%s branches on the engine name but has never heard of "
                "\"cp\" — it will silently fall through to classic" % key)


def _knob_consumers():
    """Every function that calls ``optimizer.knob_for``."""
    out = set()
    for rel in ("engine/optimizer.py", "engine/optimize_service.py", "api/main.py"):
        text = (REPO / rel).read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "knob_for"):
                    out.add((rel, node.name))
                elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                      and sub.func.id == "knob_for"):
                    out.add((rel, node.name))
    return out


def test_no_knob_consumer_ships_without_meeting_the_no_knob_case():
    """The second silent class. `knob_for` returns (None, ()) under cp, and both
    `getattr(config, None)` and `replace(config, **{None: v})` raise — in
    production, on the first deep search after the cutover."""
    found = _knob_consumers()
    assert found == KNOB_CONSUMERS, (
        "the set of optimizer.knob_for consumers changed; a new one must handle "
        "knob=None (use optimizer.knob_value, or guard with `if knob:`). "
        "added=%s removed=%s" % (sorted(found - KNOB_CONSUMERS),
                                 sorted(KNOB_CONSUMERS - found)))


def test_the_registry_is_not_empty_and_covers_the_plans_seven():
    """Non-vacuity: a scanner that finds nothing would pass the test above."""
    assert len(DISPATCH_SITES) >= 16
    assert len(_all_sites()) >= 16
    for key in (("engine/pipeline.py", "scheduler_for"),
                ("engine/optimizer.py", "optimize"),
                ("engine/optimizer.py", "knob_for"),
                ("engine/optimizer.py", "sweep_optimize"),
                ("engine/optimize_service.py", "cloud_candidates"),
                ("engine/optimize_service.py", "run_candidate"),
                ("engine/optimize_service.py", "contest_jobs")):
        assert key in DISPATCH_SITES and key in _all_sites()


# =========================================================================== #
# Blocker 1: Config.validate() rejected "cp" outright
# =========================================================================== #

def test_config_validate_accepts_cp():
    """`_load_plan_config` falls back to a bare Config() when the stored config
    fails validation, so an admin's saved choice was silently DOWNGRADED to
    classic — this task's own failure class, in the config layer."""
    _cfg("cp").validate()


@pytest.mark.parametrize("sched", ["classic", "flow", "new", "roster"])
def test_config_validate_still_accepts_every_other_engine(sched):
    _cfg(sched).validate()


@pytest.mark.parametrize("bad", ["", "CP", "cpp", "ppc", None, 5])
def test_config_validate_still_rejects_a_bad_scheduler(bad):
    with pytest.raises(ValueError):
        _cfg(bad).validate()


def test_a_saved_cp_config_is_not_downgraded(monkeypatch):
    import api.main as main
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    assert main._load_plan_config().scheduler == "cp"


def test_default_scheduler_env_selects_cp(monkeypatch):
    import api.main as main
    monkeypatch.setenv("DEFAULT_SCHEDULER", " CP ")
    assert main._load_plan_config().scheduler == "cp"


def test_the_config_knobs_round_trip():
    cfg = _cfg(cp_fairness_slack_days=2, cp_hold_across_unmanned_shift=True,
               cp_time_limit_sec=120)
    got = Config.from_dict(cfg.to_dict())
    assert got.cp_fairness_slack_days == 2
    assert got.cp_hold_across_unmanned_shift is True
    assert got.cp_time_limit_sec == 120
    got.validate()


def test_the_shipping_default_is_E1():
    """`cp_hold_across_unmanned_shift = False` (E1) is the measured shipping
    default and a deliberate reversal of the provisional E2 one: at the owner's
    scale E2 returns UNKNOWN — no plan at all — from 30 batches upward."""
    assert Config().cp_hold_across_unmanned_shift is False


@pytest.mark.parametrize("bad", [{"cp_time_limit_sec": 0},
                                 {"cp_fairness_slack_days": -1},
                                 {"cp_hold_across_unmanned_shift": "yes"}])
def test_the_new_knobs_are_validated(bad):
    with pytest.raises(ValueError):
        _cfg(**bad).validate()


# =========================================================================== #
# Sites 1-4: pipeline + optimizer
# =========================================================================== #

def test_scheduler_for_dispatches_cp_to_the_REPLAY(monkeypatch):
    """`run`, never `solve`. Wire this to a solve and every page load starts a
    CP solve on a 0.5-CPU free instance."""
    assert pipeline.scheduler_for(_cfg("cp")) is cp_adapter.run
    assert pipeline.scheduler_for(_cfg("cp")) is not cp_adapter.solve


def test_scheduler_for_is_unchanged_for_every_other_engine():
    from engine import flow_scheduler, new_engine, roster_adapter
    assert pipeline.scheduler_for(None) is rule6_allocate.run
    assert pipeline.scheduler_for(_cfg("classic")) is rule6_allocate.run
    assert pipeline.scheduler_for(_cfg("flow")) is flow_scheduler.run
    assert pipeline.scheduler_for(_cfg("new")) is new_engine.run
    assert pipeline.scheduler_for(_cfg("roster")) is roster_adapter.run


def test_knob_for_cp_has_no_knob():
    """Overlap is a MODEL VARIABLE under this engine, picked per job by the
    solver. Falling through to ('overlap_percent', OVERLAP_CANDIDATES) would
    re-solve the same book four times to answer a question the model already
    answered, at four times the worker's wall clock."""
    assert optimizer.knob_for(_cfg("cp")) == (None, ())


def test_knob_for_is_unchanged_for_every_other_engine():
    assert optimizer.knob_for(_cfg("flow")) == ("flow_chunks",
                                                optimizer.FLOW_CHUNK_CANDIDATES)
    assert optimizer.knob_for(_cfg("roster")) == ("overlap_percent",
                                                  optimizer.ROSTER_OVERLAP_CANDIDATES)
    for sched in ("classic", "new"):
        assert optimizer.knob_for(_cfg(sched)) == ("overlap_percent",
                                                   optimizer.OVERLAP_CANDIDATES)


def test_knob_value_survives_a_mode_with_no_knob():
    """Six call sites do `getattr(config, knob)` or `replace(cfg, **{knob: v})`.
    With knob None both raise TypeError, so "no knob" needs its own expression
    rather than a None nobody checked."""
    assert optimizer.knob_value(_cfg("cp")) is None
    assert optimizer.knob_value(_cfg("classic", overlap_percent=70)) == 70
    assert optimizer.knob_value(_cfg("flow", flow_chunks=6)) == 6


def _spy(monkeypatch, module, name):
    seen = []
    real = getattr(module, name)

    def wrapper(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(module, name, wrapper)
    return seen


@pytest.fixture(scope="module")
def book():
    return load_all(io.BytesIO(build_sample_bytes()))


def test_optimize_delegates_to_the_cp_solve(book, monkeypatch):
    so_lines, masters = book
    calls = []
    monkeypatch.setattr(cp_adapter, "solve",
                        lambda *a, **kw: calls.append(kw) or optimizer.OptimizeResult())
    optimizer.optimize(list(so_lines), _cfg("cp"), masters, budget_evals=2, seed=1,
                       frozen=[{"so_no": "SO1"}])
    assert calls, "optimizer.optimize did not reach cp_adapter.solve"
    assert calls[0].get("frozen"), "the frozen set never reached the CP solve"


def test_sweep_optimize_delegates_to_the_cp_adapter(book, monkeypatch):
    so_lines, masters = book
    calls = []
    monkeypatch.setattr(cp_adapter, "sweep_optimize",
                        lambda *a, **kw: calls.append(kw) or optimizer.SweepResult())
    optimizer.sweep_optimize(list(so_lines), _cfg("cp"), masters, budget_evals=2,
                             seed=1, frozen=[{"so_no": "SO1"}])
    assert calls, "optimizer.sweep_optimize did not reach cp_adapter.sweep_optimize"
    assert calls[0].get("frozen"), "the frozen set never reached the CP sweep"


def _fully_staffed(masters):
    """The repo's sample workbook staffs only CNC1/CNC2/BS1; MI1 and the
    provisional CNC9 have nobody, and the roster engine refuses a book it cannot
    crew. Operators ARE the Settings table (CLAUDE.md), so adding one here is
    what an admin does, not a fixture fudge."""
    ops = list(masters.operators) + [
        Operator(name="Operator Four", preferred_machines_raw="MI1/MW1/CNC9",
                 machines=["MI1", "MW1", "CNC9"], shift="First shift")]
    return replace(masters, operators=ops)


@pytest.mark.parametrize("sched", ["classic", "flow", "new", "roster"])
def test_no_other_engine_is_routed_through_the_cp_adapter(book, monkeypatch, sched):
    so_lines, masters = book

    def boom(*a, **kw):
        raise AssertionError("the CP adapter must not see a %r plan" % sched)

    monkeypatch.setattr(cp_adapter, "solve", boom)
    monkeypatch.setattr(cp_adapter, "sweep_optimize", boom)
    book_store.save_masters_bytes(build_sample_bytes())
    optimizer.optimize(list(so_lines), _cfg(sched, apply_operator_logic=True),
                       _fully_staffed(masters), budget_evals=2, seed=1)


# =========================================================================== #
# Sites 5-7 + the cloud path
# =========================================================================== #

def test_cloud_candidates_for_cp_is_one_job():
    assert optimize_service.cloud_candidates(_cfg("cp")) == (None,)


def test_cloud_candidates_is_unchanged_for_every_other_engine():
    assert (optimize_service.cloud_candidates(_cfg("flow"))
            is optimize_service.CLOUD_FLOW_CHUNK_CANDIDATES)
    assert (optimize_service.cloud_candidates(_cfg("new"))
            is optimize_service.CLOUD_NEW_OVERLAP_CANDIDATES)
    assert (optimize_service.cloud_candidates(_cfg("roster"))
            is optimize_service.CLOUD_ROSTER_OVERLAP_CANDIDATES)
    assert (optimize_service.cloud_candidates(_cfg("classic"))
            is optimize_service.CLOUD_OVERLAP_CANDIDATES)


def _payload(scheduler, candidates=None):
    return {"config": _cfg(scheduler).to_dict(),
            "candidates": list(optimize_service.cloud_candidates(_cfg(scheduler))
                               if candidates is None else candidates),
            "seed": 1}


def test_the_cp_contest_is_exactly_one_job():
    """Machine choice is a model variable, so doubling the contest for the
    Allotted/Suggested axis buys nothing and costs two rounds of the workflow's
    20 shards."""
    jobs = optimize_service.contest_jobs(_payload("cp"))
    assert jobs == [(None, False, 1)]


def test_contest_jobs_still_doubles_for_new_and_roster():
    for sched in ("new", "roster"):
        jobs = optimize_service.contest_jobs(_payload(sched, [70, 80]))
        assert {flex for _ov, flex, _sd in jobs} == {False, True}, sched


@pytest.mark.parametrize("sched", ["classic", "flow"])
def test_contest_jobs_is_unchanged_for_the_retired_engines(sched):
    jobs = optimize_service.contest_jobs(_payload(sched, [70, 80]))
    assert {flex for _ov, flex, _sd in jobs} == {False}


def test_cloud_budget_under_cp_is_not_a_plan_count():
    """A CP solve is TIME-boxed. `budget_evals` is ignored by the adapter and
    `evals` counts IMPROVED SOLUTIONS, so a 400-plan denominator would render a
    progress bar reading "3 of 400" that can never fill."""
    assert optimize_service.cloud_budget(_cfg("cp")) == 1
    assert optimize_service.cloud_budget(_cfg("classic")) == \
        optimize_service.CLOUD_BUDGET_PER_CANDIDATE
    assert optimize_service.cloud_budget(_cfg("new")) == \
        optimize_service.CLOUD_NEW_BUDGET_PER_CANDIDATE


def test_the_local_search_budget_is_indeterminate_under_cp():
    """The progress bar's denominator. Under cp there is no denominator: one
    time-boxed solve, and `evals` counts IMPROVED SOLUTIONS. `knob_for` returns
    None here, so the un-guarded form (`getattr(config, None)`) raises instead."""
    import api.main as main
    assert main._local_search_budget(_cfg("cp"), 1000) == 0
    classic = main._local_search_budget(_cfg("classic", overlap_percent=70), 1000)
    assert classic > 0
    # the new engine's contest also sweeps the machine-set axis: twice the count
    assert main._local_search_budget(_cfg("new", overlap_percent=70), 1000) == 2 * classic


def test_a_cp_deep_search_is_not_given_a_plan_budget(monkeypatch):
    """End of the wire for the same rule: `_start_optimize` must not hand the CP
    search a fictional eval budget, and must not size the panel against one."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2026, 9, 20))])
    monkeypatch.setattr(m, "_incumbent_metrics", lambda **kw: {
        "total_late_days": 5, "makespan_days": 5.0, "max_late_days": 1,
        "max_committed_slip": 0, "slip_severity": 1.0, "ontime_breach": 1.0})
    seen = []
    monkeypatch.setattr(cp_adapter, "sweep_optimize",
                        lambda *a, **kw: seen.append(kw) or optimizer.SweepResult())
    m._start_optimize(1000, "deep", background=False)
    assert seen, "the CP sweep was never reached"
    assert seen[0]["budget_evals"] == 0, (
        "a plan budget was invented for a time-boxed solve: %r"
        % seen[0]["budget_evals"])
    with m._OPTIMIZE_LOCK:
        assert m._OPTIMIZE["budget_evals"] == 0


def test_run_candidate_records_that_cp_needs_no_masters_priming():
    """The correct cp behaviour here is to do NOTHING — the adapter reads the
    Masters OBJECT prepare_contest builds, never the workbook bytes. An omission
    and a forgotten branch are indistinguishable at runtime, so the intent is
    pinned in the source."""
    src = inspect.getsource(optimize_service.run_candidate)
    assert '"cp"' in src


def _cp_cloud_payload():
    raw = build_sample_bytes()
    so_lines, _m = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    return optimize_service.build_payload(
        orders, [], raw, _cfg("cp"), seed=1,
        candidates=optimize_service.cloud_candidates(_cfg("cp")),
        budget_per_candidate=optimize_service.cloud_budget(_cfg("cp")))


def test_a_cp_contest_row_survives_a_None_overlap(monkeypatch):
    """`run_candidate` does `replace(cfg, **{knob: int(overlap)})`. Under cp the
    knob is None and the overlap is None, and BOTH raise — so a cloud contest
    would die at the first candidate."""
    payload = _cp_cloud_payload()
    monkeypatch.setattr(cp_adapter, "solve",
                        lambda *a, **kw: optimizer.OptimizeResult(
                            ranks={"k": 1}, best={"total_late_days": 3},
                            genome={"ranks": {"k": 1}}, evals=1))
    row = optimize_service.run_candidate(payload, None)
    assert row["overlap"] is None and row["eligible"]
    assert row["genome"], "the CP genome never left run_candidate"


def test_merge_shard_rows_brings_the_cp_genome_home(monkeypatch):
    """A row is ALL the app ever sees of a cloud candidate. Without the genome on
    it the ranks are applied and then replayed with no genome at all — the
    decoder falls back for every operation and the plan is one nobody searched."""
    payload = _cp_cloud_payload()
    monkeypatch.setattr(cp_adapter, "solve",
                        lambda *a, **kw: optimizer.OptimizeResult(
                            ranks={"k": 1}, best={"total_late_days": 3},
                            genome={"ranks": {"k": 1}, "cp_overlap_of": {"B1": 5}},
                            evals=1))
    rows = [optimize_service.run_candidate(payload, None)]
    merged = optimize_service.merge_shard_rows(payload, rows, 1, False)
    assert merged["winner_overlap"] is None
    assert merged["winner_genome"] == {"ranks": {"k": 1},
                                       "cp_overlap_of": {"B1": 5}}
    # ...and it survives the STRIPPED table, which is the ONLY thing the
    # non-sharded cloud worker posts back (scripts/ is deliberately untouched, so
    # no top-level genome field makes that hop). `genome_of_winner` reading an
    # empty table is not an error anywhere — `_optimize_apply` simply never
    # overwrites the stored genome — so the plan would be applied with the
    # PREVIOUS decisions and nothing would say so.
    assert optimize_service.genome_of_winner(
        merged["rows"], merged["winner_overlap"],
        merged["winner_flexible"], merged["best"]) == merged["winner_genome"]


def test_a_non_cp_contest_row_carries_no_genome():
    raw = build_sample_bytes()
    so_lines, _m = load_all(io.BytesIO(raw))
    orders = {}
    for so in so_lines:
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    payload = optimize_service.build_payload(orders, [], raw, _cfg("classic"),
                                             seed=1, candidates=(70, 80),
                                             budget_per_candidate=3)
    assert optimize_service.run_candidate(payload, 80)["genome"] == {}


# =========================================================================== #
# The genome's store key
# =========================================================================== #

def test_the_genome_store_key_round_trips():
    book_store.save_cp_genome({"ranks": {"a\x1fb": 0},
                               "cp_machine_of": {("B1", 1): "CNC1"},
                               "cp_overlap_of": {"B1": 5}})
    got = book_store.load_cp_genome()
    assert got["cp_overlap_of"] == {"B1": 5}
    # ...flattened for JSON, exactly as cp_engine.genome.to_json writes it.
    assert got["cp_machine_of"] == {"B1\x1f1": "CNC1"}
    book_store.clear_cp_genome()
    assert book_store.load_cp_genome() in (None, {})


def test_saving_an_ALREADY_FLATTENED_genome_does_not_re_flatten_it():
    """`genome.to_json` is NOT idempotent: fed a flattened genome it builds each
    key from the FIRST TWO CHARACTERS of the string. A cloud worker's genome
    arrives already flattened (it came home over JSON), so a blind re-flatten
    turns {"CNC1\\x1f1": ...} into {"C\\x1fN": ...} — not an error, just a genome
    that misses on every lookup while the plan still looks well-formed."""
    flat = {"ranks": {"a\x1fb": 0}, "cp_machine_of": {"CNC1\x1f1": "CNC1"}}
    book_store.save_cp_genome(flat)
    assert book_store.load_cp_genome()["cp_machine_of"] == {"CNC1\x1f1": "CNC1"}


def test_the_genome_key_is_its_own():
    """W.5 rollback: DEFAULT_SCHEDULER back to roster, and no migration to undo.
    The genome must not live in the saved plan config, which every engine reads."""
    assert book_store.CP_GENOME_KEY == "anvitech:cp_genome"
    assert book_store.CP_GENOME_KEY not in (book_store.PLAN_CONFIG_KEY,
                                            book_store.PLAN_PRIORITY_KEY)


# =========================================================================== #
# The staleness fingerprint
# =========================================================================== #

def _sig(scheduler, **kw):
    import api.main as main
    return main._inputs_signature(_cfg(scheduler, **kw))


def test_inputs_signature_folds_in_the_cp_fingerprint(monkeypatch):
    import cp_engine
    before = _sig("cp")
    monkeypatch.setattr(cp_engine, "SCHEDULER_FINGERPRINT", "cp-engine-vX")
    assert _sig("cp") != before


@pytest.mark.parametrize("sched", ["classic", "flow", "new", "roster"])
def test_the_cp_fingerprint_does_not_touch_any_other_engine(monkeypatch, sched):
    """Folding it in unconditionally would move every classic/flow/new/roster
    signature the moment this shipped and instantly flag the owner's applied
    optimization stale with a "re-run the deep search" banner on a site nobody
    has switched engines on."""
    import cp_engine
    before = _sig(sched)
    monkeypatch.setattr(cp_engine, "SCHEDULER_FINGERPRINT", "cp-engine-vX")
    assert _sig(sched) == before


def test_genome_is_not_an_input_signature():
    """The genome is an optimization OUTPUT. Leave it in and every apply
    instantly flags its own result stale."""
    for sched in ("cp", "classic", "roster"):
        bare = _sig(sched)
        assert _sig(sched, cp_genome={"ranks": {"a": 1}}) == bare
        assert _sig(sched, cp_genome=None) == bare


def test_a_moved_plan_clock_invalidates_the_genome(monkeypatch):
    """Storing ABSOLUTE datetimes in the genome does NOT protect it: the shift
    indices in `cp_roster`, the decoder's calendar rebuild and its floor clamp
    are ALL counted from plan_start, so the same genome under a clock moved one
    day slides every completion by a day and reorders jobs. Measured, not
    assumed (progress ledger, Task 8 N3)."""
    import api.main as main
    monkeypatch.setattr(main, "_current_plan_start_sig", lambda: "2026-08-12")
    a_cp, a_other = _sig("cp"), _sig("roster")
    monkeypatch.setattr(main, "_current_plan_start_sig", lambda: "2026-08-13")
    assert _sig("cp") != a_cp, "a moved plan clock left the CP signature alone"
    # ...and only under cp: every other engine replays a rank map, which a moved
    # clock does not invalidate, and it has its own plan_start marker already.
    assert _sig("roster") == a_other


def test_plan_cache_key_changes_with_scheduler():
    assert _sig("cp") != _sig("roster") != _sig("classic")
    assert _sig("cp") != _sig("classic")


# =========================================================================== #
# The genome on the plan path
# =========================================================================== #

def _api():
    import api.main as m
    importlib.reload(m)
    return m


def test_resolve_config_attaches_the_stored_genome(monkeypatch):
    """`/run` has no genome argument — it rides on the resolved config, exactly
    as the roster engine's crew genome does."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_cp_genome({"cp_overlap_of": {"B1": 5}})
    assert m._resolve_config(_cfg("cp")).cp_genome == {"cp_overlap_of": {"B1": 5}}
    # No other engine may pick it up, or a stale CP artifact would ride into a
    # roster/classic plan's fingerprint.
    assert m._resolve_config(_cfg("roster")).cp_genome is None


def test_the_plan_cache_busts_when_the_genome_changes(monkeypatch):
    """`_plan_fingerprint` hashes the RESOLVED config, so attaching the genome
    there is what makes an apply visible on the next page load."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_cp_genome({"cp_overlap_of": {"B1": 5}})
    before = m._plan_fingerprint(_cfg("cp"))
    book_store.save_cp_genome({"cp_overlap_of": {"B1": 9}})
    assert m._plan_fingerprint(_cfg("cp")) != before


def _stage_apply(monkeypatch, scheduler="cp", genome=None, best=None, inc=None):
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg(scheduler).to_dict()))
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2026, 9, 20))])
    monkeypatch.setattr(m, "_incumbent_metrics", lambda **kw: dict(
        inc or {"total_late_days": 5, "makespan_days": 5.0, "max_late_days": 1,
                "max_committed_slip": 0, "slip_severity": 9.0,
                "ontime_breach": 9.0}))
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {
            "best": dict(best or {"total_late_days": 1, "makespan_days": 4.0,
                                  "max_late_days": 1, "max_committed_slip": 0,
                                  "slip_severity": 1.0, "ontime_breach": 1.0}),
            "ranks": {"k": 1}, "budget": 5, "seed": 1, "baseline": {},
            "best_overlap": None, "current_overlap": None, "knob": None,
            "crew_rank": {}, "genome": genome if genome is not None else {}}
    return m


def test_apply_persists_the_genome(monkeypatch):
    """Persist the ranks without the genome and every later page load replays a
    job order with no machine, crew or overlap decisions behind it — the decoder
    falls back per operation and the plan is not the one that was searched."""
    m = _stage_apply(monkeypatch, genome={"ranks": {"k": 1},
                                          "cp_machine_of": {("B1", 1): "CNC1"}})
    m._optimize_apply()
    assert book_store.load_cp_genome()["cp_machine_of"] == {"B1\x1f1": "CNC1"}


def test_applying_a_plan_with_no_genome_leaves_the_stored_one_alone(monkeypatch):
    """Every other engine returns no genome; applying one of those must not wipe
    the CP genome on file."""
    book_store.save_cp_genome({"cp_overlap_of": {"B1": 5}})
    m = _stage_apply(monkeypatch, scheduler="roster", genome={})
    m._optimize_apply()
    assert book_store.load_cp_genome()["cp_overlap_of"] == {"B1": 5}


def test_finalize_survives_a_mode_with_no_knob(monkeypatch):
    """`_finalize_optimize` did `replace(cfg, **{_knob: winner_overlap})` and
    `getattr(base_config, _knob)`; both raise TypeError on the None knob. It is the
    LAST step of every contest, so without the guard a CP deep search would run to
    completion on the worker and then die on the way to the panel — after the
    minutes were already spent."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    import time as _time
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="J1", started_mono=_time.monotonic(),
                           state="running")
    stored = m._finalize_optimize(
        "J1", _cfg("cp"), None, "deep",
        winner_overlap=None, winner_flexible=False, ranks={"k": 1},
        best={"total_late_days": 1, "makespan_days": 1.0},
        evals=3, table=[], cancelled=False,
        genome={"cp_overlap_of": {"B1": 5}})
    assert stored is True
    with m._OPTIMIZE_LOCK:
        res = m._OPTIMIZE["result"]
    assert res["knob"] is None and res["best_overlap"] is None
    assert res["current_overlap"] is None
    # ...and the genome reached the result, so Apply has something to persist.
    assert res["genome"] == {"cp_overlap_of": {"B1": 5}}


def test_the_panel_measures_the_winner_at_the_WINNING_genome(monkeypatch):
    """"What you Apply is what you get". Apply has not persisted the winning
    genome when `_finalize_optimize` recomputes the winner's metrics, so
    `_resolve_config` attaches the PREVIOUS one — and the panel would publish a
    number produced by replaying the new ranks against the old decisions, a plan
    the apply does not reproduce (the 2026-07-25 52.5-vs-55.6 gap, in a new
    field). Same fix, same shape, as `crew_rank`."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2026, 9, 20))])
    book_store.save_cp_genome({"cp_overlap_of": {"OLD": 1}})
    seen = []

    def _capture(setup, masters, ranks):
        seen.append(setup.config.cp_genome)
        return [], list(setup.target)

    monkeypatch.setattr(m, "_all_lines_schedule", _capture)
    m._metrics_for_ranks({"k": 1}, None, None,
                         cp_genome={"cp_overlap_of": {"NEW": 2}})
    assert seen, "_metrics_for_ranks never reached the schedule"
    assert seen[0] == {"cp_overlap_of": {"NEW": 2}}, (
        "the winner was measured against the PREVIOUSLY stored genome")
    # ...and with no winning genome the stored one is still what gets measured —
    # that is the incumbent, and it must not silently lose its decisions.
    seen.clear()
    m._metrics_for_ranks({"k": 1}, None, None)
    assert seen[0] == {"cp_overlap_of": {"OLD": 1}}


def test_the_movement_note_compares_two_REAL_plans(monkeypatch):
    """The note the owner reads — "N orders now finish later" — is built by
    replaying the OLD ranks and the NEW ranks. Under cp the ranks are only the job
    order; without the winning genome the "after" side is the new ranks against
    the OLD decisions, so the note names orders that are not moving and misses
    ones that are. The "before" side keeps the stored genome deliberately: that IS
    the plan the floor has now."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2026, 9, 20))])
    book_store.save_cp_genome({"cp_overlap_of": {"OLD": 1}})
    seen = []

    def _capture(setup, masters, ranks):
        seen.append(setup.config.cp_genome)
        return [], list(setup.target)

    monkeypatch.setattr(m, "_all_lines_schedule", _capture)
    m._movement_note({"k": 1}, cp_genome={"cp_overlap_of": {"NEW": 2}})
    assert len(seen) == 2, "the note did not build both plans"
    assert seen[0] == {"cp_overlap_of": {"OLD": 1}}, "the BEFORE side moved"
    assert seen[1] == {"cp_overlap_of": {"NEW": 2}}, \
        "the AFTER side was built with the PREVIOUS decisions"


def test_a_browser_cannot_supply_the_genome(monkeypatch):
    """`POST /run` MERGES the submitted config over the saved one, and the user
    role posts a config on every re-plan. Without the strip, a `cp_genome` in that
    payload lands in the SAVED plan config — where `_resolve_config`'s "only if
    none" guard then leaves it alone — and steers every later plan from a value
    the floor never searched. The 2026-08-09 "a user's browser could shape the
    plan" defect, in a new field."""
    m = _api()
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.save_plan_config(json.dumps(_cfg("cp").to_dict()))
    monkeypatch.setattr(m, "_plan", lambda config: {"config": config.to_dict()})

    class _Req:
        state = type("s", (), {"role": "admin"})()

    out = m.run(_Req(), m.RunRequest(
        config={"overlap_percent": 70,
                "cp_genome": {"cp_machine_of": {"B1\x1f1": "CNC9"}}},
        persist=True))
    assert out["config"]["cp_genome"] is None
    saved = Config.from_dict(json.loads(book_store.load_plan_config()))
    assert saved.cp_genome is None, "a browser's genome was persisted"
    assert saved.overlap_percent == 70   # the ordinary merge still works
    assert saved.scheduler == "cp"       # ...and the engine pin still holds


def test_apply_survives_a_mode_with_no_knob(monkeypatch):
    """`_optimize_apply` does `replace(target, **{knob: best_ov})`. knob is None
    under cp."""
    m = _stage_apply(monkeypatch, genome={"ranks": {"k": 1}})
    meta = m._optimize_apply()
    assert meta["saved_at"]
    saved = Config.from_dict(json.loads(book_store.load_plan_config()))
    assert saved.scheduler == "cp"


def test_apply_ignores_an_overlap_a_cp_config_has_nowhere_to_put(monkeypatch):
    """The `and knob` half of that guard, which `best_overlap: None` alone cannot
    reach. A result blob CAN carry a numeric overlap under cp — `pick_winner`
    returns whatever `row["overlap"]` holds, and a job in flight across a deploy
    (or an older worker's rows) supplies one. Then `knob` is None and
    `replace(target, **{None: 80})` raises TypeError INSIDE the apply, losing a
    search that had already succeeded. The honest behaviour is to keep the plan
    and drop the number that has nowhere to live."""
    m = _stage_apply(monkeypatch, genome={"ranks": {"k": 1}})
    before = Config.from_dict(json.loads(book_store.load_plan_config())).overlap_percent
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"]["best_overlap"] = 80
        m._OPTIMIZE["result"]["knob"] = None
    m._optimize_apply()
    saved = Config.from_dict(json.loads(book_store.load_plan_config()))
    assert saved.overlap_percent == before, \
        "an overlap was written under an engine that has no overlap setting"
    assert saved.scheduler == "cp"
    assert book_store.load_plan_priority(), "the apply itself was lost"


# =========================================================================== #
# The Apply gate
# =========================================================================== #

def test_the_cp_apply_gate_ranks_on_late_days_then_spread():
    """`optimizer.score` is SYMMETRIC — it penalises finishing early exactly like
    finishing late — and it once made the app reject a plan 86 late-days better.
    Under an engine whose own objective IS total late-days the two disagree by
    construction, so the gate reads the engine's own number."""
    worse_score_better_late = {"total_late_days": 100, "slip_severity": 5.0,
                               "ontime_breach": 900.0, "makespan_days": 40.0}
    better_score_worse_late = {"total_late_days": 186, "slip_severity": 5.0,
                               "ontime_breach": 10.0, "makespan_days": 40.0}
    cp = _cfg("cp")
    assert optimizer.score(better_score_worse_late) < optimizer.score(worse_score_better_late)
    assert (optimizer.apply_key(worse_score_better_late, cp)
            < optimizer.apply_key(better_score_worse_late, cp))
    # Ties on late-days fall to the spread.
    a = {"total_late_days": 10, "slip_severity": 1.0, "ontime_breach": 0.0,
         "makespan_days": 1.0}
    b = {"total_late_days": 10, "slip_severity": 4.0, "ontime_breach": 0.0,
         "makespan_days": 1.0}
    assert optimizer.apply_key(a, cp) < optimizer.apply_key(b, cp)


@pytest.mark.parametrize("sched", ["classic", "flow", "new", "roster"])
def test_every_other_engine_still_applies_on_score(sched):
    """Constraint: nothing moves until DEFAULT_SCHEDULER=cp. These engines SEARCH
    on `score`, so the gate that accepts their answer must read the same number."""
    m = {"total_late_days": 100, "slip_severity": 5.0, "ontime_breach": 900.0,
         "makespan_days": 40.0}
    assert optimizer.apply_key(m, _cfg(sched)) == (optimizer.score(m),)
    assert optimizer.apply_key(m, None) == (optimizer.score(m),)


def test_the_apply_gate_uses_apply_key(monkeypatch):
    """End of the wire: a CP result that `score` would reject but late-days
    prefer must actually be applied."""
    m = _stage_apply(
        monkeypatch, genome={"ranks": {"k": 1}},
        best={"total_late_days": 100, "makespan_days": 40.0, "max_late_days": 1,
              "max_committed_slip": 0, "slip_severity": 5.0, "ontime_breach": 900.0},
        inc={"total_late_days": 186, "makespan_days": 40.0, "max_late_days": 1,
             "max_committed_slip": 0, "slip_severity": 5.0, "ontime_breach": 10.0})
    monkeypatch.setattr(m, "_movement_note", lambda ranks: "")
    m._auto_apply_result()
    assert book_store.load_plan_priority(), "the better CP plan was not applied"


def test_the_worst_order_and_promise_backstops_are_untouched(monkeypatch):
    """Step 8 changes the RANKING only. The no-regression check on the worst
    order stays exactly where it was."""
    m = _stage_apply(
        monkeypatch, genome={"ranks": {"k": 1}},
        best={"total_late_days": 1, "makespan_days": 1.0, "max_late_days": 40,
              "max_committed_slip": 0, "slip_severity": 1.0, "ontime_breach": 1.0},
        inc={"total_late_days": 186, "makespan_days": 40.0, "max_late_days": 1,
             "max_committed_slip": 0, "slip_severity": 5.0, "ontime_breach": 10.0})
    m._auto_apply_result()
    assert not book_store.load_plan_priority(), \
        "a plan that pushes the worst order 39 days later was applied"


# =========================================================================== #
# The validation report
# =========================================================================== #

def _tiny_masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5),
                  "MD1": Machine("MD1", "MD 1", "manual",
                                 available_hrs_per_day=9.5)},
        routings={"A": Routing("A", "a", "cust", "rm", None, [
            Process(1, "CNC FIRST SIDE", 5.0, None, None, "CNC1"),
            Process(2, "DEBURING", 1.0, None, None, "MD1")])},
        operators=[Operator("N", "CNC1", ["CNC1"], "First shift"),
                   Operator("M", "MD1", ["MD1"], "First shift")],
        calendar=WorkCalendar())


class _B:
    def __init__(self, key="B1", item="A", qty=10):
        self.batch_id, self.item_code, self.qty = key, item, qty
        self.so_refs = self.source_so_refs = ["SO-%s" % key]
        self.delivery_date, self.process_remaining = date(2026, 12, 1), None


def test_the_report_shows_cp_breaches_but_not_capacity_measurements(monkeypatch):
    """`all_violations` returns breaches AND capacity measurements in one list,
    told apart only by `row["breach"]`. IDLE_CAPACITY is legitimately non-zero
    under E1, so handing the list over undifferentiated puts a capacity note
    beside a real rule breach and buries the row that matters."""
    m = _api()
    masters = _tiny_masters()
    cfg = _cfg("cp")
    sched = cp_adapter.run([_B()], config=cfg, masters=masters)
    rows = [{"kind": "OPERATOR_SPLIT_SHIFT", "ref": "CNC1", "message": "boom",
             "breach": True},
            {"kind": "IDLE_CAPACITY", "ref": "MD1", "message": "spare",
             "breach": False}]
    monkeypatch.setattr(cp_adapter, "plan_violations",
                        lambda *a, **kw: ([r for r in rows if r["breach"]],
                                          [r for r in rows if not r["breach"]]))
    table = m._report_for_book(masters, [], config=cfg, schedule=sched,
                               batches=[_B()])
    col = table["columns"].index("Kind")
    kinds = {r[col] for r in table["rows"]}
    assert "OPERATOR_SPLIT_SHIFT" in kinds
    assert "IDLE_CAPACITY" not in kinds


def test_the_report_is_untouched_for_every_other_engine(monkeypatch):
    m = _api()
    masters = _tiny_masters()
    monkeypatch.setattr(cp_adapter, "plan_violations",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("cp check ran on a non-cp plan")))
    for sched in ("classic", "roster", "new", "flow"):
        m._report_for_book(masters, [], config=_cfg(sched), schedule=[],
                           batches=[])


def test_the_absence_blocks_reach_the_cp_check(monkeypatch):
    """Without them a person on leave reads as spare capacity and the check
    accuses the plan of wasting a machine nobody could have run."""
    m = _api()
    masters = _tiny_masters()
    seen = []
    monkeypatch.setattr(cp_adapter, "plan_violations",
                        lambda *a, **kw: (seen.append(kw), ([], []))[1])
    sched = cp_adapter.run([_B()], config=_cfg("cp"), masters=masters)
    m._report_for_book(masters, [], config=_cfg("cp"), schedule=sched,
                       batches=[_B()],
                       absences=[{"operator": "N", "from_date": "2026-08-12",
                                  "to_date": "2026-08-13"}])
    assert seen and seen[0].get("reserved"), \
        "the absence blocks never reached the CP self-check"


# =========================================================================== #
# The import graph — the failure that appears only in production
# =========================================================================== #

def test_pyjobshop_is_not_in_requirements():
    text = (REPO / "requirements.txt").read_text().lower()
    assert "pyjobshop" not in text
    assert "ortools" not in text


def test_replay_path_imports_without_pyjobshop():
    """Render imports cp_engine transitively through engine/cp_adapter.py, which
    the wiring now reaches on every page load. If any replay-path module imports
    pyjobshop at module level the live site 500s on boot — and only there, never
    in CI, where pyjobshop IS installed."""
    import subprocess
    code = (
        "import sys\n"
        "class _Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name.split('.')[0] in ('pyjobshop', 'ortools'):\n"
        "            raise ImportError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "import engine.cp_adapter, engine.pipeline, engine.optimizer\n"
        "import engine.optimize_service, engine.book_store\n"
        "import api.main\n"
        "assert engine.pipeline.scheduler_for(None) is not None\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-4000:]
    assert "ok" in out.stdout
