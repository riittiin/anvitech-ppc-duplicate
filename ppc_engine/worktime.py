"""Working-time model — turns the calendar + shift clock into concrete time windows.

Two jobs:
  1. Compute a shift's real start/end datetimes (handling the night shift that
     crosses midnight).
  2. Walk forward through a machine's working windows, skipping non-working time
     (Thursdays, holidays, and the night shift for machines that don't run it).

Plus one helper: which shift an operator is on for a given date (the Friday
rotation, RULES.md Part 1 §3).

All functions are pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Iterator

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Machine, Operator, Role, Shift

# Safety bound so a window walk can never loop forever if a routing is unschedulable.
_MAX_DAYS_LOOKAHEAD = 366 * 3


@dataclass(frozen=True)
class Window:
    """One continuous working window on a machine.

    Attributes:
        start:      Window start datetime.
        end:        Window end datetime.
        shift_date: The calendar date the shift is anchored to (its start date). The
                    night shift starting 19:00 on the 5th and ending 05:00 on the 6th
                    has shift_date = the 5th.
        shift:      Which shift this window is (FIRST / SECOND).
    """

    start: datetime
    end: datetime
    shift_date: date
    shift: Shift


def shift_window(day: date, shift: Shift, config: PlanConfig) -> Window:
    """Return the concrete datetime window for ``shift`` anchored to ``day``.

    The night (second) shift crosses midnight when its end time is earlier than its
    start time (e.g. 19:00 → 05:00): in that case the end is on the next day.
    """
    if shift == Shift.FIRST:
        start = datetime.combine(day, config.first_start)
        end = datetime.combine(day, config.first_end)
    else:
        start = datetime.combine(day, config.second_start)
        end_day = day if config.second_end > config.second_start else day + timedelta(days=1)
        end = datetime.combine(end_day, config.second_end)
    return Window(start=start, end=end, shift_date=day, shift=shift)


def iter_windows(
    machine: Machine,
    start_from: datetime,
    calendar: ShopCalendar,
    config: PlanConfig,
) -> Iterator[Window]:
    """Yield ``machine``'s working windows in time order, on/after ``start_from``.

    Skips:
      - non-working days (weekly off / holidays), and
      - the second (night) shift for machines that don't run at night
        (manual/inspection stations — their helpers/inspectors are first-shift-only).

    Only windows whose END is after ``start_from`` are yielded (fully-past windows are
    skipped). The caller clips the actual segment start with ``max(cursor, win.start)``.

    This is an iterator; the caller stops pulling once the operation is fully placed.
    """
    day = start_from.date()
    for _ in range(_MAX_DAYS_LOOKAHEAD):
        if calendar.is_working_day(day):
            # First shift, then (if the machine runs at night) the second shift.
            first = shift_window(day, Shift.FIRST, config)
            if first.end > start_from:
                yield first
            if machine.runs_second_shift:
                second = shift_window(day, Shift.SECOND, config)
                if second.end > start_from:
                    yield second
        day += timedelta(days=1)


@lru_cache(maxsize=None)
def _fridays_after(anchor: date, day: date) -> int:
    """Count Fridays f with ``anchor < f <= day`` — i.e. how many Friday rotations
    have taken effect by ``day``, given the rotation reference ``anchor``.

    Pure and cached: it's called ~a million times per full-book decode with only a few
    hundred distinct (anchor, day) pairs. (Python weekday for Friday is 4.)
    """
    if day <= anchor:
        return 0
    # Move to the first Friday strictly after anchor.
    days_to_friday = (4 - anchor.weekday()) % 7
    days_to_friday = days_to_friday or 7  # strictly after, so never 0
    first_friday = anchor + timedelta(days=days_to_friday)
    if first_friday > day:
        return 0
    return (day - first_friday).days // 7 + 1


@lru_cache(maxsize=None)
def _shift_for(role: Role, base_shift: Shift, anchor: date | None, day: date) -> Shift:
    """Pure, cached core of effective_shift (keyed only on hashable, immutable values)."""
    # The shift an admin sets in Settings is the shift the planner uses, for EVERY role
    # (2026-08-05 rule; enforced here 2026-08-07). This used to force any non-operator
    # role to FIRST, so putting a helper or inspector on nights silently did nothing.
    if anchor is None:
        return base_shift  # no rotation reference → base shift is fixed
    flips = _fridays_after(anchor, day)
    if flips % 2 == 0:
        return base_shift
    return Shift.SECOND if base_shift == Shift.FIRST else Shift.FIRST


def effective_shift(operator: Operator, day: date, config: PlanConfig) -> Shift:
    """Which shift ``operator`` is on for ``day``.

    Helpers and inspectors never rotate — always FIRST shift. Operators flip between
    FIRST and SECOND every Friday: from their ``base_shift`` (their shift as of
    ``week_anchor``), an odd number of elapsed Friday rotations flips them. See
    RULES.md Part 1 §3. Delegates to a cached pure function for speed.
    """
    return _shift_for(operator.role, operator.base_shift, config.week_anchor, day)
