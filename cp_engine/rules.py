"""The shop rules PyJobShop cannot express, added to the raw CpModel underneath.

Rule 1 needs booleans per (operator, machine, shift). Rule 3 needs a VARIABLE lag
where StartBeforeStart takes a constant. Rule 4 needs the sequence literal that
says "t2 runs directly after t1". Only Rule 1 lives here so far.

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
from datetime import timedelta

from cp_engine import windows

# pyjobshop's own ceiling for a time variable. Imported rather than restated so
# an upper bound written here can never sit below one pyjobshop already allowed.
from pyjobshop.constants import MAX_VALUE


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
