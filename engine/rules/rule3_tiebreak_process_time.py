"""Rule 3 — Smart priority (slack / critical ratio), workload-aware.

The naive rule (strict earliest-due-date, breaking only *exact* ties) fails when
due dates differ but workloads don't: a 1-piece order due 19 Jun would outrank a
100-piece order due 20 Jun, even though the big order is far more at risk of
being late. Rule 3 fixes this with an operations-management dispatching metric:

    slack          = (working time until SO delivery date) − (work needed)
    critical_ratio = (working time until due) / (work needed)

where "work needed" = the batch's total machine occupancy (Rule 4: cycle×qty +
setup, summed over the routing) — so quantity and process time both count.
Least slack (or lowest critical ratio) = most at-risk = highest priority.

Why this is a clean generalization: on *equal* due dates the available time is the
same, so least-slack reduces to "more work first" — preserving the documented
same-date oracle (61241949-01 before 61247047-01). On *different* due dates it
weighs the extra time against the extra work, so a heavy order due slightly later
can correctly jump an earlier trivial one.

``priority_window_days`` bounds how far apart two SO delivery dates may be for the
metric to reorder them (None = no limit / pure metric sort; 0 = exact-date only,
the legacy behaviour).

Reads the routing master for work content. Pure function:
``run(sorted_batches, config, notes, masters) -> list[Batch]``.
"""
from __future__ import annotations

from datetime import datetime

from ..config import (
    PRIORITY_SLACK,
    PRIORITY_CRITICAL_RATIO,
    PRIORITY_PROCESS_TIME,
)
from ..models import fmt_date
from ..worktime import WorkClock
from . import rule4_setup_time as r4
# OS/off-machine classifiers — reused (not duplicated) from Rule 6 so this rule's
# view of "what is machine work" stays identical to how Rule 6 actually schedules
# it. rule6 does not import rule3, so this import is safe (no cycle).
from .rule6_allocate import _is_os, _is_offmachine


def _is_machine_work(proc):
    """A step that occupies an in-house machine (so it counts toward workload /
    per-piece cycle time). An OUTSOURCED (OS) step is a flat vendor turnaround, not
    per-piece machine work; an off-machine milestone (DISPATCH, etc.) consumes no
    machine. Rule 6 reserves both as flat/zero blocks — never cycle x qty — so Rule
    3 must exclude them too, or an outsourced job masquerades as a huge machining
    job (e.g. a 5000-min paint block charged as 5000 x qty) and distorts slack."""
    return not _is_os(proc) and not _is_offmachine(proc)


def _plan_start(config):
    # config.plan_start_date is never None: the API resolves "auto" (None) to
    # today (IST) at the boundary (_resolve_config) before any rule runs.
    if config.plan_start_date is None:
        # Fail loud (project law) instead of a cryptic AttributeError deep in
        # the rules if a future caller forgets to resolve auto-mode first.
        raise ValueError("plan_start_date is unresolved (None) — resolve "
                         "auto-mode to a real date before running the rules")
    return datetime(
        config.plan_start_date.year,
        config.plan_start_date.month,
        config.plan_start_date.day,
        config.first_shift_start_hour,
    )


def _work_needed(routing, qty, config):
    """Total machine occupancy the batch needs (reuses Rule 4 — no duplication).

    In-house machine steps only: OS turnaround and off-machine milestones are
    excluded (they are not cycle x qty machine work — see ``_is_machine_work``)."""
    if routing is None:
        return 0.0
    return sum(r4.occupancy_minutes(p.cycle_time, qty, config)
               for p in routing.processes if _is_machine_work(p))


def _cycle_per_piece(routing):
    """Per-piece machining time shown as 'Cycle time per piece' — the sum of the
    REAL in-house steps' cycle times. Excludes OS (flat vendor turnaround) and
    off-machine milestones, which are not per-piece machine cycle time."""
    if routing is None:
        return 0.0
    return sum(p.cycle_time for p in routing.processes
               if isinstance(p.cycle_time, (int, float)) and _is_machine_work(p))


def _time_available(clock, plan_start, due_date, config):
    due_dt = datetime(due_date.year, due_date.month, due_date.day, config.first_shift_start_hour)
    return clock.working_minutes_between(plan_start, due_dt)


def _metrics(batch, masters, clock, plan_start, config):
    """Return (work_needed, time_available, slack, critical_ratio) for a batch."""
    routing = masters.routings.get(batch.item_code)
    work = _work_needed(routing, batch.qty, config)
    avail = _time_available(clock, plan_start, batch.so_delivery_date, config)
    slack = avail - work
    cr = (avail / work) if work > 0 else float("inf")
    return work, avail, slack, cr


def run(batches, config=None, notes=None, masters=None, **kw):
    notes = notes if notes is not None else []
    routings = masters.routings if masters else {}
    metric = config.priority_metric if config else PRIORITY_SLACK
    window = config.priority_window_days if config else None

    clock = WorkClock(masters.calendar, config)
    plan_start = _plan_start(config)

    # Per-piece machining time for display (Schedule INPUT "Cycle time per piece").
    for b in batches:
        b.total_process_time = _cycle_per_piece(routings.get(b.item_code))

    def sort_key(b):
        work, avail, slack, cr = _metrics(b, masters, clock, plan_start, config)
        # batch_id is the final, always-unique tiebreak -> fully deterministic
        # ordering regardless of input order.
        if metric == PRIORITY_PROCESS_TIME:
            return (-(b.total_process_time or 0.0), b.so_delivery_date, b.item_code, b.batch_id)
        if metric == PRIORITY_CRITICAL_RATIO:
            return (cr, b.so_delivery_date, b.item_code, b.batch_id)
        return (slack, b.so_delivery_date, b.item_code, b.batch_id)  # PRIORITY_SLACK

    # Cluster by the look-ahead window over the EDD order, then sort each cluster
    # by the metric. window=None -> a single cluster (pure metric sort).
    edd = sorted(batches, key=lambda b: b.so_delivery_date)
    if window is None:
        clusters = [edd] if edd else []
    else:
        clusters = []
        for b in edd:
            if clusters and (b.so_delivery_date - clusters[-1][0].so_delivery_date).days <= window:
                clusters[-1].append(b)
            else:
                clusters.append([b])

    ordered = []
    for cl in clusters:
        ordered.extend(sorted(cl, key=sort_key))

    # Notes: explain the metric and surface any "smart" overtakes (a later-due
    # batch placed ahead of an earlier-due one because of workload risk).
    win_txt = "no window" if window is None else f"window {window}d"
    notes.append(f"Priority metric = {metric} ({win_txt}).")
    for i, b in enumerate(ordered):
        earlier_due_after = [
            o for o in ordered[i + 1:] if o.so_delivery_date < b.so_delivery_date
        ]
        if earlier_due_after:
            _, _, slack_b, _ = _metrics(b, masters, clock, plan_start, config)
            beaten = earlier_due_after[0]
            _, _, slack_e, _ = _metrics(beaten, masters, clock, plan_start, config)
            notes.append(
                f"{b.item_code} (due {fmt_date(b.so_delivery_date)}, slack "
                f"{round(slack_b)} min) prioritized ahead of earlier-due "
                f"{beaten.item_code} (due {fmt_date(beaten.so_delivery_date)}, slack "
                f"{round(slack_e)} min): higher lateness risk."
            )

    return ordered


def build_priority_breakdown(ordered, config, masters):
    """Per-batch metric breakdown for the Rule 3 tab — shows *why* each batch
    ranks where (work needed, time available, slack, critical ratio)."""
    clock = WorkClock(masters.calendar, config)
    plan_start = _plan_start(config)
    rows = []
    for rank, b in enumerate(ordered, 1):
        work, avail, slack, cr = _metrics(b, masters, clock, plan_start, config)
        rows.append({
            "Rank": rank,
            "Batch": b.batch_id,
            "Item Code": b.item_code,
            "Qty": b.qty,
            "SO Delivery Date": fmt_date(b.so_delivery_date),
            "Work needed (min)": round(work, 1),
            "Time available (min)": round(avail, 1),
            "Slack (min)": round(slack, 1),
            "Critical Ratio": (round(cr, 2) if cr != float("inf") else "∞"),
        })
    return rows
