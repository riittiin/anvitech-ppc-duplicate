"""Operator + shift coverage → per-machine working windows (pure, no IO).

The shop rule: an activity can run on a machine during a shift only if at least one
operator who (a) specialises in that machine and (b) is assigned to that shift is
available. A machine's working window is the union of the shift windows it is
covered for, intersected with what its Available Hrs/Day allows:

  * two-shift machine (Available Hrs/Day ≥ threshold, ≈19.5): eligible First
    (08:00-19:00) + Second (19:00-05:00); runs the shifts that have a qualified
    operator.
  * single-shift / manual resource (≈9.5): eligible 09:00-18:00 only, and only if a
    FIRST-shift operator qualifies (manual work runs first shift).

Operator specialties match a machine by its machine_no OR its (normalized) type, so
both "CNC 1" and "Milling M/c"/"Manual Deburring" style entries resolve. Provisional
machines (routing-referenced, not in the master) bypass the gate and keep a
two-shift window so nothing silently disappears.

Windows are lists of (start_min, end_min) minutes-from-midnight; an end > 1440
crosses midnight. Feed them straight to ``WorkClock``.
"""
from __future__ import annotations

from .loaders import normalize_resource_id


def _shift_windows(config):
    first = (config.first_shift_start_hour * 60, config.first_shift_end_hour * 60)
    second = (config.first_shift_end_hour * 60, (24 + config.second_shift_end_hour) * 60)
    manual = (config.manual_start_hour * 60, config.manual_end_hour * 60)
    return first, second, manual


def _shift_kind(operator) -> str:
    s = (operator.shift or "").strip().lower()
    if "second" in s or s == "2":
        return "second"
    if "first" in s or s == "1":
        return "first"
    return ""  # unknown / blank


def _machine_keys(machine) -> set:
    return {machine.machine_no, normalize_resource_id(machine.machine_type)}


def _qualifies(operator, machine) -> bool:
    return bool(set(operator.machines) & _machine_keys(machine))


def _shift_of(start_dt, config) -> str:
    """Which shift a start time falls in: first (08:00–19:00, incl. the manual
    09:00–18:00 window) vs second (19:00–05:00, incl. early morning)."""
    h = start_dt.hour + start_dt.minute / 60.0
    if config.first_shift_start_hour <= h < config.first_shift_end_hour:
        return "first"
    return "second"


def qualified_operators(machine_id, start_dt, masters, config) -> list:
    """All operators (names, sheet order) able to run ``machine_id`` at ``start_dt``:
    their Shift covers the op's start time and their specialty matches the machine
    (by machine no OR type). Rule 6 assigns the **earliest-free** of these so people
    load-balance and concurrency is capped by headcount."""
    machine = masters.machines.get(machine_id)
    keys = {machine_id}
    if machine is not None:
        keys.add(normalize_resource_id(machine.machine_type))
    want = _shift_of(start_dt, config)
    return [o.name for o in masters.operators
            if _shift_kind(o) == want and (set(o.machines) & keys)]


def _day_window(config):
    """The window a SINGLE-SHIFT (manual / inspection / packing) station runs, **as the
    ACTIVE scheduler defines it** — the one place that rule lives.

    The production NEW engine (`ppc_engine.worktime.iter_windows`) skips only the
    SECOND shift for such a station, so it runs the whole FIRST shift, 08:00–19:00.
    The ROSTER engine does the same, by its own explicit rule
    (`roster_engine.worktime.machine_runs_shift`: "First shift is the full 08:00-19:00
    window, NOT the legacy 09:00-18:00 manual window"). The retired classic and flow
    engines used the narrower manual window, 09:00–18:00, and keep it.

    Reporting features must model the shop the way the engine that BUILT the plan does,
    or they describe a different shop: before this was shared (2026-08-07), Analytics
    understated those stations' capacity by 2 h/day and the delay justification report
    labelled 08:00–09:00 and 18:00–19:00 waits "outside working hours" while the shop
    was open — 158 hours of genuinely planned work on Test9 fell outside the window
    those two features believed in. Roster was falling into exactly that hole: on the
    repo's own sample book 83 of 83 planned minutes on single-shift stations sat
    outside the window four features believed in (analytics.py, delay_report.py,
    rule6_allocate's shift-wise window AND shift label, and api's Rule-6 machine
    table)."""
    first, _second, manual = _shift_windows(config)
    return (first if getattr(config, "scheduler", "classic") in ("new", "roster")
            else manual)


def eligible_window(machine, config):
    """A machine's PHYSICAL working window by its Available Hrs/Day, IGNORING operator
    coverage: a two-shift machine runs first + second, a single-shift/manual resource
    runs ``_day_window`` (the active engine's own rule). This is the un-gated version of
    ``machine_windows`` (before the "is an operator qualified this shift?" filter). Used
    by analytics so a station the plan actually uses is never reported as zero-capacity
    just because no operator currently qualifies for it — staffing is a separate concern
    (the operator panel). Returns a list of (start_min, end_min) intervals."""
    first, second, _manual = _shift_windows(config)
    if machine.is_two_shift(config.two_shift_threshold_hours):
        return [first, second]
    return [_day_window(config)]


def staffing_gaps(masters, config):
    """Every machine in the workbook's **Machine master** that the **Settings operator
    table** cannot staff. Pure; reporting-only — never changes a plan.

    Operators, their shifts and their machines are owned by the Settings tab (the
    workbook's operator sheet only seeds it once and is a fossil afterwards), so this is
    the cross-check between the two sources of truth: Excel says which machines exist,
    Settings says who can run them.

    Returns a list of ``{"kind", "ref", "message"}`` rows, ready for the validation
    report. Two kinds:

      MACHINE_NO_OPERATOR      nobody in Settings can run it on any shift — the plan can
                               never schedule it. Provisional machines are included:
                               a routing already points at them, so an unstaffed one is
                               a real gap, not a placeholder to ignore.
      MACHINE_SHIFT_UNCOVERED  a two-shift machine with nobody on one of its shifts —
                               it still runs, but that shift's capacity is unusable.
    """
    rows = []
    for mid, machine in sorted(masters.machines.items()):
        first_ops = [o for o in masters.operators
                     if _shift_kind(o) == "first" and _qualifies(o, machine)]
        second_ops = [o for o in masters.operators
                      if _shift_kind(o) == "second" and _qualifies(o, machine)]
        name = machine.display_name or mid
        if not first_ops and not second_ops:
            rows.append({
                "kind": "MACHINE_NO_OPERATOR", "ref": mid,
                "message": (f"machine '{name}' has no operator assigned in Settings — "
                            f"add someone under Settings > Operators & shifts who can "
                            f"run it, or the plan can never schedule work on it")})
            continue
        if not machine.is_two_shift(config.two_shift_threshold_hours):
            continue
        if not second_ops:
            rows.append({
                "kind": "MACHINE_SHIFT_UNCOVERED", "ref": mid,
                "message": (f"machine '{name}' runs two shifts but nobody in Settings "
                            f"is on 2nd shift for it — its night capacity is unusable")})
        elif not first_ops:
            rows.append({
                "kind": "MACHINE_SHIFT_UNCOVERED", "ref": mid,
                "message": (f"machine '{name}' runs two shifts but nobody in Settings "
                            f"is on 1st shift for it — its day capacity is unusable")})
    return rows


def machine_windows(masters, config):
    """Return ``(windows, report)``.

    ``windows``: ``{machine_id: [(start_min, end_min), ...]}`` — an empty list means
    the machine is blocked (eligible but no covered shift).
    ``report``: ``{"blocked": [...], "unmatched_specialties": [...]}``.
    """
    first, second, manual = _shift_windows(config)
    threshold = config.two_shift_threshold_hours
    ops = masters.operators

    windows, blocked = {}, []
    for mid, machine in masters.machines.items():
        if machine.provisional:
            windows[mid] = [first, second]   # bypass coverage gate
            continue
        if machine.is_two_shift(threshold):
            iv = []
            if any(_shift_kind(o) == "first" and _qualifies(o, machine) for o in ops):
                iv.append(first)
            if any(_shift_kind(o) == "second" and _qualifies(o, machine) for o in ops):
                iv.append(second)
            reason = "no operator (any shift) specialises in this machine"
        else:
            covered = any(_shift_kind(o) == "first" and _qualifies(o, machine) for o in ops)
            iv = [_day_window(config)] if covered else []
            reason = "no first-shift operator specialises in this single-shift resource"
        windows[mid] = iv
        if not iv:
            blocked.append({"machine": machine.display_name, "machine_id": mid, "reason": reason})

    # Specialty tokens that don't match any machine (by no or type) — surfaced so
    # the owner can fix naming (e.g. an operator listing 'CNC 2' that isn't a machine).
    valid = set()
    for machine in masters.machines.values():
        valid |= _machine_keys(machine)
    unmatched = []
    for o in ops:
        for tok in o.machines:
            if tok and tok not in valid:
                unmatched.append({"operator": o.name, "specialty": tok})

    return windows, {"blocked": blocked, "unmatched_specialties": unmatched}
