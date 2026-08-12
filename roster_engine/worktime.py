"""When the shop is open, and which shift a machine or a person works.

One definition, used by the roster, the scheduler and the reports — the 2026-08-07
lesson was that a feature which re-derives shift hours WILL disagree with the
engine that built the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

FIRST = "first"
SECOND = "second"

# How far ahead iter_shifts will walk before giving up. A plan that cannot place
# an operation inside a year is a bug, not a long plan — the caller fails loud.
_HORIZON_DAYS = 400


@dataclass(frozen=True)
class ShiftWindow:
    """One shift of one day. ``day`` is the date the shift STARTS on, so a second
    shift running 19:00 -> 05:00 belongs to the earlier date."""

    day: date
    shift: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0


def _shift_bounds(day: date, shift: str, config) -> tuple[datetime, datetime]:
    """Shift clock, read from ``engine.config.Config``.

    The real Config has no ``first_shift_start``/``first_shift_end`` datetime.time
    fields (the brief's sketch assumed those) — it stores plain hour ints:
    ``first_shift_start_hour`` (8), ``first_shift_end_hour`` (19, also the 1st/2nd
    shift boundary) and ``second_shift_end_hour`` (5, next day). The second shift's
    START is not a separate field; it IS the first shift's end (the boundary), per
    Config's own comment "1st/2nd boundary".
    """
    if shift == FIRST:
        start = datetime.combine(day, time(config.first_shift_start_hour, 0))
        end = datetime.combine(day, time(config.first_shift_end_hour, 0))
        return start, end
    start = datetime.combine(day, time(config.first_shift_end_hour, 0))
    end = datetime.combine(day, time(config.second_shift_end_hour, 0))
    if end <= start:                      # 19:00 -> 05:00 crosses midnight
        end += timedelta(days=1)
    return start, end


def iter_shifts(after: datetime, calendar, config):
    """Every working shift window from ``after`` onwards, in time order.

    A shift already partly gone is still yielded (clipped by the caller against
    its own cursor) — the plan clock can start mid-shift.
    """
    day = after.date()
    for _ in range(_HORIZON_DAYS):
        if calendar.is_working_day(day):
            for shift in (FIRST, SECOND):
                start, end = _shift_bounds(day, shift, config)
                if end > after:
                    yield ShiftWindow(day, shift, start, end)
        day += timedelta(days=1)


def machine_runs_shift(machine, shift: str) -> bool:
    """A two-shift machine (CNC/VMC, Available Hrs/Day >= 12) runs both shifts; a
    single-shift station runs the FIRST shift only.

    First shift is the full 08:00-19:00 window, NOT the legacy 09:00-18:00 manual
    window — that discrepancy hid 9,470 minutes of real planned work from four
    reporting features (2026-08-07). One window, everywhere.
    """
    if shift == FIRST:
        return True
    return bool(machine.is_two_shift())


def operator_shift(operator) -> str | None:
    """Which shift this person works, from the Settings table. Rotation was removed
    2026-08-05: the shift on file is the shift, every week, until an admin edits it."""
    text = (getattr(operator, "shift", "") or "").strip().lower()
    if not text:
        return FIRST
    if "2" in text or "second" in text or "night" in text:
        return SECOND
    return FIRST
