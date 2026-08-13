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
    machine_res: dict               # machine id -> resource index
    operator_res: dict              # operator name -> resource index
    os_res: int
    shifts: list
    jobs: list
    dated_jobs: set                 # job keys that HAVE a delivery date
    machining_tasks: dict = field(default_factory=dict)   # task idx -> (job_key, op)
    setup_mode: str = "credit"

    # Filled by later layers. Declared HERE so there is one definition of this
    # object's shape: rules.py populates shift_work (E2) and setup_credit
    # (Rule 4), and both address the model by index through the maps above.
    setup_credit: dict = field(default_factory=dict)   # task idx -> IntVar (Task 5)
    shift_work: dict = field(default_factory=dict)     # task idx -> [IntVar] (Task 4)
    job_by_key: dict = field(default_factory=dict)     # job key -> domain.Job
    machine_res_order: dict = field(default_factory=dict)  # machine id -> res idx

    def machine_res_index(self, mid: str) -> int:
        """Resource index of a machine. PyJobShop's Variables are keyed by
        index — assign_vars is (task_idx, resource_idx) — and machines are added
        first, so a machine's resource index is its position in machine_res."""
        return self.machine_res_order[mid]


def build(jobs, shop, config, plan_start: datetime, shifts,
          *, setup_mode: str = "credit") -> Built:
    from pyjobshop import Model

    horizon_min = shifts[-1].end if shifts else 0
    setup_min = int(getattr(config, "setup_time_min", 90) or 0)
    m = Model()

    machine_res, operator_res = {}, {}
    for mid, machine in sorted(shop.machines.items()):
        machine_res[mid] = m.add_machine(
            breaks=windows.machine_breaks(machine, shifts, horizon_min), name=mid)

    # Manual/inspection operators are ordinary capacity-1 renewables: Rule 1 does
    # not bind them, so they are a free per-task choice. CNC/VMC operators do NOT
    # appear here at all — they enter through the roster in rules.py.
    for operator in sorted(shop.operators, key=lambda o: o.name):
        operator_res[operator.name] = m.add_renewable(capacity=1, name=operator.name)

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

            task = m.add_task(job=cp_job, allow_breaks=True,
                              name=f"{job.key}/{op.seq}")
            idx = len(task_of)
            task_of[(job.key, op.seq)] = idx

            if op.kind == OUTSOURCED:
                m.add_mode(task, os_res, int(max(1, op.cycle_min)), demands=1)
            elif op.kind == MACHINING:
                # Rule 4, inverted (§5.4): 90 minutes is ALWAYS in the duration
                # and credited back in rules.py only for a same-part changeover.
                # A Machine is unary and takes no demand, so no demands= here.
                duration = setup_min + max(1, int(round(qty * op.cycle_min)))
                for mid in op.machine_options:
                    m.add_mode(task, machine_res[mid], duration)
                machining[idx] = (job.key, op)
            else:
                duration = max(1, int(round(qty * op.cycle_min)))
                for mid in op.machine_options:
                    for name in _qualified(shop, mid):
                        # [0, 1]: unary machine takes no capacity, the operator
                        # takes one. [1, 1] is rejected as "infeasible demands".
                        m.add_mode(task, [machine_res[mid], operator_res[name]],
                                   duration, demands=[0, 1])

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
    # inspection operators, then the OS pool. Recording the machine order here is
    # what lets rules.py address assign_vars[(task_idx, resource_idx)] without
    # re-deriving an index and getting it silently wrong.
    order = {mid: i for i, mid in enumerate(sorted(shop.machines))}
    return Built(m=m, data=m.data(), task_of=task_of, job_of=job_of,
                 machine_res=machine_res, operator_res=operator_res,
                 os_res=len(machine_res) + len(operator_res),
                 shifts=list(shifts), jobs=list(jobs),
                 dated_jobs=dated, machining_tasks=machining,
                 setup_mode=setup_mode, machine_res_order=order,
                 job_by_key={j.key: j for j in jobs})


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
