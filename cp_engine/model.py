"""The native PyJobShop layer: everything the library expresses well.

What is here: machines with calendar breaks, tasks, modes, the outsourcing pool,
and the precedences that do not depend on a decision. What is NOT here, and lives
in rules.py instead: Rule 1's roster, Rule 3's per-job overlap and Rule 4's setup
credit — the three things that need a variable where PyJobShop takes a constant.

WORKER-ONLY. This module imports pyjobshop, which is deliberately absent on
Render (see cp_engine/__init__.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from cp_engine import windows
from cp_engine.domain import DISPATCH, MACHINING, OUTSOURCED

# Enough parallel capacity that outsourcing never binds: a vendor takes every
# batch at once. The engine models an OS step as a flat 24x7 block with no
# operator and no calendar, and this is that.
_OS_HEADROOM = 1


@dataclass
class Built:
    """The model plus every index map the later layers address it by.

    PyJobShop's Task is not hashable and its Variables are keyed by INDEX, so
    the maps are the interface between this layer and rules/objective/genome.
    """

    m: object                       # pyjobshop.Model
    data: object                    # pyjobshop.ProblemData
    task_of: dict                   # (job_key, op_seq) -> task index
    job_of: dict                    # job_key -> job index
    # machine_res / operator_res hold the pyjobshop OBJECTS returned by
    # add_machine/add_renewable (add_mode needs the object, not an index) —
    # they are NOT index maps despite the name. Unhashable
    # (TypeError: unhashable type: 'Machine'), so they can never key a dict
    # or appear in a tuple used as one. The INDEX a later layer needs to
    # address Variables.assign_vars[(task_idx, resource_idx)] lives in
    # machine_res_order / operator_res_order below — use
    # machine_res_index()/operator_res_index(), never these two directly.
    machine_res: dict               # machine id -> pyjobshop Machine object
    operator_res: dict              # operator name -> pyjobshop Renewable object
    os_res: int
    shifts: list
    jobs: list
    dated_jobs: set                 # job keys that HAVE a delivery date
    machining_tasks: dict = field(default_factory=dict)   # task idx -> (job_key, op)
    setup_mode: str = "credit"

    # The wall clock the shift minutes are measured from. Carried so a later
    # layer can convert a Shift BACK to real time — operator absences arrive as
    # datetimes (engine.book_store stores {from_date, to_date}) and rules.py has
    # to test them against a shift, which is integer minutes.
    plan_start: datetime | None = None

    # Which encoding of Rule 2's "may span an unmanned shift" clause this data
    # was built FOR (spec §5.1). It is not merely a rules.py concern: E2 needs
    # allow_idle on the machining tasks (the held-but-unstaffed time is idle),
    # and allow_idle is frozen into ProblemData — Variables gives the idle var an
    # upper bound of 0 when it is False, and nothing downstream can relax it.
    # Recorded here so rules.add_roster can check the caller agrees rather than
    # silently building E2 constraints on a model that cannot satisfy them.
    hold_across_unmanned_shift: bool = True

    # Filled by later layers. Declared HERE so there is one definition of this
    # object's shape: rules.py populates shift_work (E2) and setup_credit
    # (Rule 4), and both address the model by index through the maps above.
    setup_credit: dict = field(default_factory=dict)   # task idx -> IntVar (Task 5)
    shift_work: dict = field(default_factory=dict)     # task idx -> [IntVar] (Task 4)
    # (task idx, shift idx) -> IntVar. The overlap of a task with a shift depends
    # on nothing else, so it is built once and shared by every candidate machine.
    shift_overlap: dict = field(default_factory=dict)
    job_by_key: dict = field(default_factory=dict)     # job key -> domain.Job
    machine_res_order: dict = field(default_factory=dict)   # machine id -> res idx
    operator_res_order: dict = field(default_factory=dict)  # operator name -> res idx

    def machine_res_index(self, mid: str) -> int:
        """Resource index of a machine. PyJobShop's Variables are keyed by
        index — assign_vars is (task_idx, resource_idx) — and machines are added
        first, so a machine's resource index is its position in machine_res."""
        return self.machine_res_order[mid]

    def operator_res_index(self, name: str) -> int:
        """Resource index of a manual/inspection operator. Resources are
        indexed machines first, then operators (in the same sorted-by-name
        order build() creates them in), then the OS pool — mirroring
        machine_res_index so the two can never disagree about that ordering."""
        return self.operator_res_order[name]


def build(jobs, shop, config, plan_start: datetime, shifts,
          *, setup_mode: str = "credit",
          hold_across_unmanned_shift: bool = True) -> Built:
    from pyjobshop import Model

    horizon_min = shifts[-1].end if shifts else 0
    setup_min = int(getattr(config, "setup_time_min", 90) or 0)
    m = Model()

    machine_res, operator_res = {}, {}
    for mid, machine in sorted(shop.machines.items()):
        machine_res[mid] = m.add_machine(
            breaks=windows.machine_breaks(machine, shifts, horizon_min), name=mid)

    # Manual/inspection operators are ordinary capacity-1 renewables: Rule 1 does
    # not roster them, so which one runs a bench step is a free per-task choice.
    # Their SHIFT and their ABSENCES are not free, though, and nothing else in
    # the model carries those for a bench operator — Rule 1's roster covers
    # CNC/VMC only. They ride in as breaks on the renewable, from the same
    # windows helper the machine calendar comes from, so a shut station and a man
    # who is not there can never be read two ways.
    for operator in sorted(shop.operators, key=lambda o: o.name):
        operator_res[operator.name] = m.add_renewable(
            capacity=1,
            breaks=windows.operator_breaks(operator, shifts, horizon_min,
                                           shop.absent.get(operator.name, ()),
                                           plan_start),
            name=operator.name)

    os_res = m.add_renewable(capacity=max(1, len(jobs) + _OS_HEADROOM), name="OS")

    task_of, job_of, dated, machining = {}, {}, set(), {}
    for job in jobs:
        due_min = _due_minutes(job, plan_start)
        cp_job = m.add_job(due_date=due_min, name=job.key)
        job_of[job.key] = len(job_of)
        if due_min is not None:
            dated.add(job.key)

        prev_task = prev_op = None
        for op in job.ops:
            qty = job.qty_for(op.seq)
            if op.kind == DISPATCH:
                continue                      # a milestone, not work; §5.2
            if op.kind != OUTSOURCED and (qty <= 0 or not op.machine_options):
                continue

            # allow_idle, on a machining task, is EXACTLY Rule 2's "the part
            # stays in the chuck across an unmanned shift": the machine is held,
            # the clock runs, and nothing is cut. Off under E1, where an
            # operation may not span a dark shift at all, and off always for
            # manual/inspection tasks, which Rule 1 does not bind (spec §5.2).
            hold = hold_across_unmanned_shift and op.kind == MACHINING
            task = m.add_task(job=cp_job, allow_breaks=True, allow_idle=hold,
                              name=f"{job.key}/{op.seq}")
            idx = len(task_of)
            task_of[(job.key, op.seq)] = idx

            if op.kind == OUTSOURCED:
                m.add_mode(task, os_res, int(max(1, op.cycle_min)), demands=1)
            else:
                if op.kind == MACHINING:
                    # Rule 4, inverted (§5.4): 90 minutes is ALWAYS in the
                    # duration and credited back in rules.py only for a same-part
                    # changeover.
                    duration = setup_min + max(1, int(round(qty * op.cycle_min)))
                    machining[idx] = (job.key, op)
                else:
                    duration = max(1, int(round(qty * op.cycle_min)))
                for mid in op.machine_options:
                    _add_modes(m, task, mid, duration, shop,
                               machine_res, operator_res)

            if prev_task is not None:
                if prev_op.kind == OUTSOURCED or op.kind == OUTSOURCED:
                    m.add_end_before_start(prev_task, task)     # §5.3, sequential
                else:
                    # Rule 3's release is a VARIABLE lag, so it cannot be a
                    # StartBeforeStart(delay=...) here — rules.py adds it. Only
                    # the pacing half is expressible natively.
                    m.add_end_before_end(prev_task, task)
            prev_task, prev_op = task, op

    # Resources are indexed in creation order: machines first, then the manual/
    # inspection operators, then the OS pool. Recording that order here is what
    # lets rules.py address assign_vars[(task_idx, resource_idx)] without
    # re-deriving an index and getting it silently wrong. The operator order
    # uses the SAME sort key (by name) as the add_renewable loop above, so
    # operator_res_order can never drift out of step with what was actually built.
    order = {mid: i for i, mid in enumerate(sorted(shop.machines))}
    op_order = {op.name: len(machine_res) + i
                for i, op in enumerate(sorted(shop.operators, key=lambda o: o.name))}
    return Built(m=m, data=m.data(), task_of=task_of, job_of=job_of,
                 machine_res=machine_res, operator_res=operator_res,
                 os_res=len(machine_res) + len(operator_res),
                 shifts=list(shifts), jobs=list(jobs),
                 dated_jobs=dated, machining_tasks=machining,
                 setup_mode=setup_mode, machine_res_order=order,
                 operator_res_order=op_order,
                 job_by_key={j.key: j for j in jobs},
                 plan_start=plan_start,
                 hold_across_unmanned_shift=hold_across_unmanned_shift)


def _add_modes(m, task, mid: str, duration: int, shop,
               machine_res: dict, operator_res: dict) -> None:
    """Every way ONE machine can run this step.

    Keyed on the MACHINE, never on the step's kind. Rule 1 is a property of
    people and the machines they are rostered to, and a step's kind is only ever
    read off its FIRST machine option (``domain._kind_for_machine_id``), so the
    two disagree the moment a routing lists ``MD1/CNC1`` or ``CNC1/MD1`` — and
    real routings do.

    * A machine the roster covers (CNC/VMC) gets a **machine-only** mode: the man
      is whoever Rule 1 put on it for the shift. Booking one here as well would
      charge the same person twice for the same work, and a shop with one
      qualified operator would come out INFEASIBLE for work it can plainly do.
    * Every other machine carries its operator in the mode, because nothing else
      in the model will.

    ``demands=[0, 1]``: a unary Machine takes no capacity and the operator takes
    one. ``[1, 1]`` is rejected as "infeasible demands".
    """
    if mid in shop.machining_ids:
        m.add_mode(task, machine_res[mid], duration)
        return
    for name in _qualified(shop, mid):
        m.add_mode(task, [machine_res[mid], operator_res[name]],
                   duration, demands=[0, 1])


def _due_minutes(job, plan_start: datetime):
    """The last minute of the delivery DATE, or None.

    An order finishing any time ON its delivery date is on time, matching the
    app's ``(completion_date - due_date).days <= 0``. None for an undated order:
    pyjobshop asserts on a missing due date only when tardiness vars are built,
    and objective.py skips undated jobs for the same reason the roster engine
    does — recording 0.0 would claim a perfect landing.
    """
    if job.due is None:
        return None
    midnight = datetime.combine(job.due, datetime.min.time())
    return int((midnight - plan_start).total_seconds() // 60) + 1440


def _qualified(shop, mid: str) -> list:
    """Operators the Settings table says may run this machine.

    Qualification is EXACTLY the Settings machine list. Role is not a gate — it
    is inherited by name from the workbook's operator sheet, a fossil, and gating
    on it silently discarded the admin's assignment (2026-08-07: Sandeep Kumar
    was given CNC4, dropped from its pool as a workbook "helper", and CNC4 sat
    idle with work waiting).
    """
    return sorted(o.name for o in shop.operators
                  if mid in (getattr(o, "machines", None) or ()))
