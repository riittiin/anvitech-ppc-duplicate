"""``solve_book`` — the composition that finally runs the engine.

    domain.build_jobs / build_shop
      -> windows.build_shifts
      -> model.build                      (machines, tasks, modes, precedences)
      -> CPModel(data)                    (pyjobshop's own constraints)
      -> CLOSE THE HORIZON
      -> rules.add_roster                 (Rule 1)
      -> rules.add_release                (Rule 3)
      -> rules.add_setup_credit           (Rule 4)   + assert it was linked
      -> objective.add_days_late
      -> phase 1 solve  (minimise total late-days)
      -> phase 2 solve  (minimise the spread, total held)   warm-started
      -> genome.from_solution

Two things in that list are not obvious and are the reason this module exists
rather than a few lines in a caller.

**The horizon is closed here, and nowhere else.** ``windows.machine_breaks``
describes time only as far as the horizon it is given. PAST the last shift a
machine has no breaks, no calendar and no unstaffed shifts — so a solver handed
work it cannot staff will park it out there and report OPTIMAL (measured: minute
28,620 on a book whose last shift ends at 28,620). The objective would then be
minimised against a schedule that cannot happen. Every task therefore ends inside
the last shift, and a book that genuinely does not fit comes back
``status_ok=False`` — widen ``horizon_days``, do not relax this.

**The solve runs on a raw ``CpSolver``, not ``CPModel.solve``.** Rule 1's roster
and Rule 3's k are built directly on the underlying ``CpModel`` by
``cp_engine.rules``, so the only way to read what the solver decided about them
is the solver object itself — and ``CPModel.solve`` creates one, uses it and
throws it away. The pyjobshop ``Solution`` the genome reads is rebuilt here from
the same solver, by pyjobshop's own definitions.

WORKER-ONLY. Imports pyjobshop and ortools, both deliberately absent on Render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cp_engine import domain, genome, model, objective, rules, windows

# How the time budget splits between the two phases. Fixed SHARES, not "phase 2
# gets whatever phase 1 left over": an elapsed-time remainder makes the second
# solve's limit depend on wall clock, and two runs of the same book would then
# search for different lengths and could return different (equally optimal)
# plans. Determinism is a stated requirement of this engine.
_PHASE_ONE_SHARE = 0.6


@dataclass
class Solved:
    """What one solve produced, in the shape every later layer reads it in.

    ``total_late_days`` and ``days_late`` are UNCAPPED — the real number the
    owner is judged on, derived from the solved completions. ``spread`` is the
    sum of CAPPED squares, because the cap exists only to stop one hopeless
    order dominating the distribution. ``stats["capped_total_late_days"]`` is the
    domain the spread was computed over, for anyone reconciling the two.

    ``status`` is the WEAKER of the two phases' statuses — see ``_weaker``.
    """

    status_ok: bool
    status: str
    genome: dict
    total_late_days: float | None
    spread: float | None
    lower_bound_days: float | None
    stats: dict
    shifts: list
    completion: dict                      # job key -> completion datetime
    days_late: dict = field(default_factory=dict)   # job key -> TRUE days late

    # Per-task detail. ``windows`` is the raw map ``task_window`` reads; it is
    # public because a whole-plan check wants to sweep it rather than ask for one
    # key at a time.
    windows: dict = field(default_factory=dict)     # (job key, op seq) -> (s, e)
    machine_of: dict = field(default_factory=dict)  # (job key, op seq) -> machine
    processing: dict = field(default_factory=dict)  # (job key, op seq) -> minutes
    quantities: dict = field(default_factory=dict)  # (job key, op seq) -> pieces

    def task_window(self, job_key: str, op_seq: int) -> tuple:
        """``(start, end)`` in minutes from the plan start."""
        return self.windows[(job_key, op_seq)]

    def machine_busy_minutes(self, mid: str) -> int:
        """Processing minutes booked on this machine — SETUP INCLUDED, because
        setup is inside the selected mode's duration (Rule 4, inverted). This is
        the number a wrongly granted credit makes too small."""
        return sum(minutes for key, minutes in self.processing.items()
                   if self.machine_of.get(key) == mid)

    def op_qty(self, job_key: str, op_seq: int) -> int:
        """Pieces this step ran.

        Derived at BATCH level (``domain.Job.qty_for``), never from a per-SO-line
        number: Rule 1 clubs SO lines, and the 2026-08-11 escalation was exactly
        a quantity taken from one clubbed line — 88 pieces planned, 281 in no
        plan at all."""
        return self.quantities[(job_key, op_seq)]


def solve_book(batches, masters, config, plan_start: datetime, *,
               time_limit: float, horizon_days: int, num_workers: int = 1,
               absent=None, frozen=None, hold_across_unmanned_shift: bool = True,
               setup_mode: str = "credit", seed: int = 42,
               on_progress=None, should_cancel=None) -> Solved:
    """Solve one book and return the decisions.

    Args:
        batches: Rule 1's output, already clubbed. Never re-consolidated here.
        masters: the app's ``Masters`` — machines, routings, and the SETTINGS
            operator table (``masters.operators`` is the Settings table, not the
            workbook sheet).
        config: the app's ``Config``. Read for shift hours, ``setup_time_min``
            and ``committed_promise_slack_days``-style knobs only; nothing here
            re-derives a working window (2026-08-07: a feature that re-derives
            shift hours WILL disagree with the engine that built the plan).
        plan_start: the plan clock. Minute 0.
        time_limit: TOTAL seconds for both phases together, split by
            ``_PHASE_ONE_SHARE``.
        horizon_days: how far the calendar is built, and therefore how far a task
            may run. A book that does not fit returns ``status_ok=False``.
        num_workers: pinned by callers that need determinism.
        absent: ``{operator name: [(from, to)]}`` wall-clock absence blocks.
        frozen: in-progress work pinned to its last-applied machine/operator.
            NOT IMPLEMENTED — raises rather than silently dropping it.
        hold_across_unmanned_shift: Rule 2's "may span an unmanned shift" clause;
            see ``rules._link_work_to_roster``.
        setup_mode: ``"credit"`` (Rule 4 as written) or ``"always"``.
        seed: CP-SAT's random seed.
        on_progress: called with a dict on every improved solution.
        should_cancel: polled on every improved solution; a true answer stops the
            search and keeps the best found so far.
    """
    if frozen:
        raise NotImplementedError(
            "solve_book does not honour a frozen set yet. Silently ignoring one "
            "would replan work that is physically running on another machine — "
            "pass frozen=None until that lands.")

    from ortools.sat.python import cp_model as cp_sat
    from pyjobshop.solvers.ortools.CPModel import CPModel

    jobs, _batch_by_key, skipped = domain.build_jobs(batches, masters)
    shop = domain.build_shop(masters, absent or {})
    shifts = windows.build_shifts(plan_start, masters.calendar, config,
                                  horizon_days)
    built = model.build(jobs, shop, config, plan_start, shifts,
                        setup_mode=setup_mode,
                        hold_across_unmanned_shift=hold_across_unmanned_shift)

    cp = CPModel(built.data)
    # CLOSE THE HORIZON. ``windows.machine_breaks`` describes time only as far as
    # the horizon it was given; past the last shift a machine has no breaks, no
    # calendar and no unstaffed shifts, so work the shop cannot staff parks out
    # there and the solve reports OPTIMAL against a schedule that cannot happen
    # (measured at minute 28,620 on a book whose last shift ends at 28,620).
    # A book that genuinely does not fit comes back status_ok=False — widen
    # horizon_days rather than relaxing this. No shifts at all (a horizon of
    # nothing but off days) is the same statement: no calendar, no plan.
    horizon = shifts[-1].end if shifts else 0
    for task_idx in range(built.data.num_tasks):
        cp.model.add(cp.variables.task_vars[task_idx].end <= horizon)

    roster = rules.add_roster(cp.model, cp.variables, built, shop,
                              hold_across_unmanned_shift=hold_across_unmanned_shift)
    released = rules.add_release(cp.model, cp.variables, built, config)
    rules.add_setup_credit(cp.model, cp.variables, built, config)
    if not built.setup_credit_linked:
        # A raise, not an assert: ``python -O`` strips assertions, and this one
        # guards capacity that does not exist. An unlinked model lets every
        # member of every same-part group take its setup-free mode
        # unconditionally — 90 minutes of CNC capacity invented per affected
        # task, with no exception, no failing test and no report row anywhere.
        raise RuntimeError(
            "the Rule 4 setup-credit modes were never linked "
            "(built.setup_credit_linked is False) — rules.add_setup_credit must "
            "run before the solve or the plan invents 90 minutes of machining "
            "capacity per same-part task.")

    days = objective.add_days_late(cp.model, cp.variables, built)

    # ---- phase 1: total late-days ------------------------------------------
    objective.phase_one(cp.model, days)
    stats = _base_stats(cp.model, built, shifts, days, skipped)
    solver, status = _run(cp.model, time_limit * _PHASE_ONE_SHARE, num_workers,
                          seed, on_progress, should_cancel, phase=1)
    stats["phase_one_status"] = solver.status_name(status)
    stats["phase_one_runtime"] = solver.wall_time
    if status not in (cp_sat.OPTIMAL, cp_sat.FEASIBLE):
        return Solved(False, solver.status_name(status), {}, None, None, None,
                      stats, shifts, {})

    # The UNCAPPED total — phase 1's own objective value, and the number phase 2
    # is held to. Never the capped sum: an order already past the cap would then
    # be free to drift later, and the guarantee would hold only on the metric
    # that cannot see the drift.
    total_star = sum(solver.value(d.true) for d in days.values())
    stats["phase_one_total"] = float(total_star)
    lower_bound = float(solver.best_objective_bound)

    # ---- phase 2: the spread, at that total ---------------------------------
    # Skipped outright when nothing is late: there is no distribution to even
    # out, and a second full solve for a provably-zero answer is pure cost.
    spread = 0.0
    status_name = stats["phase_one_status"]
    stats["phase_two_status"] = "SKIPPED"
    stats["phase_two_runtime"] = 0.0
    if total_star > 0:
        _hint(cp.model, cp.variables, built, solver, days, roster, released)
        # ``Config`` carries no fairness-slack field today, so this is 0 — the
        # strict tie-break the owner asked for. It is read off config rather
        # than hardcoded so buying evenness later is a settings change, and it
        # is read HERE rather than defaulted inside ``phase_two`` so there is
        # one place that decides what the shop's eps is.
        objective.phase_two(cp.model, days, total_star,
                            int(getattr(config, "cp_fairness_slack_days", 0) or 0))
        stats["phase_two_constraints"] = len(cp.model.proto.constraints)
        two_solver, two_status = _run(
            cp.model, time_limit * (1.0 - _PHASE_ONE_SHARE), num_workers, seed,
            on_progress, should_cancel, phase=2)
        stats["phase_two_status"] = two_solver.status_name(two_status)
        stats["phase_two_runtime"] = two_solver.wall_time
        if two_status in (cp_sat.OPTIMAL, cp_sat.FEASIBLE):
            solver = two_solver
            spread = float(two_solver.objective_value)
            # The WEAKER of the two, never simply phase 2's. Phase 2 is the
            # cheap, warm-started, ``sum(true) <= T*``-constrained half, so a
            # book whose phase 1 times out at FEASIBLE will very often see
            # phase 2 prove the optimal SPREAD at that unproven total — and
            # reporting OPTIMAL would publish "proven optimal" for a headline
            # total that is merely the best found. ``lower_bound_days`` still
            # carries phase 1's bound; this makes ``status`` say it too.
            status_name = _weaker(status_name, stats["phase_two_status"])
        else:
            # Phase 1's answer is feasible for phase 2 by construction (the added
            # constraint is ``<= T*``, read off that very solution), so a phase-2
            # failure can only be the time limit, never infeasibility. Keep phase
            # 1's plan — it has the total, which is what the owner is judged on —
            # and report the spread as unknown rather than as zero.
            spread = None

    return _read_back(cp, built, shifts, roster, released, days, solver,
                      plan_start, spread, lower_bound, stats, status_name)


# --------------------------------------------------------------------------- #
# Running one phase
# --------------------------------------------------------------------------- #

def _run(cp_model, time_limit, num_workers, seed, on_progress, should_cancel,
         *, phase: int):
    """One phase's solve.

    ``should_cancel`` is polled on the solver's SOLUTION callback, so a stop is
    observed when a better plan is found and at the time limit — not instantly.
    That is the only hook CP-SAT offers without a second thread, and it is
    enough: a cancelled run keeps the best plan found so far rather than
    throwing the work away.

    ortools is imported at call time, here and everywhere in this file, so the
    module's import surface stays inert on a box that has no solver.
    """
    from ortools.sat.python import cp_model as cp_sat

    class _Callback(cp_sat.CpSolverSolutionCallback):
        def on_solution_callback(self):
            if on_progress is not None:
                on_progress({"phase": phase,
                             "objective": self.objective_value,
                             "bound": self.best_objective_bound})
            if should_cancel is not None and should_cancel():
                self.stop_search()

    solver = cp_sat.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, float(time_limit))
    solver.parameters.num_workers = int(num_workers)
    solver.parameters.random_seed = int(seed)
    callback = _Callback() if (on_progress or should_cancel) else None
    status = (solver.solve(cp_model, callback) if callback is not None
              else solver.solve(cp_model))
    return solver, status


# How much a solve status claims, strongest first. Anything unlisted claims
# least of all: a plan exists (the caller only reaches ``_weaker`` on success)
# but nothing about it is proven.
_STRENGTH = ("OPTIMAL", "FEASIBLE")


def _weaker(a: str, b: str) -> str:
    """The status that claims LESS of the two.

    A lexicographic solve is only as proven as its first phase: the headline
    number is the total, and phase 1 owns it. Phase 2 proving the best spread
    *at* an unproven total says nothing about the total.
    """
    rank = {name: i for i, name in enumerate(_STRENGTH)}
    fallback = len(_STRENGTH)
    return a if rank.get(a, fallback) >= rank.get(b, fallback) else b


def _hint(cp_model, variables, built, solver, days, roster, released) -> None:
    """Warm-start phase 2 from phase 1's plan.

    Phase 1's solution satisfies phase 2's added constraint by construction
    (``sum D <= T*`` with T* read off that very solution), so it is always a
    legal starting point — phase 2 can only improve the spread from there, never
    lose the total.

    Hinted through ortools' own ``add_hint`` rather than
    ``CPModel.solve(initial_solution=...)``: the pyjobshop route would hint only
    the variables pyjobshop knows about, which is precisely not the roster, the
    release or the late-day variables this engine's answer lives in.
    """
    cp_model.clear_hints()
    for task_idx in range(built.data.num_tasks):
        task_var = variables.task_vars[task_idx]
        cp_model.add_hint(task_var.start, solver.value(task_var.start))
        cp_model.add_hint(task_var.end, solver.value(task_var.end))
    for mode_var in variables.mode_vars:
        cp_model.add_hint(mode_var, solver.value(mode_var))
    for var in roster.x.values():
        cp_model.add_hint(var, solver.value(var))
    for var in released.values():
        cp_model.add_hint(var, solver.value(var))
    for late in days.values():
        cp_model.add_hint(late.true, solver.value(late.true))
        cp_model.add_hint(late.capped, solver.value(late.capped))


# --------------------------------------------------------------------------- #
# Reading the answer back
# --------------------------------------------------------------------------- #

def _read_back(cp, built, shifts, roster, released, days, solver, plan_start,
               spread, lower_bound, stats, status_name) -> Solved:
    from pyjobshop.Solution import ScheduledTask, Solution

    task_windows, processing, machine_of, quantities = {}, {}, {}, {}
    for key, task_idx in built.task_of.items():
        task_var = cp.variables.task_vars[task_idx]
        task_windows[key] = (solver.value(task_var.start),
                             solver.value(task_var.end))
        processing[key] = solver.value(task_var.processing)
        quantities[key] = built.job_by_key[key[0]].qty_for(key[1])
        for mid in sorted(built.machine_res_order):
            assign = cp.variables.assign_vars.get(
                (task_idx, built.machine_res_index(mid)))
            if assign is not None and solver.value(assign.present):
                machine_of[key] = mid

    # pyjobshop's own Solution, rebuilt from the same solver — it is what gives
    # the genome its per-job completion (max over the job's present tasks) by
    # pyjobshop's definition rather than a second one invented here.
    scheduled = []
    for task_idx in range(built.data.num_tasks):
        task_var = cp.variables.task_vars[task_idx]
        chosen, resources = 0, []
        for mode_idx in built.data.task2modes(task_idx):
            if solver.value(cp.variables.mode_vars[mode_idx]):
                chosen = mode_idx
                resources = built.data.modes[mode_idx].resources
                break
        scheduled.append(ScheduledTask(
            chosen, resources,
            solver.value(task_var.start), solver.value(task_var.end),
            solver.value(task_var.idle), solver.value(task_var.breaks),
            present=bool(solver.value(task_var.present))))

    result = _Result(Solution(built.data, scheduled))

    # Read off the COMPLETIONS, not off ``late.true``. Under phase 2 an order
    # past the cap has a flat square, so its ``true`` variable is free to float
    # anywhere the budget allows and would over-report. The schedule cannot.
    solved_days = _days_late_by_job(built, result)
    stats["capped_total_late_days"] = float(
        sum(solver.value(late.capped) for late in days.values()))
    completion = {
        job.key: plan_start + timedelta(
            minutes=result.best.jobs[built.job_of[job.key]].end)
        for job in built.jobs
    }

    g = genome.from_solution(
        result, built,
        _Resolved({k: solver.value(v) for k, v in roster.x.items()}),
        {k: solver.value(v) for k, v in released.items()},
        plan_start)

    return Solved(True, status_name, g, float(sum(solved_days.values())),
                  spread, lower_bound, stats, shifts, completion,
                  days_late=solved_days, windows=task_windows,
                  machine_of=machine_of, processing=processing,
                  quantities=quantities)


@dataclass
class _Result:
    """The two attributes ``genome.from_solution`` reads off a pyjobshop
    ``Result``. Built here rather than taken from ``CPModel.solve`` because this
    module runs its own solver — see the module docstring."""

    best: object


@dataclass
class _Resolved:
    """``rules.Roster`` with every variable already resolved to a plain int.

    ``genome.from_solution`` refuses a raw ``BoolVar`` on purpose (every object
    is truthy, so accepting one would silently roster every physically-possible
    pairing). This is the resolution it demands, done in the one place that has
    a solver."""

    x: dict


def _days_late_by_job(built, result) -> dict:
    """``{job key: true days late}``, UNCAPPED, from the solved completions.

    The headline number, so it is derived from the schedule rather than read off
    a model variable: ``Lateness.true`` is only pinned to its lower bound while
    it is in the objective, and phase 2 leaves an order past the cap free to
    float. The completion cannot float — it is what the shop will actually do.
    """
    out = {}
    for job in built.jobs:
        if job.key not in built.dated_jobs:
            continue
        job_idx = built.job_of[job.key]
        due = built.data.jobs[job_idx].due_date
        end = result.best.jobs[job_idx].end
        out[job.key] = max(0, -(-(end - due) // 1440))  # ceil for positive gaps
    return out


def _base_stats(cp_model, built, shifts, days, skipped) -> dict:
    """Model size, for Task 1's tractability spike, plus what the book skipped.

    Read off the raw proto so the numbers are the SOLVER's own count and not an
    estimate: a boolean is a variable whose domain is exactly ``[0, 1]``. Taken
    after every rule and the objective have been added, or it would measure a
    model nobody solves.
    """
    proto = cp_model.proto
    return {
        "tasks": built.data.num_tasks,
        "jobs": built.data.num_jobs,
        "dated_jobs": len(days),
        "shifts": len(shifts),
        "skipped_item_codes": list(skipped),
        "setup_free_modes": len(built.setup_free_modes),
        "variables": len(proto.variables),
        "booleans": sum(1 for v in proto.variables
                        if list(v.domain) == [0, 1]),
        "constraints": len(proto.constraints),
    }
