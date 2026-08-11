"""Analytics — utilization & bottlenecks derived from a plan (pure; no state/UI).

Every resource is measured against ITS OWN available capacity in the plan window
[min(start), max(end)], so a manual station (≈9.5 h/day) and a two-shift CNC
(≈19.5 h/day) are judged fairly. Machine capacity reuses Rule 6's per-machine
clock, so it matches exactly how the plan was scheduled.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .models import fmt_date
from .operator_coverage import eligible_window
from .optimizer import makespan_days as _makespan_days
from .rules.rule6_allocate import _clock_factory, build_shiftwise_timeline, UNSTAFFED
from .worktime import WorkClock

NON_MACHINE_LANES = {"OS / Outsourced", "Off-machine"}
BOTTLENECK_PCT = 85.0
UNDERUSED_PCT = 30.0


def _util(busy_hrs, avail_hrs):
    """Utilization % (Busy/Available), or None when there is no available capacity."""
    return round(busy_hrs / avail_hrs * 100.0, 1) if avail_hrs > 0 else None


def _status(u):
    if u is None:
        return "no capacity"
    if u >= BOTTLENECK_PCT:
        return "bottleneck"
    if u <= UNDERUSED_PCT:
        return "under-used"
    return "healthy"


def _by_util(rows):
    rows.sort(key=lambda r: (r["Utilization %"] is None, -(r["Utilization %"] or 0.0)))
    return rows


def _avg_util(rows):
    vals = [r["Utilization %"] for r in rows if r["Utilization %"] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _working_days(calendar, start, end):
    d, last, n = start.date(), end.date(), 0
    while d <= last:
        if calendar.is_working_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def _shift_hours(shift, config):
    """Standard hours for a shift label. Two-shift operators: first 08->19 (11h),
    second 19->05 (10h). A blank/day label is a manual/helper day window (09->18)."""
    s = (shift or "").lower()
    if "second" in s:                                   # 19:00 -> 05:00 next day
        return float(24 - config.first_shift_end_hour + config.second_shift_end_hour)
    if "first" in s:
        return float(config.first_shift_end_hour - config.first_shift_start_hour)   # 08->19
    return float(getattr(config, "manual_end_hour", 18) - getattr(config, "manual_start_hour", 9))


def _friday_on_or_before(d):
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _rotations(anchor, day):
    """Fridays f with anchor < f <= day. Historical: used to count how many Friday
    rotations had taken effect when the new engine still flipped two-shift operators
    weekly. Rotation was removed 2026-08-05 (an admin's Settings shift now holds every
    week) — the caller below always passes ``rotate=False``, so this count is no
    longer consulted, but the function is kept for `_effective_shift`'s dead branch."""
    if day <= anchor:
        return 0
    dtf = (4 - anchor.weekday()) % 7 or 7                 # to the first Friday strictly after
    first = anchor + timedelta(days=dtf)
    if first > day:
        return 0
    return (day - first).days // 7 + 1


def _effective_shift(nominal, anchor, day, rotate):
    """The operator's shift ON `day`. ``rotate`` is a historical hook for the old
    Friday-flip behaviour (removed 2026-08-05) and is always passed ``False`` by the
    caller below, so this always returns the nominal shift unchanged — an operator's
    shift is whatever is set in Settings, every week, until an admin changes it. The
    ``rotate=True`` branch is kept only so a future re-introduction of rotation would
    not need to relearn this math; it is dead code today. Manual/day operators are
    unaffected either way."""
    s = (nominal or "").lower()
    if "first" in s:
        base_second = False
    elif "second" in s:
        base_second = True
    else:
        return nominal                                   # manual/day — no rotation
    if not rotate:
        return nominal
    is_second = base_second ^ (_rotations(anchor, day) % 2 == 1)
    return "Second shift" if is_second else "First shift"


def _absent_days(operator, absences, calendar, win_start_d, win_end_d):
    """The SET of ``operator``'s absence dates that fall on a working day in the window.
    Tolerates malformed rows (skip)."""
    from datetime import date as _date
    out = set()
    for a in absences or []:
        if a.get("operator") != operator:
            continue
        try:
            f = _date.fromisoformat(a["from_date"]); t = _date.fromisoformat(a["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        d = max(f, win_start_d)
        while d <= min(t, win_end_d):
            if calendar.is_working_day(d):
                out.add(d)
            d += timedelta(days=1)
    return out


def _operator_available_hours(nominal, calendar, win_start, win_end, plan_start, config,
                              absent, rotate):
    """The operator's available hours in the window = sum over each working day (not
    absent) of that day's EFFECTIVE shift hours. ``rotate`` is always ``False`` now
    (rotation removed 2026-08-05 — see `_effective_shift`), so this reduces to
    working-days x nominal-shift-hours: the operator's Settings shift is fixed for the
    whole plan, keeping them <=100% since they work at most one shift/day either way."""
    anchor = _friday_on_or_before(plan_start)
    d, last, total = win_start.date(), win_end.date(), 0.0
    while d <= last:
        if calendar.is_working_day(d) and d not in absent:
            total += _shift_hours(_effective_shift(nominal, anchor, d, rotate), config)
        d += timedelta(days=1)
    return total


def build_analytics(schedule, masters, config, batches=None, absences=None):
    """Utilization analytics for one plan. Returns a JSON-able dict with keys:
    ``window``, ``machines``, ``machine_groups``, ``operators``, ``processes``, ``headline``."""
    if not schedule:
        return {"window": None, "machines": [], "machine_groups": [],
                "operators": [], "processes": [], "headline": {}}

    win_start = min(e.start for e in schedule)
    win_end = max(e.end for e in schedule)
    clock_for, _ = _clock_factory(masters, config)

    def avail_hrs(mid):
        """Available machine-hours in the plan window. Normally the operator-coverage
        clock (matches how the OLD engine scheduled). But the NEW (production) engine
        schedules manual/finishing work regardless of first-shift operator coverage, so
        an uncovered-but-used station would get a 0-capacity clock and show '-' ("no
        capacity") for a machine the plan actually uses (live 2026-07-24 report). When
        the gated clock is empty, fall back to the machine's PHYSICAL window so
        utilization stays honest; a covered machine is unchanged (byte-identical)."""
        mins = clock_for(mid).working_minutes_between(win_start, win_end)
        if mins == 0:
            mac = masters.machines.get(mid)
            if mac is not None:
                mins = WorkClock(masters.calendar, eligible_window(mac, config)) \
                    .working_minutes_between(win_start, win_end)
        return mins / 60.0

    def disp(mid):
        m = masters.machines.get(mid)
        return m.display_name if m else mid

    def mtype(mid):
        m = masters.machines.get(mid)
        return m.machine_type if (m and m.machine_type) else "Other"

    # --- Machines (exclude non-machine lanes) ---
    # EVERY machine in the master gets a row, not just the ones this plan used. A
    # utilization report that walks only the schedule silently DROPS an idle machine —
    # exactly the row that matters most (live 2026-08-07: Test9 lost MA1/MP1/MPK3/MW3,
    # and the same bug hid an operator with no work from the per-operator table).
    by_machine = defaultdict(list)
    for mid in masters.machines:
        by_machine[mid] = []
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_machine[e.machine].append(e)   # a lane-less resource not in the master still counts

    machines = []
    for mid, ops in by_machine.items():
        busy = sum(e.occupancy_min for e in ops) / 60.0
        avail = avail_hrs(mid)
        u = _util(busy, avail)
        machines.append({
            "Machine": disp(mid), "Type": mtype(mid),
            "Busy (hrs)": round(busy, 1), "Available (hrs)": round(avail, 1),
            "Utilization %": u, "Idle (hrs)": round(max(avail - busy, 0.0), 1),
            "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
            "Status": _status(u),
        })
    _by_util(machines)

    # --- Group rollup by machine type ---
    groups = defaultdict(lambda: {"busy": 0.0, "avail": 0.0, "machines": 0})
    for mid, ops in by_machine.items():
        g = groups[mtype(mid)]
        g["busy"] += sum(e.occupancy_min for e in ops) / 60.0
        g["avail"] += avail_hrs(mid)
        g["machines"] += 1
    machine_groups = _by_util([
        {"Type": t, "Machines": v["machines"],
         "Busy (hrs)": round(v["busy"], 1), "Available (hrs)": round(v["avail"], 1),
         "Utilization %": _util(v["busy"], v["avail"]),
         "Status": _status(_util(v["busy"], v["avail"]))}
        for t, v in groups.items()
    ])

    # --- Operators (only when operator logic assigned them) ---
    # Hours are attributed PER SHIFT via the shift-wise timeline: a multi-shift op
    # (e.g. a days-long block on a two-shift VMC) carries ONE operator name on the
    # schedule entry, but its night-shift hours are really worked by the qualified
    # second-shift person. Billing the whole block to the named (day) operator made
    # utilization exceed 100% — physically impossible. Splitting each op into its
    # per-day, per-shift segments and crediting the operator actually manning each
    # shift keeps every person within their own shift capacity.
    operators = []
    unstaffed_hrs = 0.0
    if getattr(config, "apply_operator_logic", False):
        segs = build_shiftwise_timeline(schedule, masters, config, batches)
        by_op = defaultdict(list)
        # EVERY operator on the app's Settings table gets a row, not just the ones this
        # plan gave work to. Walking only the schedule silently dropped an unused person
        # — the live 2026-08-07 report: "20 staff in Settings, 19 in Analytics" (Sandeep
        # Kumar). A 0% row is the most actionable row in a utilization table.
        for o in masters.operators:
            if o.name:
                by_op[o.name] = []
        for r in segs:
            if r["Operator"] == UNSTAFFED:
                # No qualified person was free for this shift segment — the plan
                # wants more concurrent work than the crew can staff. Surfaced as
                # its own headline number; never billed to a person (a person
                # cannot exceed 100% of their own shift).
                unstaffed_hrs += r["Minutes"] / 60.0
            elif r["Operator"]:
                by_op[r["Operator"]].append(r)
        shift_of = {o.name: o.shift for o in masters.operators}
        # Rotation anchor = the engine's week_anchor (Friday on/before the plan start),
        # so the per-day effective shift here matches the shift the engine scheduled.
        plan_start = getattr(config, "plan_start_date", None) or win_start.date()
        # Shift rotation was removed 2026-08-05: every operator works the shift on
        # file in Settings, every week. Capacity must match, so never rotate here.
        rotate = False
        for name, rows in by_op.items():
            busy = sum(r["Minutes"] for r in rows) / 60.0
            absent = _absent_days(name, absences, masters.calendar,
                                  win_start.date(), win_end.date())
            avail = _operator_available_hours(
                shift_of.get(name, ""), masters.calendar, win_start, win_end,
                plan_start, config, absent, rotate)
            u = _util(busy, avail)
            # Distinct operations the person manned (a multi-shift op shared with
            # another shift's operator counts for each participant).
            distinct = {(r["Batch"], r["Process"]): r["Qty"] for r in rows}
            operators.append({
                "Operator": name, "Busy (hrs)": round(busy, 1),
                "Available (hrs)": round(avail, 1), "Utilization %": u,
                "Ops": len(distinct), "Pieces": round(sum(distinct.values())),
                "Status": _status(u),
            })
        _by_util(operators)

    # --- Processes (in-house machine work only; where capacity is spent) ---
    by_proc = defaultdict(list)
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_proc[e.process_name].append(e)
    proc_total = sum(e.occupancy_min for ops in by_proc.values() for e in ops) / 60.0
    processes = sorted(
        [{"Process": name, "Work (hrs)": round(sum(e.occupancy_min for e in ops) / 60.0, 1),
          "Share %": (round(sum(e.occupancy_min for e in ops) / 60.0 / proc_total * 100.0, 1)
                      if proc_total else 0.0),
          "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
          "Machines": ", ".join(sorted({disp(e.machine) for e in ops}))}
         for name, ops in by_proc.items()],
        key=lambda r: -r["Work (hrs)"])

    total_busy = round(sum(m["Busy (hrs)"] for m in machines), 1)
    # The SAME makespan the Optimize panel/header shows (days from plan start to the
    # last end) — one shared definition so the tabs never disagree (2026-07-26). Not
    # the old calendar-days-spanned count, which read a different number for one plan.
    plan_start = getattr(config, "plan_start_date", None) or win_start.date()
    makespan_days = _makespan_days(schedule, plan_start)
    return {
        "window": {"start": fmt_date(win_start), "end": fmt_date(win_end),
                   "makespan_days": makespan_days},
        "machines": machines,
        "machine_groups": machine_groups,
        "operators": operators,
        "processes": processes,
        "headline": {
            "window_start": fmt_date(win_start), "window_end": fmt_date(win_end),
            "makespan_days": makespan_days,
            "total_busy_hrs": total_busy,
            "avg_machine_util": _avg_util(machines),
            "bottleneck": machines[0] if machines else None,
            "underused": [m for m in machines
                          if m["Utilization %"] is not None and m["Utilization %"] <= UNDERUSED_PCT],
            "unstaffed_hrs": round(unstaffed_hrs, 1),
        },
    }
