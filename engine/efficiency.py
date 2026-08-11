"""Fair monthly operator efficiency report — pure, reporting-only.

The formula (owner-approved, see
``docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md``):

    Efficiency % = Earned ÷ Attended × 100
      Earned minutes  = Σ standard cycle_time(item, process) × good qty punched
      Attended minutes = Σ per worked (operator, day, shift) window HAVING at
                         least one standard punch:
                         that shift's window minutes
                         − the standard punches' recorded downtime minutes
                         − their recorded setup minutes        (floored at 0)

Fairness rules baked in:
  * Downtime and setup are NEUTRAL — they shrink attended time, never earn nor
    penalize (an idle-through-no-fault operator is not judged for it).
  * Only GOOD quantity earns; rejects earn nothing and surface as a reject %.
  * A punch whose item/process has no cycle-time standard is EXCLUDED from
    BOTH sides — it earns nothing AND contributes nothing to attended (neither
    its shift window nor its downtime/setup); a wholly-no-standard shift adds
    zero attended minutes. Such punches are counted in "No-standard punches"
    (and still roll into the informational qty/downtime/setup totals) — nobody
    is judged against a standard that doesn't exist.
  * Absence days come from the absence table as their own column — never folded
    into pace.
  * Legacy punches with no operator name fall into an "Unattributed" row.

Pure: everything is derived from the parameters — no storage, no wall clock.
"""
from __future__ import annotations

from datetime import date, timedelta

from .loaders import normalize_process_name


# --------------------------------------------------------------------------- #
# Cycle-time (standard) lookup
# --------------------------------------------------------------------------- #
def _cycle_for(masters, item_code, process_name):
    """Standard cycle time (minutes/piece) for a punched (item, process), or None
    when there is no standard — no routing for the item, no process whose name
    matches (NORMALIZED, case/space-insensitive, reusing the loader's
    ``normalize_process_name`` — the same key Rule 6 / the order book match on),
    or a blank/non-numeric cycle time. A None means "no standard": the punch is
    excluded from both sides of the formula and flagged."""
    routings = getattr(masters, "routings", None) or {}
    routing = routings.get(item_code)
    if routing is None:
        return None
    target = normalize_process_name(process_name)
    for p in routing.processes:
        if normalize_process_name(p.name) == target:
            ct = p.cycle_time
            return ct if isinstance(ct, (int, float)) else None
    return None


# --------------------------------------------------------------------------- #
# Shift windows (minutes) from config
# --------------------------------------------------------------------------- #
def _norm_shift(shift):
    """Canonical shift key for grouping the attended window: 'first' / 'second' /
    'manual'. Any blank or unrecognised shift text maps to the manual (day)
    window, per the spec.

    NOTE: this is deliberately its OWN normalizer, not shared with
    ``engine.operator_coverage._shift_kind`` (which governs Rule 6 scheduling
    off the Operator MASTER's ``shift`` field — real data there is literally
    "First shift"/"Second shift"). This function instead classifies the
    Capture-form's free-text ``Actual.shift`` field, whose real vocabulary is
    "1st shift"/"2nd shift" (see web/app.js's Capture form + the ``/items``
    response in api/main.py). Broadening ``_shift_kind`` to also match "1st"/
    "2nd" would risk changing Rule 6's machine-window/scheduling output for no
    benefit (it already correctly matches its own real inputs) — so this stays
    a separate, reporting-only normalizer scoped to efficiency.py.
    """
    s = (shift or "").strip().lower()
    if "first" in s or "1st" in s or s.startswith("1"):
        return "first"
    if "second" in s or "2nd" in s or s.startswith("2"):
        return "second"
    return "manual"


def _shift_window_min(shift_key, config):
    """Minutes in a shift window, computed from config hours (never hardcoded):
      First  = first_shift_start_hour → first_shift_end_hour
      Second = first_shift_end_hour → (24 + second_shift_end_hour)
      manual = manual_start_hour → manual_end_hour
    """
    if shift_key == "first":
        hours = config.first_shift_end_hour - config.first_shift_start_hour
    elif shift_key == "second":
        hours = (24 + config.second_shift_end_hour) - config.first_shift_end_hour
    else:
        hours = config.manual_end_hour - config.manual_start_hour
    return float(hours * 60)


# --------------------------------------------------------------------------- #
# Absences
# --------------------------------------------------------------------------- #
def _absence_days_in_month(operator, absences, calendar, year, month):
    """Count an operator's absence DAYS that fall inside the report month.

    A day counts only if it is a WORKING day (``calendar.is_working_day`` — so a
    weekly-off / holiday inside an absence range is not double-counted as both
    non-working and absent). Malformed rows are skipped, matching
    ``optimize_service.absence_reservations``. This mirrors
    ``analytics._absent_working_days`` but is scoped to a calendar month rather
    than a plan window."""
    m_start = date(year, month, 1)
    m_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    m_end = m_end - timedelta(days=1)                 # inclusive last day of month
    n = 0
    for a in absences or []:
        if a.get("operator") != operator:
            continue
        try:
            f = date.fromisoformat(a["from_date"])
            t = date.fromisoformat(a["to_date"])
        except (KeyError, ValueError, TypeError):
            continue                                  # malformed row — skip
        if t < f:
            f, t = t, f
        d, last = max(f, m_start), min(t, m_end)
        while d <= last:
            if calendar.is_working_day(d):
                n += 1
            d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
_UNATTRIBUTED = "Unattributed"

# The column contract, in order — exposed so callers (e.g. the CSV endpoint)
# can emit a header row even for an empty month (no rows to read keys from).
REPORT_COLUMNS = [
    "Operator", "Days worked", "Days absent", "Attended (min)", "Earned (min)",
    "Efficiency %", "Pace vs standard (x)", "Good qty", "Rejected qty",
    "Reject %", "Downtime (min)", "Setup (min)", "Jobs handled",
    "No-standard punches",
]


def monthly_report(actuals, absences, masters, config, year, month):
    """One row per operator for the given calendar month (dicts, spec columns).

    Sorted by Efficiency % descending; rows whose efficiency is None (never
    attended, or no standards earned) come after the numeric ones; the
    "Unattributed" bucket is always last.
    """
    calendar = getattr(masters, "calendar", None)

    # Only this month's punches, keyed by the operator name ("" -> Unattributed).
    month_actuals = [
        a for a in actuals
        if a.entry_date.year == year and a.entry_date.month == month
    ]

    # Which operators appear at all: those who punched + those with month absences.
    punch_ops = {(a.operator or _UNATTRIBUTED) for a in month_actuals}
    absent_ops = {a.get("operator") for a in (absences or []) if a.get("operator")}
    absent_ops = {
        op for op in absent_ops
        if calendar and _absence_days_in_month(op, absences, calendar, year, month) > 0
    }
    operators = punch_ops | absent_ops

    rows = []
    for op in operators:
        op_actuals = [a for a in month_actuals if (a.operator or _UNATTRIBUTED) == op]

        # --- attended: one window per distinct (date, normalized shift) --- #
        # "Excluded from BOTH sides": only punches WITH a standard build attended.
        # A (date, shift) group's window exists iff it has >= 1 standard punch,
        # and only the standard punches' downtime + setup deduct from it — a
        # wholly-no-standard shift contributes nothing to attended (its punches
        # still count in the flag column and the informational qty totals).
        groups = {}   # (date, shift_key) -> [window_min, deducted_min]
        for a in op_actuals:
            if _cycle_for(masters, a.item_code, a.process) is None:
                continue                                  # no standard — excluded
            key = (a.entry_date, _norm_shift(a.shift))
            if key not in groups:
                groups[key] = [_shift_window_min(key[1], config), 0.0]
            groups[key][1] += a.total_downtime_min() + a.actual_setup_min
        attended = sum(max(win - deducted, 0.0) for win, deducted in groups.values())

        # --- earned / qty / flags --- #
        earned = 0.0
        good_qty = 0.0
        rejected_qty = 0.0
        produced_qty = 0.0
        downtime = 0.0
        setup = 0.0
        no_standard = 0
        jobs = set()
        days = set()
        for a in op_actuals:
            good = a.good_qty()
            good_qty += good
            rejected_qty += a.qty_rejected
            produced_qty += a.qty_produced
            downtime += a.total_downtime_min()
            setup += a.actual_setup_min
            jobs.add(a.key)                    # distinct (SO#, item) orders handled
            days.add(a.entry_date)
            ct = _cycle_for(masters, a.item_code, a.process)
            if ct is None:
                no_standard += 1
            else:
                earned += ct * good

        days_absent = (
            _absence_days_in_month(op, absences, calendar, year, month)
            if (calendar and op != _UNATTRIBUTED) else 0
        )

        # Efficiency / pace are None when there is nothing to fairly compare
        # against: no attended time, or no earned standard minutes.
        if attended > 0 and earned > 0:
            efficiency = round(earned / attended * 100, 1)
            pace = round(attended / earned, 2)         # spec: attended/earned
        else:
            efficiency = None
            pace = None

        reject_pct = round(rejected_qty / produced_qty * 100, 1) if produced_qty > 0 else 0.0

        rows.append({
            "Operator": op,
            "Days worked": len(days),
            "Days absent": days_absent,
            "Attended (min)": round(attended, 1),
            "Earned (min)": round(earned, 1),
            "Efficiency %": efficiency,
            "Pace vs standard (x)": pace,
            "Good qty": good_qty,
            "Rejected qty": rejected_qty,
            "Reject %": reject_pct,
            "Downtime (min)": round(downtime, 1),
            "Setup (min)": round(setup, 1),
            "Jobs handled": len(jobs),
            "No-standard punches": no_standard,
        })

    # Sort: Unattributed always last; then numeric efficiency desc; then None-eff
    # rows (tie-broken by operator name for stable output).
    def _sort_key(r):
        eff = r["Efficiency %"]
        return (
            1 if r["Operator"] == _UNATTRIBUTED else 0,
            0 if eff is not None else 1,
            -(eff if eff is not None else 0.0),
            r["Operator"],
        )

    rows.sort(key=_sort_key)
    return rows
