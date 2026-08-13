"""When the shop is open, in the solver's own unit: integer minutes from the
plan start floor.

ONE definition, used by the model, the decoder and the reports. The 2026-08-07
lesson was that a feature which re-derives shift hours WILL disagree with the
engine that built the plan — by 9,470 minutes, that time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

FIRST = "first"
SECOND = "second"


@dataclass(frozen=True)
class Shift:
    """One shift of one day, in minutes from the plan start.

    ``day`` is the date the shift STARTS on, so a second shift running
    19:00 -> 05:00 belongs to the earlier date. ``index`` is its position in the
    horizon and is what the roster variables are keyed on.
    """

    index: int
    day: date
    shift: str
    start: int
    end: int

    @property
    def minutes(self) -> int:
        return self.end - self.start


def _minutes(when: datetime, plan_start: datetime) -> int:
    return int((when - plan_start).total_seconds() // 60)


def build_shifts(plan_start: datetime, calendar, config,
                 horizon_days: int) -> list[Shift]:
    """Every working shift in the horizon, in time order, indexed from 0.

    A shift already partly gone is still included; the caller clips against its
    own cursor. Non-working days contribute nothing at all.
    """
    first_a = config.first_shift_start_hour
    first_b = config.first_shift_end_hour
    second_b = config.second_shift_end_hour          # crosses midnight
    horizon_min = horizon_days * 1440

    out: list[Shift] = []
    day = plan_start.date()
    end_date = (plan_start + timedelta(days=horizon_days)).date()
    while day <= end_date:
        if calendar.is_working_day(day):
            midnight = datetime.combine(day, datetime.min.time())
            base = _minutes(midnight, plan_start)
            for shift, (a, b) in (
                    (FIRST, (first_a * 60, first_b * 60)),
                    (SECOND, (first_b * 60, (24 + second_b) * 60))):
                start, end = base + a, base + b
                if end > 0 and start < horizon_min:
                    out.append(Shift(len(out), day, shift,
                                     max(0, start), min(horizon_min, end)))
        day += timedelta(days=1)
    return out


def machine_breaks(machine, shifts: list[Shift],
                   horizon_min: int) -> list[tuple[int, int]]:
    """Every interval this machine cannot work, as CP breaks.

    Built as the COMPLEMENT of the shifts it runs, so it can never disagree with
    ``build_shifts`` — computed by walking every known shift in time order and
    marking each one this machine does NOT work (a second shift, for a
    single-shift machine) as its own break, plus any gap between known shifts
    (a weekly off day) as a break for everyone. Filtering to first-shift-only
    spans and complementing against the whole horizon (an earlier version of
    this function) can never place a break boundary at a second shift's own
    end — that boundary is only visible while walking the shifts themselves.
    Getting this wrong in either direction breaks the plan: too few breaks and
    it schedules work the shop cannot do, too many and it throws away capacity
    that exists.

    The result is COALESCED: sorted, non-overlapping, and never touching —
    a single-shift machine's nightly gap and the next calendar-off day merge
    into one interval rather than two that happen to share a boundary. This
    matters to the consumer, not just for tidiness: pyjobshop pre-enumerates a
    discrete break-duration choice per mode keyed on start-time domains, so a
    redundant touching boundary doubles the interval count for every
    single-shift station (most of the manual/inspection fleet) for no reason —
    solver tractability is this project's top named risk. One definition of the
    working calendar; a caller must never need to re-coalesce this itself.
    """
    two_shift = bool(machine.is_two_shift())
    raw: list[tuple[int, int]] = []
    cursor = 0
    for s in sorted(shifts, key=lambda s: s.start):
        if s.start > cursor:
            raw.append((cursor, s.start))
        if two_shift or s.shift == FIRST:
            cursor = max(cursor, s.end)
        else:
            raw.append((max(cursor, s.start), s.end))
            cursor = max(cursor, s.end)
    if cursor < horizon_min:
        raw.append((cursor, horizon_min))
    return _coalesce(raw)


def _coalesce(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping AND touching (start, end) intervals into the minimal
    sorted set describing the same union of forbidden time."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def operator_shift(operator) -> str:
    """Which shift this person works, from the Settings table. Rotation was
    removed 2026-08-05: the shift on file is the shift, every week, until an
    admin edits it."""
    text = (getattr(operator, "shift", "") or "").strip().lower()
    if "2" in text or "second" in text or "night" in text:
        return SECOND
    return FIRST
