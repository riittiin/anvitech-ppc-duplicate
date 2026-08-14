"""The seam between the app and ``cp_engine``.

The ONLY file that knows both worlds. Everything upstream (loader, order book,
store, freeze, Rules 1-3) and everything downstream (Gantt, Schedule tab, delay
report, Analytics, shift-wise export, efficiency report, Daily Entry) is
untouched, because this returns exactly the ``ScheduleEntry`` list they already
consume. That is what makes an A/B against the live engine exact.

TWO ENTRY POINTS, AND THEY ARE NOT THE SAME THING:

  ``run()``    REPLAYS the stored genome. Milliseconds. This is what ``/run``
               calls on every page load, and it never solves anything. It must
               import nothing from ``cp_engine.solve``/``model``/``rules``/
               ``objective``, because Render does not have pyjobshop installed
               and never will.

  ``solve()``  SOLVES. Minutes. Worker-only, reached through the Optimize panel,
               and every solver import in it is INSIDE the function.

Confusing the two is how this ships broken: wire ``run()`` to a solve and every
page load starts a CP solve on a 0.5-CPU free instance. The import graph is
pinned by a test that blocks pyjobshop AND ortools in a subprocess and then
replays a plan — the failure it guards is one that appears only in production,
only on boot, and never in CI, where pyjobshop IS installed.

Contract notes that are load-bearing elsewhere:

  * ``op_segments`` is a **list** of ``(start, end, operator)`` tuples sorted by
    start. Five surfaces read it, and ``rule6_allocate``'s operator-balance pass
    assigns into it by index — a tuple would raise there.
  * OS and off-machine entries carry the lane STRINGS below as their machine.
    ``delay_report._OFF_LANES``, ``analytics.NON_MACHINE_LANES`` and
    ``freeze._OS_LANES`` all match on them literally; anything else bills
    outsourcing to an in-house machine (the 2026-08-09 defect).
  * EVERY real-machine entry names an operator and carries at least one segment.
    ``freeze.py`` pins machine AND operator, so an empty name freezes a ghost.
  * Rule 1 already clubbed the SO lines into batches upstream. Nothing here
    re-consolidates, and every quantity comes from the BATCH (``Job.qty_for``),
    never from a per-SO-line remainder (live 2026-08-11: 281 pieces of a clubbed
    order in no plan at all).

THE FROZEN-ROW TRANSLATION is the delicate part, and this file deliberately does
not own a copy of it. ``engine.roster_adapter._pins`` is THE definition: frozen
rows are keyed per **SO LINE** (``engine.freeze.compute_frozen_set`` emits
``{so_no, item_code, process, op_seq, machine, operator, remaining_qty,
prev_start}``) while a job key is a **BATCH** id, and handed over raw every row
is dropped while the plan still looks perfectly well-formed. Its output shape is
exactly what ``cp_engine.decode._apply_frozen`` and ``cp_engine.rules.resolve_
pins`` read (``order_key`` / ``op_seq`` / ``machine_id``, and never a quantity),
so it is imported rather than re-typed. See that function for the five things
the translation has to get right.
"""

from __future__ import annotations

from datetime import date, datetime, time

from cp_engine import SCHEDULER_FINGERPRINT  # noqa: F401  (re-exported)
from cp_engine import decode, domain
from cp_engine import report as cp_report
from cp_engine.domain import OUTSOURCED
from cp_engine.genome import _TUPLE_KEYED, from_json as _genome_from_json
from engine.models import ScheduleEntry
from engine.pipeline import RuleError
# ONE definition of the per-SO-line -> batch frozen-row translation, and of how a
# frozen row is read. A second copy is the defect class this repo keeps paying
# for, so these are imported from the sibling seam that already carries them
# rather than restated here. ``_pins`` is pure: rows in, pin dicts out, notes
# appended to the list it is handed — it knows nothing about either engine.
from engine.roster_adapter import _describe, _pins, _so_refs

# The exact strings every off-machine consumer matches on. Pinned by test against
# delay_report._OFF_LANES / analytics.NON_MACHINE_LANES / freeze._OS_LANES.
OS_LANE = "OS / Outsourced"
OFF_LANE = "Off-machine"

# How long one solve may run, in seconds, when the config carries no
# ``cp_time_limit_sec``. The tractability measurement (2026-08-14,
# docs/superpowers/plans/2026-08-14-cp-tractability-findings.md, decision 1) is
# that this ships as a time-boxed FEASIBLE-with-a-bound engine at a ~15-30 minute
# budget on a 4+ core worker; 15 minutes is the low end, chosen because Render's
# own OPTIMIZE_CLOUD_TIMEOUT_MIN is 20 and a budget that outlives the caller
# waiting for it is a budget nobody collects.
CP_TIME_LIMIT_SEC = 900

# How far the working calendar is built, and therefore how far a task may run. A
# book that genuinely does not fit comes back ``status_ok=False`` — widen this,
# never relax the horizon closure in ``solve_book``. 70 days is the horizon every
# tractability number was measured at.
CP_HORIZON_DAYS = 70

# Rule 2's "may span an unmanned shift" clause. **E1 (False) is the shipping
# default and this is a deliberate reversal** of the provisional E2 default the
# ledger carried before it was measured: E2 costs 2-3x the model on the owner's
# scale and shortening the horizon does not rescue it (findings §2, decision 2).
# ``solve_book``'s own default is True, so this must be passed explicitly.
CP_HOLD_ACROSS_UNMANNED_SHIFT = False


# --------------------------------------------------------------------------- #
# The replay — what /run calls on every page load
# --------------------------------------------------------------------------- #

def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None, genome=None, **kw):
    """Scheduler seam contract: prioritized ``batches`` -> list[ScheduleEntry].

    REPLAYS the genome; it decides nothing and it never solves. The job order,
    the machine per operation, the crew and the overlap all come from the genome
    (``cp_engine.decode``), and only the WHEN is computed here.

    ``genome`` is the stored decisions. It is read from the argument first (the
    solve's own replay hands one over directly) and otherwise off
    ``config.cp_genome``, which is where the app attaches it — ``/run`` has no
    genome argument, exactly as the roster engine's crew genome rides on the
    config. No genome at all is a legal, flagged state, not an error: the decoder
    falls back per operation and the book still gets a plan, which is what the
    first plan after an upload and every plan before the first search must do.

    ``machine_lost_min`` is accepted for signature compatibility and ignored, as
    it is by the ``new``, ``flow`` and ``roster`` engines; no caller passes one.
    """
    say = notes if notes is not None else []
    if not batches or masters is None:
        return []

    jobs, batch_by_key, skipped = domain.build_jobs(batches, masters)
    if skipped:
        say.append("skipped %d item(s) with no routing: %s"
                   % (len(skipped), ", ".join(sorted(skipped)[:5])))
    if not jobs:
        return []

    shop = domain.build_shop(masters, _absent_from_reserved(reserved, masters, say))
    pins = _pins(frozen, batch_by_key, masters, say)
    g = _genome(config, genome)
    try:
        plan = decode.lay_out(jobs, shop, config, _plan_start(config), g,
                              frozen=pins)
    except (decode.Unschedulable, decode.PlanStartMissing) as err:
        raise _rule_error(err) from err
    _check_pins_landed(pins, plan, say)
    _report_dropped(plan, batch_by_key, say)
    _report_unassigned(plan, batch_by_key, g, say)
    return _entries(plan, batch_by_key)


# --------------------------------------------------------------------------- #
# The Optimize feature's entry point — WORKER ONLY
# --------------------------------------------------------------------------- #

def solve(so_lines, config, masters, *, reserved=None, budget_evals=150,
          seed=42, on_progress=None, should_cancel=None, frozen=None):
    """Solve the book with CP and return the app's own ``OptimizeResult``.

    Every solver import is INSIDE this function. ``cp_engine.solve`` imports
    pyjobshop and ortools at call time, and neither is installed on the server
    that serves the app — importing this module must stay free of both.

    ``budget_evals`` is accepted for signature compatibility with every other
    engine's search and **ignored**: a CP solve is time-boxed, not eval-boxed.
    There is exactly ONE solve, so the progress hook fires once per IMPROVED
    solution and its count is a running total, never a fraction of a budget.

    ``frozen`` is not optional decoration. ``run()`` honours it (it pins
    in-progress work to the machine it is physically on), so a solve that ignored
    it would optimise a plan the app will never build.

    The whole point of the seam is that both halves share ONE plan clock: the
    genome's shift indices are counted from ``_plan_start(config)``, so a solve
    and a replay that disagreed about it by an hour would silently be two
    different plans. Both call the same function.
    """
    from cp_engine import solve as cp_solve      # LAZY: pyjobshop lives in here
    from engine.optimizer import OptimizeResult, plan_metrics
    from engine.rules import rule1_consolidate

    batches = rule1_consolidate.run(list(so_lines), config=config,
                                    masters=masters)
    if not batches:
        return OptimizeResult()
    say: list = []
    jobs, batch_by_key, _skipped = domain.build_jobs(batches, masters)
    if not jobs:
        return OptimizeResult()

    absent = _absent_from_reserved(reserved, masters, say)
    pins = _pins(frozen, batch_by_key, masters, say)
    plan_start = _plan_start(config)

    # The app's progress hook is ``(evals, metrics)``. The metrics object is
    # deliberately not forwarded: it is cp_engine's shape, not the dict every
    # caller expects, and the app recomputes ``best`` at finalize anyway.
    seen = {"n": 0}

    def _tick(_info):
        seen["n"] += 1
        on_progress(seen["n"], None)

    solved = cp_solve.solve_book(
        batches, masters, config, plan_start,
        time_limit=float(getattr(config, "cp_time_limit_sec", None)
                         or CP_TIME_LIMIT_SEC),
        horizon_days=int(getattr(config, "cp_horizon_days", None)
                         or CP_HORIZON_DAYS),
        num_workers=int(getattr(config, "cp_num_workers", None) or 1),
        absent=absent, frozen=pins,
        hold_across_unmanned_shift=bool(getattr(
            config, "cp_hold_across_unmanned_shift",
            CP_HOLD_ACROSS_UNMANNED_SHIFT)),
        seed=int(seed),
        on_progress=_tick if on_progress is not None else None,
        should_cancel=should_cancel)

    cancelled = bool(should_cancel is not None and should_cancel())
    if not solved.status_ok:
        # No plan, not a crash. A search that cannot solve this book must leave
        # the plan the shop already has in place, exactly as an unimproved
        # contest does.
        return OptimizeResult(evals=seen["n"], cancelled=cancelled)

    g = solved.genome
    # MEASURE THE WINNER BY REPLAYING IT EXACTLY AS THE APP WILL — same genome,
    # same frozen pins, same reservations. Anything else re-opens the gap where
    # the panel promises a plan the apply does not reproduce; and under this
    # engine the model and the decoder are two definitions of one schedule, so
    # the published number has to come from the published plan.
    winner = run(batches, config=config, masters=masters, reserved=reserved,
                 frozen=frozen, genome=g)
    metrics = plan_metrics(
        winner, so_lines, plan_start.date(),
        ceiling_days=getattr(config, "worst_ceiling_days", None),
        with_distribution=True,
        promise_slack_days=getattr(config, "committed_promise_slack_days", 3))
    metrics["cp_total_late_days"] = solved.total_late_days
    metrics["cp_spread"] = solved.spread
    metrics["cp_lower_bound_days"] = solved.lower_bound_days
    metrics["cp_status"] = solved.status
    # ``ranks`` is the genome's own — the solver's job order, keyed
    # "<so>\x1f<item>" exactly as ``optimizer.ranks_for`` produces it, so Apply
    # and ``pipeline.apply_priority_rank`` need no new vocabulary.
    return OptimizeResult(ranks=dict(g.get("ranks") or {}), best=metrics,
                          evals=seen["n"], improved=True, cancelled=cancelled,
                          genome=g)


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None,
                   frozen=None, **kw):
    """The "Start deep search" contract, over an engine that has no sweep.

    Every other engine tunes a knob OUTSIDE the search — the overlap % under
    classic/roster, the chunk count under flow — because their schedulers cannot
    choose it. **Here the overlap is a model variable**, picked per job by the
    solver under the same objective as everything else (``rules.add_release``),
    so sweeping it outside would re-solve the same book N times to answer a
    question the model already answered, at N times the worker's wall clock.

    So this is ONE solve, wrapped in the ``SweepResult`` the panel and the apply
    path already speak. ``overlap_percent`` comes back UNCHANGED — Apply
    persists it, and a value invented here would overwrite the stored one with a
    number that means nothing under this engine.
    """
    from engine.optimizer import SweepResult

    overlap = int(getattr(config, "overlap_percent", 0) or 0)
    res = solve(so_lines, config, masters, reserved=base_reserved,
                budget_evals=budget_evals, seed=seed, on_progress=on_progress,
                should_cancel=should_cancel, frozen=frozen)
    table = [{"overlap": overlap, "eligible": True, "best": res.best,
              "evals": res.evals}]
    return SweepResult(overlap_percent=overlap, knob="overlap_percent",
                       flexible_machines=False, result=res, table=table,
                       evals=res.evals, cancelled=res.cancelled)


# --------------------------------------------------------------------------- #
# The self-check, partitioned the one way it can be read wrongly
# --------------------------------------------------------------------------- #

def plan_violations(entries, masters, config, *, batches=None, genome=None,
                    reserved=None):
    """``(breaches, measurements)`` from ``cp_engine.report.all_violations``.

    The four independent rule checks, drift and staleness all answer "did this
    plan break a rule, or disagree with the solve?" and must read 0.
    ``IDLE_CAPACITY`` answers something else entirely — "what did this plan leave
    on the table?" — and is legitimately non-zero under this engine (E1 forbids
    an operation spanning an unmanned shift; the decoder defers a pool-staffed
    machine's fallback work by one shift on purpose). Hand the undifferentiated
    list to the directors' banner and a capacity note sits beside a real rule
    breach and buries the row that matters.

    The partition reads ``row["breach"]``, which ``all_violations`` puts on every
    row it returns. A row WITHOUT it is treated as a breach: an unrecognised row
    that surfaces is a nuisance, an unrecognised row that is silently filed under
    "measurements" is a rule breach nobody sees.

    ``reserved`` is the app's absence blocks, passed through as ``absent=`` —
    without them a person on leave reads as spare capacity and the check accuses
    the plan of wasting a machine nobody could have run.
    """
    rows = cp_report.all_violations(
        entries, masters, config, batches=batches,
        genome=_genome(config, genome) or None,
        absent=_absent_from_reserved(reserved, masters, []))
    breaches = [row for row in rows if row.get("breach", True)]
    measured = [row for row in rows if not row.get("breach", True)]
    return breaches, measured


# --------------------------------------------------------------------------- #
# The plan clock — ONE definition, shared by the solve and the replay
# --------------------------------------------------------------------------- #

def _plan_start(config) -> datetime:
    """When the clock starts: the first shift's start hour on the plan-start
    date, or the auto-mode floor if that is later.

    The SAME rule ``roster_engine.scheduler._plan_start`` applies, deliberately —
    two engines that disagreed about when "today" begins would produce plans that
    cannot be compared, and the A/B between them is how this engine is judged.
    ``Config.plan_start_floor`` is an ISO **string**, not a datetime, and Config
    stores plain hour ints rather than a time.

    ``plan_start_date`` is nullable in Config ("auto: start from today, IST") and
    the API boundary resolves it; this carries the same ``or date.today()``
    fallback ``new_engine`` and ``roster_adapter`` already carry, for the paths
    that reach an engine without going through it.
    """
    day = getattr(config, "plan_start_date", None) or date.today()
    hour = int(getattr(config, "first_shift_start_hour", 8) or 0)
    base = datetime.combine(day, time(hour, 0))
    floor = getattr(config, "plan_start_floor", None)
    if not floor:
        return base
    if isinstance(floor, str):
        floor = datetime.fromisoformat(floor)
    return max(base, floor)


def _genome(config, given) -> dict:
    """The stored decisions, whatever door they came in by.

    Read from the argument first, then off ``config.cp_genome``. A genome that
    has been through JSON — the store writes it there, and the cloud worker's
    payload carries it home — has its tuple keys flattened to ``"B1\\x1f1"``
    strings. Read back the wrong way that is not an exception and not a
    malformed genome: every lookup against the real ``("B1", 1)`` tuple simply
    MISSES, the decoder falls back for every single operation, and the plan looks
    perfectly well-formed. So the shape is checked here, at the one door both
    paths go through, rather than trusted to have been converted upstream.
    """
    raw = given if given is not None else getattr(config, "cp_genome", None)
    if not raw:
        return {}
    flattened = any(isinstance(key, str)
                    for name in _TUPLE_KEYED
                    for key in (raw.get(name) or {}))
    return _genome_from_json(raw) if flattened else dict(raw)


def _absent_from_reserved(reserved, masters, say) -> dict:
    """``reserved`` maps machine id / operator name -> busy ``(start, end)``
    intervals. Only the operator entries mean anything to this engine: an absent
    person cannot be rostered or booked, and ``Shop`` has no machine-reservation
    concept.

    Today's only live producer is ``optimize_service.absence_reservations``,
    which emits operator names only. A key that is NOT an operator is therefore
    unexpected — it is reported rather than swallowed, because a constraint that
    quietly does nothing is this codebase's most expensive recurring defect.
    """
    if not reserved:
        return {}
    names = {o.name for o in masters.operators}
    out = {k: list(v) for k, v in reserved.items() if k in names}
    ignored = sorted(k for k in reserved if k not in names)
    if ignored:
        say.append("ignored %d reserved block(s) that name no operator in "
                   "Settings (the CP engine reserves people, not machines): %s"
                   % (len(ignored), ", ".join(ignored[:5])))
    return out


# --------------------------------------------------------------------------- #
# Reporting what the plan could not do
# --------------------------------------------------------------------------- #

def _report_dropped(plan, batch_by_key, say):
    """Name every order the engine could not place. It is in NO plan — no bar on
    the Gantt, no row in the delay report — and a silently dropped order is this
    codebase's most expensive recurring defect class, so it is reported through
    the same ``notes`` channel ``build_jobs`` reports its NO_ROUTING skips on.

    The plan itself is still returned: one machine with nobody qualified for it
    in Settings must not cost every OTHER order its schedule (CLAUDE.md principle
    5(a)). ``cp_engine.decode`` raises instead when NOTHING could be placed.
    """
    dropped = list(getattr(plan, "dropped", ()) or ())
    if not dropped:
        return
    say.append(
        "CP replay: %d order(s) could NOT be scheduled and are in no plan — %s%s"
        % (len(dropped),
           "; ".join("%s (%s) blocked at step %s '%s' on %s — %s"
                     % (key, _order_label(batch_by_key.get(key)), seq, name,
                        "/".join(options) or "(no machine)", why)
                     for key, seq, name, options, why in dropped[:5]),
           "" if len(dropped) <= 5 else "; …"))


def _report_unassigned(plan, batch_by_key, g, say):
    """Orders the genome never covered — uploaded since the solve, or with a
    routing that has moved under them. They ARE planned (on their routing's first
    machine option); the note is what says the published plan is partly not the
    one that was searched.

    Silent when there is no genome at all, deliberately: before the first search
    EVERY order is uncovered, and a banner that fires on every plan of a fresh
    book teaches the directors to ignore the one that is real. The staleness of
    a genome that HAS been solved is ``report.genome_stale``'s job, not this.
    """
    unassigned = list(getattr(plan, "unassigned", ()) or ())
    if not unassigned or not g:
        return
    say.append("CP replay: %d order(s) are not in the searched plan and were "
               "laid out on their routing's first machine — %s%s"
               % (len(unassigned),
                  "; ".join("%s (%s)" % (key, _order_label(batch_by_key.get(key)))
                            for key in unassigned[:5]),
                  "" if len(unassigned) <= 5 else "; …"))


def _check_pins_landed(pins, plan, say):
    """The accounting must close: every pin handed over is either applied or
    named in ``Plan.unpinned``. A pin the engine could not honour is a real,
    reportable fact — the part is planned on a machine it is not on and pays a
    setup it does not owe — so it is surfaced rather than trusted to be noticed.
    """
    unpinned = list(getattr(plan, "unpinned", ()) or ())
    if not unpinned:
        return
    say.append("frozen work: %d of %d in-progress operation(s) could not be held "
               "on the machine they are running on — %s"
               % (len(unpinned), len(pins),
                  "; ".join("%s (%s)" % (_describe(row), why)
                            for row, why in unpinned[:5])))


def _order_label(batch) -> str:
    if batch is None:
        return "?"
    refs = ", ".join(str(so) for so in _so_refs(batch)[:3])
    return "%s%s" % (getattr(batch, "item_code", "?"), " / " + refs if refs else "")


def _rule_error(err) -> RuleError:
    """``cp_engine`` fails loud with its own ``Unschedulable``: it knows nothing
    about this app and must not, so it cannot raise ``RuleError`` itself.
    Translating it here is part of why this file exists.

    Why the translation matters: ``pipeline.run_rule`` catches ``RuleError`` and
    nothing else. An escaping ``RuntimeError`` unwinds ``run_forward`` entirely —
    the trace Rules 1-3 filled in is discarded, every per-rule tab goes blank and
    ``POST /run`` returns a 500 with nothing to show the planner. CLAUDE.md
    principle 5(b): a rule-level contract violation is typed and LOCALIZED. The
    same argument is why ``PlanStartMissing`` (a ``ValueError``, so an
    ``except Unschedulable`` never saw it) is caught with it.
    """
    blocked = getattr(err, "blocked", ()) or ()
    record_id = str(blocked[0][0]) if blocked else "-"
    return RuleError("rule6", record_id, str(err))


# --------------------------------------------------------------------------- #
# Placements -> ScheduleEntry
# --------------------------------------------------------------------------- #

def _entries(plan, batch_by_key) -> list:
    """``decode.Placement`` is field-for-field identical to the sibling greedy
    engine's, so this is the same mapping, kept in step with it deliberately."""
    out = []
    for p in plan.placements:
        batch = batch_by_key.get(p.job_key)
        if batch is None:                       # cannot happen; never guess a batch
            continue
        if p.machine is None:
            # An outsourced block goes to the OS lane; every other machine-less
            # step (DISPATCH, and a step a re-plan has nothing left to make on)
            # is an off-machine milestone. Both lanes are matched literally by
            # the delay report, Analytics and freeze.
            machine = OS_LANE if p.kind == OUTSOURCED else OFF_LANE
            segments, operator = [], ""
        else:
            machine = p.machine
            segments = [(s, e, who) for s, e, who in p.segments]
            operator = segments[0][2] if segments else ""
        out.append(ScheduleEntry(
            batch_id=str(batch.batch_id),
            item_code=batch.item_code,
            process_seq=p.op_seq,
            process_name=p.op_name,
            machine=machine,
            qty=p.qty,
            occupancy_min=p.work_min,
            start=p.start,
            end=p.end,
            so_refs=_so_refs(batch),
            operator=operator,
            op_segments=segments))
    # REDUNDANT TODAY, AND MEASURED SO: ``decode._pace`` already returns
    # placements sorted by exactly this key, so mutating this line away kills no
    # test. Kept, like ``_pace``'s own belt-and-braces sweep, because the
    # published order is a contract (the Schedule tab and the Rule 6 CSV render
    # this list as given, and the two engines' output is diffed line for line)
    # and it must not depend on which layer last touched the list.
    out.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
    return out
