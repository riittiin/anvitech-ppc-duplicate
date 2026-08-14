"""The shop rules PyJobShop cannot express, added to the raw CpModel underneath.

Rule 1 needs booleans per (operator, machine, shift). Rule 3 needs a VARIABLE lag
where StartBeforeStart takes a constant. Rule 4 needs a literal that says "t2 runs
directly after t1 on this machine".

Establishing why this file has to exist at all, rather than another few lines of
model.py: linking the roster to the work means saying "this machine does not work
during this shift, unless somebody is on it", and a break that DEPENDS ON A
DECISION is impossible in PyJobShop's own vocabulary. It pre-computes every
possible break duration per mode as a discrete choice keyed on start-time domains
(``Variables.BreakVar``, one per equivalence class of start time), so the break
set has to be known before the search begins. "This shift is unstaffed" is
exactly a thing the search decides.

WORKER-ONLY. This module imports ortools and pyjobshop, both of which are
deliberately absent on Render — nothing in the replay path (``__init__``,
``domain``, ``windows``, ``genome``, ``decode``, ``report``) may import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cp_engine import windows
# ONE definition of how a frozen row is read and which of two rows for one
# operation wins. ``decode`` is a replay-path module (no pyjobshop at any depth),
# so importing it here is safe in the direction that matters — and its own
# docstring says a later layer must import these rather than keep a third copy.
from cp_engine.decode import pin_rank, prev_start_key, row_value  # noqa: F401
from cp_engine.domain import INSPECTION, MACHINING, MANUAL

# pyjobshop's own ceiling for a time variable. Imported rather than restated so
# an upper bound written here can never sit below one pyjobshop already allowed.
from pyjobshop.constants import MAX_VALUE

# The kinds that hand pieces over gradually. OS is a vendor block and DISPATCH a
# milestone; neither releases anything until it is done. Note this reading of
# ``kind`` is SAFE where the setup's was not: it separates in-house work from
# OS/DISPATCH, and those two are decided by ``domain._is_os`` / ``is_dispatch``,
# never off a step's first machine option. All three in-house kinds answer the
# same way, so a routing written ``MD1/CNC1`` cannot change the answer.
_INHOUSE = (MACHINING, MANUAL, INSPECTION)


@dataclass
class Roster:
    """Who is on which machine, per shift.

    ``x`` holds a variable only for a pairing that is PHYSICALLY POSSIBLE — the
    machine is on the operator's Settings list, the shift is his shift, and he is
    not away. An impossible pairing is therefore absent from the model rather
    than forbidden by a constraint, which is both smaller and impossible to get
    wrong by forgetting to add one.
    """

    x: dict = field(default_factory=dict)        # (operator, machine, shift idx) -> Bool
    staffed: dict = field(default_factory=dict)  # (machine, shift idx) -> Bool


def add_roster(cp_model, variables, built, shop, *,
               hold_across_unmanned_shift: bool) -> Roster:
    """Rule 1: one operator mans exactly one machine for a whole shift.

    He may change machine only at the next shift, and only to a machine on his
    Settings machine list. **Binds CNC/VMC only** — helpers and inspectors
    physically walk between manual and inspection stations, and rostering them
    for a whole shift would delete capacity that really exists.

    Args:
        cp_model: the raw ``ortools.sat.python.cp_model.CpModel``, i.e.
            ``pyjobshop.solvers.ortools.CPModel.CPModel(...).model``.
        variables: that CPModel's ``.variables``.
        built: ``cp_engine.model.Built``.
        shop: ``cp_engine.domain.Shop`` — the Settings operator table.
        hold_across_unmanned_shift: which encoding of Rule 2's "may span an
            unmanned shift" clause to link the roster to the work with. See
            ``_link_work_to_roster``. Must match the flag ``built`` was built
            for; a mismatch raises rather than quietly building E2 constraints on
            a model that cannot satisfy them.
    """
    if bool(hold_across_unmanned_shift) != bool(built.hold_across_unmanned_shift):
        raise ValueError(
            "hold_across_unmanned_shift="
            f"{hold_across_unmanned_shift} but the model was built for "
            f"{built.hold_across_unmanned_shift}. E2 needs allow_idle on the "
            "machining tasks, which is frozen into ProblemData and cannot be "
            "relaxed here — pass the same flag to model.build().")

    roster = Roster()
    for mid in sorted(mid for mid in built.machine_res_order
                      if mid in shop.machining_ids):
        machine = shop.machines[mid]
        for shift in built.shifts:
            if not _machine_runs(machine, shift):
                continue                      # already a break for this machine
            here = []
            for operator in sorted(shop.operators, key=lambda o: o.name):
                if mid not in (getattr(operator, "machines", None) or ()):
                    continue                  # qualification == the Settings list
                if windows.operator_shift(operator) != shift.shift:
                    continue
                if _absent(shop, operator.name, shift, built):
                    continue
                var = cp_model.new_bool_var(
                    f"x_{operator.name}_{mid}_{shift.index}")
                roster.x[(operator.name, mid, shift.index)] = var
                here.append(var)

            flag = cp_model.new_bool_var(f"staffed_{mid}_{shift.index}")
            roster.staffed[(mid, shift.index)] = flag
            # One person per machine per shift, and the flag IS that person: an
            # equality, not an implication, so "staffed" can never be true with
            # nobody on it.
            if here:
                cp_model.add(sum(here) == flag)
            else:
                cp_model.add(flag == 0)

    # Rule 1 itself: nobody is on two machines in the same shift. He may change
    # machine at the next shift, which is why this groups on (person, shift) and
    # not on the person alone.
    #
    # Since _reserve_rostered_operators consumes a rostered man's capacity-1
    # renewable for the whole shift, this is now REDUNDANT — measured, not
    # assumed: removing it fails no test. It is kept because it is the direct
    # statement of the rule, and because a clause propagates where a cumulative
    # has to reason. If it is ever removed, Rule 1 stops being written down
    # anywhere and survives only as a side effect of the bench-work fix.
    per_person: dict = {}
    for (name, _mid, shift_idx), var in roster.x.items():
        per_person.setdefault((name, shift_idx), []).append(var)
    for _key, group in sorted(per_person.items()):
        if len(group) > 1:
            cp_model.add_at_most_one(group)

    _reserve_rostered_operators(cp_model, variables, built, roster)
    _link_work_to_roster(cp_model, variables, built, roster.staffed,
                         hold=hold_across_unmanned_shift)
    return roster


def _reserve_rostered_operators(cp_model, variables, built, roster):
    """A man on a CNC is on that CNC — he is not also at a bench.

    Rule 1 rosters CNC/VMC only, but that scopes which MACHINES get a roster; it
    never licensed a rostered man to be booked somewhere else in the same shift.
    One person legitimately spans kinds — the live 2026-08-07 case ran manual
    stations AND CNC4 — and every operator, machining or not, is a capacity-1
    renewable in the model that manual and inspection modes book. Without this,
    the published roster is not merely noisy but WRONG: it names a man on a CNC
    while the schedule has him at MD1.

    So the roster boolean CONSUMES that operator's own renewable for the whole
    shift, as an optional interval present iff he is rostered. It goes in a
    second cumulative of its own for the same reason E1 needs a second
    no-overlap: CP-SAT fixes a cumulative's interval list when it is created, and
    PyJobShop already built one per renewable. ``res2assign`` and ``res2demand``
    are walked in the same order PyJobShop's own ``_renewable_capacity`` walks
    them, which is what keeps interval i paired with demand i.
    """
    by_operator: dict = {}
    for (name, mid, shift_idx), var in sorted(roster.x.items()):
        shift = built.shifts[shift_idx]
        by_operator.setdefault(name, []).append(
            cp_model.new_optional_interval_var(
                shift.start, shift.minutes, shift.end, var,
                f"mans_{name}_{mid}_{shift_idx}"))

    for name, manning in sorted(by_operator.items()):
        res_idx = built.operator_res_index(name)
        booked = [assign.interval for assign in variables.res2assign(res_idx)]
        demands = list(variables.res2demand(res_idx))
        cp_model.add_cumulative(booked + manning,
                                demands + [1] * len(manning), 1)


# --------------------------------------------------------------------------- #
# Linking the roster to the work — the two encodings
# --------------------------------------------------------------------------- #

def _link_work_to_roster(cp_model, variables, built, staffed, *, hold: bool):
    """Work on a machining machine requires that machine to be staffed.

    E1 (hold=False) — dark-shift blocking. A shift with nobody on it BLOCKS the
    machine outright. Cheap (|M_c| x |S| intervals, no per-task variables) and
    restrictive: an operation may not SPAN an unstaffed shift, which Rule 2
    permits. It errs toward under-claiming capacity, so a plan built this way is
    always runnable.

    E2 (hold=True) — exact. Per (task, machine, shift) the processing minutes in
    that shift are a variable, capped at zero when the machine is unstaffed, so
    the part is HELD across an unmanned shift instead of being forbidden from
    spanning it.

    Which one ships is a measurement (spec §5.1, Task 1), not a preference, and
    both stay correct until it is made.
    """
    if not hold:
        _block_unstaffed_shifts(cp_model, variables, built, staffed)
        return
    _work_only_in_staffed_shifts(cp_model, variables, built, staffed)


def _block_unstaffed_shifts(cp_model, variables, built, staffed):
    """E1. An optional interval covering the shift, present iff unstaffed.

    It goes into a SECOND no-overlap of its own, alongside this machine's task
    intervals. CP-SAT fixes a NoOverlap's interval list when the constraint is
    created and there is no way to append to the one PyJobShop already built, so
    the choice is a second constraint or none. Two no-overlaps saying compatible
    things about the same intervals is redundant, not conflicting.
    """
    by_machine: dict = {}
    for (mid, shift_idx), flag in sorted(staffed.items()):
        shift = built.shifts[shift_idx]
        by_machine.setdefault(mid, []).append(
            cp_model.new_optional_interval_var(
                shift.start, shift.minutes, shift.end, flag.negated(),
                f"dark_{mid}_{shift_idx}"))

    for mid, dark in sorted(by_machine.items()):
        work = [assign.interval
                for assign in variables.res2assign(built.machine_res_index(mid))]
        cp_model.add_no_overlap(work + dark)


def _work_only_in_staffed_shifts(cp_model, variables, built, staffed):
    """E2. The processing minutes of a rostered machine's work, shift by shift.

    ``w[t,m,s] <= overlap(t, s)`` and ``w[t,m,s] <= len(s) * staffed[m,s]``, and
    the w's over a machine must cover the task's whole processing time when the
    task is assigned to it. The interval may then stretch across an unmanned
    shift — the machine is held, and that time is the task's ``idle``, which is
    why E2 needs ``allow_idle`` on those tasks and E1 does not.

    Driven, like E1, off the ROSTERED MACHINE and every task that can run on it —
    never off the tasks' kinds. A step's kind is read from its first machine
    option, so a routing written ``MD1/CNC1`` is a manual-kind step that can land
    on a CNC; keyed on kind, E2 would leave it entirely unconstrained while E1
    caught it, and the flag would then decide WHICH WORK RULE 1 COVERS instead of
    only how a part may span a dark shift.

    Conversely a machine the roster does not cover (MD1 in ``CNC1/MD1``) gets no
    constraint at all here, or its empty w list would read as "cover your
    processing from nothing" and forbid a legal assignment.
    """
    for mid, res_idx in sorted((mid, built.machine_res_index(mid))
                               for mid in {m for (m, _s) in staffed}):
        for task_idx in sorted(_tasks_on(built, res_idx)):
            assign = variables.assign_vars.get((task_idx, res_idx))
            if assign is None:
                continue
            task_var = variables.task_vars[task_idx]
            work = []
            for shift in _shifts_in_window(built, task_idx):
                flag = staffed.get((mid, shift.index))
                if flag is None:
                    continue      # a break for this machine: no work either way
                overlap = _overlap_minutes(cp_model, built, task_var,
                                           task_idx, shift)
                minutes = cp_model.new_int_var(
                    0, shift.minutes, f"w_{task_idx}_{mid}_{shift.index}")
                cp_model.add(minutes <= overlap)
                cp_model.add(minutes <= shift.minutes * flag)
                work.append(minutes)
            built.shift_work.setdefault(task_idx, []).extend(work)
            # An empty ``work`` is not a special case: the machine is open in no
            # shift this task could touch, and ``0 >= processing`` is false for
            # every real step, so the same line rules the assignment out.
            cp_model.add(
                sum(work) >= task_var.processing
            ).only_enforce_if(assign.present)


def _tasks_on(built, res_idx) -> set:
    """Every task with a mode on this resource — the same set PyJobShop's own
    machine no-overlap covers, so E1 and E2 can never disagree about which work a
    rostered machine is answerable for."""
    return {built.data.modes[mode_idx].task
            for mode_idx in built.data.resource2modes(res_idx)}


def _overlap_minutes(cp_model, built, task_var, task_idx, shift):
    """``min(end_t, end_s) - max(start_t, start_s)``, clipped at 0.

    A variable, not a constant: how much of a shift a task covers is exactly what
    the solver is deciding.

    Cached on ``(task, shift)``, which is everything the value depends on. A task
    with k candidate machines asks for this k times, and three IntVars plus three
    equalities per ask — building them per machine would inflate E2 k-fold in
    exactly the number Task 1 measures E2 against E1 by, and bias that choice for
    no modelling reason.
    """
    cached = built.shift_overlap.get((task_idx, shift.index))
    if cached is not None:
        return cached

    name = f"{task_idx}_{shift.index}"
    lower = cp_model.new_int_var(shift.start, MAX_VALUE, f"lo_{name}")
    cp_model.add_max_equality(lower, [task_var.start, shift.start])

    upper = cp_model.new_int_var(0, shift.end, f"hi_{name}")
    cp_model.add_min_equality(upper, [task_var.end, shift.end])

    overlap = cp_model.new_int_var(0, shift.minutes, f"ov_{name}")
    cp_model.add_max_equality(overlap, [upper - lower, 0])
    built.shift_overlap[(task_idx, shift.index)] = overlap
    return overlap


# --------------------------------------------------------------------------- #
# Rule 3 — the successor starts once k whole pieces have cleared
# --------------------------------------------------------------------------- #

def add_release(cp_model, variables, built, config) -> dict:
    """Rule 3: the successor starts once ``ceil(p x qty)`` pieces have cleared.

    ``k_j`` is an integer DECISION — the pieces that must clear — so the solver
    picks the overlap PER JOB instead of the whole book inheriting one tuned
    number (spec §3). Whole pieces by construction: k is an integer, so a release
    on 5.6 pieces cannot be expressed. One k per job, not per step boundary: the
    overlap is a property of how a batch is handed down its routing, and the
    previous engine's one global percentage is what this replaces.

    **Which quantity k is counted in.** Its domain is ``1..min(pieces still owed
    by any step it governs)`` — ``Job.qty_for``, the per-step WIP remainder, NOT
    the batch's ordered ``job.qty``. On a clean book the two are equal; on a
    re-plan with work in progress they are not, and the difference is not
    cosmetic (see the domain comment below). The contract this buys every later
    layer: **k never exceeds what any step of its job still owes**, so a decoder
    can read it straight against a step's remaining qty.

    Returns ``{job key: k IntVar}`` for every job with at least one overlapping
    pair. A job with none — a single step, or nothing but OS — is absent.

    **k is a variable with no decision in it, and that is now measured rather
    than suspected** (Task 6, 2026-08-14). k appears ONLY in the two lower bounds
    below, and the right-hand side of each is monotonically INCREASING in k. A
    successor is never obliged to start at its lower bound, so every schedule
    legal at any k is also legal at k = 1: the feasible set at k = 1 contains
    every other one, and NO objective — makespan, tardiness, or the fairness pair
    Task 6 ships — can make k > 1 strictly pay. The earlier note here guessed
    that a tardiness objective might make the value a real choice by letting a
    job hold a machine back for a more urgent one; it cannot, because holding
    back is already available for free.

    So under this engine's objective Rule 3 is, in effect, **"release after one
    piece" — maximum overlap, always**. That is a real statement about the
    owner's overlap rule, not a defect: this engine does not tune a global
    overlap percentage the way the incumbent's contest did, it always takes the
    loosest legal release and lets the machine calendars, Rule 1 and the
    objective decide the rest.

    Its DOMAIN is still pinned by tests (widen it to 0 and two fail; size it on
    ``job.qty`` and one does) because the domain is what fixes what k MEANS to a
    decoder, and because the top of a wrong domain is where the harm is.

    ⚠ **k is UNDER-DETERMINED above its lower bound.** Because it is in no
    objective, any k consistent with the solved starts is an equally optimal
    answer — on a schedule with slack, 1 and 100 are both legal. CP-SAT's
    presolve fixes it to its lower bound today (measured across five seeds), so
    the genome carries 1, but nothing in this model *requires* that. A decoder
    must therefore treat ``cp_overlap_of`` as "at least this many pieces had
    cleared", never as "the release this schedule needs", and a drift check is
    what proves the replay matches the solve.

    **The release is an APPROXIMATION, and knowingly so.** The shop's rule is
    WORKED minutes (the sibling greedy engine's ``release`` module:
    "an overnight gap must not release pieces that were never cut"), while the
    bound below is WALL CLOCK, and it is wrong in the PESSIMISTIC direction: a
    break falling late in the predecessor makes ``a.end`` later than the moment
    the last k pieces really needed, so the successor waits a little longer than
    the shop would.

    **There used to be a second, HEAD bound**
    ``b.start >= a.start + setup + k x cycle`` — the other side of the same rule,
    optimistic where this one is pessimistic. It was REMOVED (owner-authorized,
    2026-08-14) because it can never bind: ``a.end - a.start >= processing =
    setup + cutting``, so ``tail - head = (a.end - a.start - setup) - cutting >=
    0`` always, and the tail bound provably dominates. Removing it also retired
    ``_setup_charged``, 28 lines that read the assignment and mode variables back
    out of the model to work out what setup the predecessor would really pay —
    the subtlest code in this package, feeding an inert constraint. Measured
    before removal on three tardy books x two frozen states: identical
    ``total_late_days`` and ``spread``, -1.2% model size. **It is restorable from
    git, and the condition that would make it live again is anything that NARROWS
    a task's interval** — a latest_end, a fixed duration, a no-idle regime — since
    the dominance argument rests entirely on the interval being at least as long
    as the processing time.

    A later task's decoder computes the exact worked-minute release and a drift
    check measures what this cost. If the drift is material, tighten this —
    never loosen the decoder.

    The bound is written in SCALED integer arithmetic (multiplied through by
    ``qty``) rather than with a per-piece minute constant. Rounding a sub-minute
    cycle to whole minutes would otherwise make it inconsistent with the duration
    ``model.build`` charged — ``qty x round(cycle)`` is not ``round(qty x
    cycle)`` — in the direction that releases pieces before they exist.

    ``config`` is unused since the head bound went (it was read only for
    ``setup_time_min``) and is kept so every ``add_*`` in this module takes the
    same four arguments.
    """
    out: dict = {}
    for job in built.jobs:
        pairs = _overlapping_pairs(built, job)
        if not pairs:
            continue
        # k is ONE decision for the whole job, so its domain has to be legal for
        # every step it governs: the SMALLEST number of pieces any of those steps
        # still owes. Sizing it on ``job.qty`` instead is wrong the moment a
        # predecessor is part-finished — ``qty_for`` reads the WIP remainder, so
        # k could exceed the pieces that step has left, ``(qty - k)`` would go
        # negative, and the tail bound would demand the successor start LATER
        # than fully sequential. Rule 3 makes no such statement.
        #
        # It also fixes what k MEANS for the decoder: k pieces, never more than
        # any step of this job owes, so the number can be read against any step's
        # remaining qty without a second clamp. A quantity that reaches the
        # scheduler is derived at batch level, and a model and a decoder counting
        # one number in two different quantities is this project's opening
        # cautionary tale (2026-08-11).
        k = cp_model.new_int_var(1, min(qty for _a, _b, qty, _c in pairs),
                                 f"k_{job.key}")
        out[job.key] = k
        for a_idx, b_idx, qty, cutting in pairs:
            a = variables.task_vars[a_idx]
            b = variables.task_vars[b_idx]
            cp_model.add(qty * b.start >= qty * a.end - (qty - k) * cutting)
    return out


def _overlapping_pairs(built, job) -> list:
    """``[(predecessor task, successor task, pieces owed, cutting minutes)]``.

    A step with no task is stepped OVER, not carried: ``model.build`` skips a
    DISPATCH milestone, a step already finished (``qty_for`` is 0) and a step
    with no machine, and chains its precedence past them. Keeping the absent step
    as the predecessor would look its task up and raise — the brief's version
    did exactly that, and a mid-routing DISPATCH is enough to reach it.
    """
    pairs, prev_op = [], None
    for op in job.ops:
        idx = built.task_of.get((job.key, op.seq))
        if idx is None:
            continue
        if prev_op is not None and _overlaps(prev_op, op):
            qty = job.qty_for(prev_op.seq)
            if qty > 0:
                pairs.append((built.task_of[(job.key, prev_op.seq)], idx, qty,
                              max(1, int(round(qty * prev_op.cycle_min)))))
        prev_op = op
    return pairs


def _overlaps(prev, nxt) -> bool:
    """May ``nxt`` start before ``prev`` has finished?

    The same predicate as the sibling greedy engine's ``release.overlaps``,
    rewritten here because this package stands alone: both steps in-house,
    and the predecessor
    actually cutting. A step with no cycle time produces nothing gradually, so
    its successor waits for it to complete (RULES.md:127).
    """
    if prev.kind not in _INHOUSE or nxt.kind not in _INHOUSE:
        return False
    return prev.cycle_min > 0.0


# --------------------------------------------------------------------------- #
# Rule 4 — 90 minutes on a CNC/VMC whenever the part or the side changes
# --------------------------------------------------------------------------- #

def add_setup_credit(cp_model, variables, built, config) -> None:
    """Rule 4: no setup when the machine's previous job was the same
    (item, process). Same part, same side, back to back — the fixture is already
    on.

    **The encoding is INVERTED, and that inversion is the whole point.** Charging
    setup as a sequence-dependent cost needs one entry per ordered pair of tasks
    per machine — 18,944 of them on the owner's book — encoded as an O(n^2)
    circuit, which consumed an entire 90-second solve and returned no feasible
    solution at all (``scripts/tardiness_bound.py`` around line 384). So 90
    minutes is already IN every machining mode's duration (``model.py``), and
    this grants the CREDIT only where Rule 4 says none is owed.

    **The credit is a MODE, not a subtracted variable.** PyJobShop posts
    ``task.processing == mode.duration`` under the selected mode, so the planned
    ``processing == duration - credit`` is infeasible for any nonzero credit.
    ``model._add_setup_free_modes`` therefore builds a second mode on the same
    machine carrying the cutting time alone, and this function is what makes
    selecting it legal.

    **"Directly after" is NOT read from a sequence-variable arc.** The plan said
    to activate ``variables.sequence_vars[res]`` and read ``arcs[(a, b)]``. Two
    things are wrong with that, both verified rather than reasoned: (1)
    ``Constraints._circuit_constraints`` has ALREADY run by the time this is
    called (``CPModel.__init__`` builds the constraints eagerly), so a sequence
    activated now gets its arc literals and no circuit, no arc-to-presence link
    and no ``end <= start`` — the arcs are FREE BOOLEANS and the solver sets them
    to whatever wins it a credit; (2) posting the circuit here to fix that
    reintroduces exactly the O(n^2) structure the inversion exists to avoid.

    Instead: ``b`` runs directly after ``a`` on machine ``m`` if both are on
    ``m`` and ``b`` starts exactly when ``a`` ends. Every task is at least a
    minute long, so nothing can hide in a zero-length gap — the condition is
    SUFFICIENT for adjacency, which is the direction that matters (a credit
    granted wrongly claims capacity the shop does not have). It is not necessary:
    ``a`` ending at 19:00 and ``b`` starting at 08:00 leaves the fixture on but
    earns nothing here, so the model over-estimates a little and stays runnable.
    The cost is one boolean per same-part pair per machine, not per pair of
    tasks.

    ``setup_mode != "credit"`` is a no-op by construction: ``model.build`` then
    creates no setup-free modes, so every changeover pays. Conservative
    (durations are over-estimated, so the plan stays runnable) but NOT Rule 4 as
    written; it ships only on the owner's say-so.
    """
    # Stamped FIRST, and stamped even when there is nothing to link: it records
    # "these credit modes are constrained", and a model with none is already
    # safe. An unlinked model is not merely unchecked — every member of every
    # same-part group may take its setup-free mode unconditionally, so the plan
    # claims 90 minutes of CNC capacity per affected task with no exception, no
    # failing test and no report row anywhere. An invariant that is CHECKED beats
    # one that is merely intended, so the caller gets a boolean to assert on.
    built.setup_credit_linked = True
    if not built.setup_free_modes:
        return

    # The sameness is model.build's, not a second opinion formed here: it built
    # one setup-free mode per member of each group, and re-deriving "same part,
    # same side" would let the two drift — a credit granted for a sameness the
    # duration was never discounted for is 90 minutes of capacity the shop does
    # not have.
    for (res_idx, _sig), members in sorted(built.setup_groups.items()):
        for b_idx in members:
            warm = [_directly_after(cp_model, variables, res_idx, a_idx, b_idx)
                    for a_idx in members if a_idx != b_idx]
            free = variables.mode_vars[built.setup_free_modes[(b_idx, res_idx)]]
            cp_model.add(free <= sum(warm) if warm else free == 0)


def _directly_after(cp_model, variables, res_idx: int, a_idx: int, b_idx: int):
    """A literal that, when true, means ``b`` runs on this machine immediately
    behind ``a``.

    A one-way implication on purpose. The solver may leave it false even when the
    two do abut — that only forfeits a credit, which is the safe direction — but
    it can never be true without both tasks really being on this machine with no
    time between them.

    One warm fixture cannot be spent twice: two tasks credited by the same ``a``
    would both have to start at ``a.end``, and this machine's no-overlap forbids
    that for any pair of tasks a minute long or more — which every task is
    (``model.build`` floors every duration at 1).
    """
    adjacent = cp_model.new_bool_var(f"after_{a_idx}_{b_idx}_{res_idx}")
    cp_model.add(adjacent <= variables.assign_vars[(a_idx, res_idx)].present)
    cp_model.add(adjacent <= variables.assign_vars[(b_idx, res_idx)].present)
    cp_model.add(
        variables.task_vars[b_idx].start == variables.task_vars[a_idx].end
    ).only_enforce_if(adjacent)
    return adjacent


# --------------------------------------------------------------------------- #
# Frozen in-progress work (spec §5.5)
#
# A FROZEN ROW PINS WHERE AND WHEN, NEVER HOW MUCH. That sentence is the whole
# of this section and it is written from a live escalation: on 2026-08-11 a
# director opened the Gantt for two clubbed SOs and found CNC FIRST SIDE running
# 88 pieces — ONE SO line's own remainder — while the other line's 281 were in no
# plan at all, though every later step still showed the full 535. It hit 4 of 58
# batches on the owner's real book. ``remaining_qty`` is therefore never read
# here; the quantity comes from ``Job.qty_for``, at BATCH level, which is the same
# expression ``model.build`` charges the duration from and ``Solved.op_qty``
# publishes. One definition, one quantity.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Pin:
    """One in-progress operation, already resolved against TODAY's book.

    ``resume_shift`` is the index of the first shift in which the pinned machine
    could pick the part back up — the shift the roster force below is written
    for. ``rank`` is ``decode.pin_rank``, so this layer and the replay decoder
    can never disagree about which of two rows for one operation wins.
    """

    job_key: str
    op_seq: int
    machine: str
    operator: str | None
    earliest_start: int
    resume_shift: int | None
    rank: tuple


def resolve_pins(frozen, jobs, shop, shifts, plan_start) -> tuple:
    """Frozen rows -> ``({(job key, op seq): Pin}, [reason])``, before the model.

    PURE, and deliberately run BEFORE ``model.build``: two of the four things a
    pin does — the resumed op's missing setup and its ``earliest_start`` — are
    per-MODE and per-TASK facts, frozen into ``ProblemData`` when the task is
    created. Nothing added to the raw CpModel afterwards can relax a duration.

    THE ACCOUNTING CLOSES. Every row that arrives is either a ``Pin`` or a
    reason, mirroring ``decode._apply_frozen``. A silently dropped constraint is
    a named recurring defect class in this repo: the plan looks perfectly
    well-formed while the part is planned on a machine it is not physically on.

    A pin the current book cannot honour is DROPPED, never forced. The
    alternative is a machine list of exactly one impossible machine, which takes
    the whole book out of the plan at the horizon — and the app's rule is that a
    live plan must never simply fail. Dropped rows fall through to ordinary
    scheduling and are reported.

    Rows are collapsed to ONE pin per (job, op): several clubbed SO lines can each
    be in progress on the same step, and an operation runs once, on one machine.
    """
    by_key = {job.key: job for job in jobs}
    horizon = shifts[-1].end if shifts else 0
    best: dict = {}
    reasons: list = []

    for row in frozen or ():
        key = row_value(row, "order_key", "job_key", "batch_id")
        seq = row_value(row, "op_seq", "process_seq")
        mid = row_value(row, "machine_id", "machine")
        if key is None or seq is None or mid is None:
            reasons.append(f"{_describe(row)}: the row names no job, step or "
                           "machine")
            continue
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            reasons.append(f"{_describe(row)}: step {seq!r} is not a number")
            continue
        key, mid = str(key), str(mid)
        job = by_key.get(key)
        if job is None:
            reasons.append(f"{_describe(row)}: no job {key!r} in this plan")
            continue
        op = next((o for o in job.ops if o.seq == seq), None)
        if op is None:
            reasons.append(f"{_describe(row)}: {key} has no step {seq} in its "
                           "routing")
            continue
        if mid not in op.machine_options:
            reasons.append(f"{_describe(row)}: {key} step {seq} no longer runs "
                           f"on {mid}")
            continue
        if job.qty_for(seq) <= 0:
            # No task is built for a step with nothing left to make, so there is
            # nothing to pin — and the quantity that decides that is the BATCH's.
            reasons.append(f"{_describe(row)}: {key} step {seq} has nothing left "
                           "to make")
            continue
        if not _staffable(shop, mid):
            reasons.append(f"{_describe(row)}: no operator in Settings is "
                           f"qualified for {mid}")
            continue
        floor = _earliest_start(row, plan_start)
        if horizon and floor >= horizon:
            reasons.append(f"{_describe(row)}: the previous plan starts it past "
                           "this horizon")
            continue

        who = row_value(row, "operator")
        pin = Pin(job_key=key, op_seq=seq, machine=mid,
                  operator=str(who) if who else None,
                  earliest_start=floor,
                  resume_shift=_resume_shift(shop, mid, floor, shifts),
                  rank=pin_rank(row, mid))
        current = best.get((key, seq))
        if current is None or pin.rank < current.rank:
            if current is not None:
                reasons.append(_supersede(current, pin))
            best[(key, seq)] = pin
        else:
            reasons.append(_supersede(pin, current))

    return best, reasons


def pin_frozen(cp_model, variables, built, roster, pins) -> tuple:
    """Force the model onto the pins: the MACHINE, and the PERSON for the shift
    the part is picked back up in. Returns ``(applied, [reason])``.

    The other two halves of §5.5 — ``earliest_start`` and no setup on resume —
    were built into ``ProblemData`` by ``model.build``; see ``resolve_pins``.

    **The machine is forced, the person is not always.** ``roster.x`` holds a
    variable only for a pairing that is PHYSICALLY POSSIBLE today — the machine
    is on that operator's SETTINGS list, it is his shift, and he is not away —
    so a pinned operator who has since had the machine taken off him has no
    variable to force at all. Forcing one would mean inventing it, and the model
    would come back INFEASIBLE for a book the shop can plainly run. That is
    exactly the live 2026-08-03 / 2026-08-07 defect from the other direction
    ("Sidhu Singe on CNC5"): re-pinning a planned operator without re-checking
    qualification froze him straight back onto a machine an admin had just taken
    away. So the MACHINE PIN IS PHYSICS AND STAYS; the PERSON is re-staffed from
    today's Settings and the row is reported.

    Two stale rows can also disagree about who is on one machine in one shift, or
    put one person on two machines at once. Rule 1 forbids both, so forcing both
    would be INFEASIBLE — a live plan must never simply fail. The earliest-started
    row wins (``decode.pin_rank``, the decoder's own tie-break, so the two can
    never disagree) and the loser's PERSON is reported, keeping its machine.

    **A BENCH PIN IS WHERE ONLY — the WHO half is not applied at all.** Rule 1
    rosters CNC/VMC (``add_roster`` iterates ``shop.machining_ids``) because
    helpers and inspectors physically walk between manual and inspection
    stations, so ``roster.x`` holds no entry for MD/MW/MPK/MI/CMM/DTC and there
    is no boolean here to force. A bench mode carries its own operator
    (``model._add_modes``) and the solver stays FREE to pick any qualified
    person for it; the decoder staffs benches per operation for the same reason.
    That is a property of Rule 1, not a fault in anybody's Settings row, so it is
    NOT reported: until 2026-08-14 the missing entry fell into the message below
    and printed "N is not on MD1 for that shift under today's Settings" about a
    helper who IS on MD1 — a cause nobody checked (CLAUDE.md 2026-08-09), and one
    that would have DOMINATED the owner-facing list, since ``engine.freeze``
    freezes every in-progress non-OS step and the owner's routings are largely
    bench steps. The row is still counted in ``applied``: its machine pin landed,
    which is the whole of what a bench pin claims.

    **Only the resume shift is forced, not every shift the op may span.** Which
    shifts an operation really occupies is a decision — the model has no variable
    for it under E1 at all — and forcing the pinned person across a span the
    solver has not chosen would both over-book him and forbid the shift handoff
    the shop actually runs (RULES.md: a man may change machine at the next shift).
    So the force is written for the ONE shift in which the machine can pick the
    part back up, which is the shift the pin is a physical statement about.
    """
    if dict(pins or {}) != built.pins:
        # Same guard, same reason, as add_roster's hold_across_unmanned_shift
        # check nine lines up: model.build baked the setup credit and the
        # earliest_start of ONE pin set into ProblemData, and a second set forced
        # here would pin a machine whose mode still carries 90 minutes it does
        # not owe, or float a start the previous plan fixed.
        raise ValueError(
            "pin_frozen was given a different pin set from the one model.build "
            "was built for — the resumed setup and earliest_start are frozen "
            "into ProblemData and cannot be changed here. Pass the same dict.")

    applied = 0
    reasons: list = []
    on_machine: dict = {}        # (machine, shift) -> operator forced there
    on_operator: dict = {}       # (operator, shift) -> machine he was forced to
    # Which machines Rule 1 rosters at all, read off the roster ITSELF rather
    # than re-derived from shop.machining_ids — one definition, so the test below
    # can never disagree with the lookup it guards.
    rostered = {mid for mid, _shift in roster.staffed}

    for _key, pin in sorted(pins.items(), key=lambda kv: kv[1].rank):
        task_idx = built.task_of.get((pin.job_key, pin.op_seq))
        if task_idx is None:
            reasons.append(f"{_name(pin)}: this plan has no task for that step")
            continue
        res_idx = built.machine_res_order.get(pin.machine)
        assign = (variables.assign_vars.get((task_idx, res_idx))
                  if res_idx is not None else None)
        if assign is None:
            reasons.append(f"{_name(pin)}: {pin.machine} has no mode for that "
                           "step in this model")
            continue
        # A task selects exactly one mode and every mode names exactly one
        # machine, so forcing this assignment present forbids every other machine
        # without a constraint per alternative.
        cp_model.add(assign.present == 1)
        applied += 1

        if pin.operator is None or pin.resume_shift is None:
            continue
        if pin.machine not in rostered:
            # A BENCH. Rule 1 does not roster it, so the WHO half of this pin is
            # NOT APPLIED — the mode is free to pick any qualified person — and
            # that is not a Settings fault to report. See the docstring.
            continue
        var = roster.x.get((pin.operator, pin.machine, pin.resume_shift))
        if var is None:
            reasons.append(
                f"{_name(pin)}: {pin.operator} is not on {pin.machine} for that "
                "shift under today's Settings — the machine pin stands and the "
                "person is re-staffed")
            continue
        held = on_machine.get((pin.machine, pin.resume_shift))
        if held is not None and held != pin.operator:
            reasons.append(
                f"{_name(pin)}: another in-progress row already puts {held} on "
                f"{pin.machine} that shift, and one machine has one operator — "
                f"{pin.operator} is re-staffed")
            continue
        elsewhere = on_operator.get((pin.operator, pin.resume_shift))
        if elsewhere is not None and elsewhere != pin.machine:
            reasons.append(
                f"{_name(pin)}: {pin.operator} is already pinned to {elsewhere} "
                "that shift, and nobody mans two machines in one shift — he is "
                f"re-staffed off {pin.machine}")
            continue
        if held is None:
            cp_model.add(var == 1)
        on_machine[(pin.machine, pin.resume_shift)] = pin.operator
        on_operator[(pin.operator, pin.resume_shift)] = pin.machine

    return applied, reasons


def _earliest_start(row, plan_start) -> int:
    """The previous plan's start, in minutes from the plan clock, floored at 0.

    The past is past: an operation started yesterday resumes no earlier than the
    plan clock, never at a negative minute. Both spellings and both types —
    ``engine.freeze.compute_frozen_set`` emits an ISO STRING while an adapter may
    hand over a datetime. A row with no readable previous start gets NO floor
    rather than a guessed one; it still pins WHERE.
    """
    value = row_value(row, "prev_start", "start")
    when = value if isinstance(value, datetime) else None
    if when is None and value is not None:
        try:
            when = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            when = None
    if when is None or plan_start is None:
        return 0
    return max(0, int((when - plan_start).total_seconds() // 60))


def _resume_shift(shop, mid: str, floor: int, shifts) -> int | None:
    """The first shift at or after the floor in which this machine is open.

    Not "the shift the op runs in" — that is a decision. It is the earliest shift
    the shop could pick the part back up in, which is the shift a pin makes a
    physical statement about.
    """
    machine = (shop.machines or {}).get(mid)
    for shift in shifts or ():
        if shift.end <= floor:
            continue
        if machine is not None and not _machine_runs(machine, shift):
            continue
        return shift.index
    return None


def _staffable(shop, mid: str) -> bool:
    """Can anybody in Settings run this machine at all?

    A machining machine nobody can man is BLOCKED by Rule 1 (``staffed == 0``),
    and a bench nobody can man has no mode at all — forcing a pin onto either is
    an infeasible model rather than a plan. Qualification is EXACTLY the Settings
    machine list (2026-08-07: role is not a gate).
    """
    return any(mid in (getattr(o, "machines", None) or ())
               for o in shop.operators)


def _supersede(loser: Pin, kept: Pin) -> str:
    """Why a second row for ONE operation was not applied. CHECKED, not assumed:
    the same machine is a duplicate — the ordinary clubbed-SO-lines case — while a
    different machine is a real data conflict, and calling that "superseded"
    attributes a cause nobody checked (2026-08-09)."""
    if loser.machine == kept.machine:
        return (f"{_name(loser)}: another in-progress row already covers that "
                f"operation ON THE SAME MACHINE (clubbed SO lines on one batch "
                f"step); an operation runs once")
    return (f"{_name(loser)}: this row says {loser.machine} for an operation "
            f"another row puts on {kept.machine}, which one operation cannot be "
            f"— the earlier-started row ({kept.machine}) was kept")


def _name(pin: Pin) -> str:
    return f"{pin.job_key} step {pin.op_seq}"


def _describe(row) -> str:
    key = row_value(row, "order_key", "job_key", "batch_id", "so_no") or "?"
    seq = row_value(row, "op_seq", "process_seq")
    return f"{key} step {seq if seq is not None else '?'}"


# --------------------------------------------------------------------------- #
# Small shared predicates
# --------------------------------------------------------------------------- #

def _machine_runs(machine, shift) -> bool:
    """Does this machine work this shift at all?

    The same test ``windows.machine_breaks`` complements the calendar with, so a
    shift this returns False for is already a break and needs no roster: a
    single-shift station cannot be manned at night because it is not open at
    night, which is a different fact from nobody being available.
    """
    return bool(machine.is_two_shift()) or shift.shift == windows.FIRST


def _absent(shop, name: str, shift, built) -> bool:
    """Is this operator away for any part of this shift?

    Absences arrive as wall-clock blocks (``engine.book_store``'s
    ``{from_date, to_date}``, turned into 00:00 through 00:00-of-the-day-after)
    while a Shift is integer minutes, so the SHIFT is converted back to wall
    clock rather than the absence forward — one conversion, and it is then the
    same half-open overlap test (``away_from < shift_end and shift_start <
    away_to``) the sibling greedy engine's crew check applies, so the two can
    never disagree about who was away.
    """
    blocks = shop.absent.get(name) or ()
    if not blocks or built.plan_start is None:
        return False
    start = built.plan_start + timedelta(minutes=shift.start)
    end = built.plan_start + timedelta(minutes=shift.end)
    return any(away_from < end and start < away_to for away_from, away_to in blocks)


def _shifts_in_window(built, task_idx) -> list:
    """Only the shifts a task could possibly touch.

    This is the tightening E2's size rests on (spec §5.1: four constraints per
    (task, machine, shift) triple is tens of thousands otherwise). Today it bites
    only where a task carries a real window — frozen in-progress work, which pins
    ``earliest_start`` — and returns the whole horizon for everything else. That
    is honest rather than clever: the bound is read from the task PyJobShop
    actually built, so it tightens by itself as later layers narrow it, and can
    never claim a window the model does not have.
    """
    task = built.data.tasks[task_idx]
    lower = getattr(task, "earliest_start", 0) or 0
    upper = getattr(task, "latest_end", MAX_VALUE)
    return [s for s in built.shifts if s.end > lower and s.start < upper]
