"""Working-time calendar used by Rule 6.

A ``WorkClock`` advances real machine-busy time across only working periods. It is
built from a **list of day-relative intervals** (minutes from midnight; an end
> 1440 means the interval crosses into the next day), so different machines can
have different windows:

  * two-shift machine (08:00 → 05:00 next day):  ``[(8*60, 24*60 + 5*60)]``
  * single-shift / manual (09:00 → 18:00):       ``[(9*60, 18*60)]``
  * second shift only (19:00 → 05:00 next day):  ``[(19*60, 24*60 + 5*60)]``

Thursdays (weekly off) + holidays are skipped (``WorkCalendar``).

``advance(start, minutes)`` returns the datetime reached after consuming
``minutes`` of working time from ``start`` (snapping ``start`` forward to the next
working window first). ``advance(start, 0)`` snaps a time onto the next working
instant. A clock with **no intervals** raises ``NoWorkingWindow`` rather than
looping — callers (Rule 6) treat that as "this machine can't run".

Back-compat: ``WorkClock(calendar, config)`` (passing a Config) builds the legacy
single two-shift window, identical to the old behaviour.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta


class NoWorkingWindow(Exception):
    """A clock has no working intervals (e.g. an uncovered/zero-hours machine)."""


def _normalize(intervals):
    """Sort + merge touching/overlapping intervals (minutes from midnight)."""
    iv = sorted((int(s), int(e)) for s, e in intervals if e is not None and s is not None and e > s)
    merged = []
    for s, e in iv:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


class WorkClock:
    def __init__(self, calendar, intervals_or_config):
        self.cal = calendar
        # Back-compat: a Config (duck-typed) → the legacy two-shift window.
        if hasattr(intervals_or_config, "first_shift_start_hour"):
            c = intervals_or_config
            intervals = [(c.first_shift_start_hour * 60, (24 + c.second_shift_end_hour) * 60)]
        else:
            intervals = list(intervals_or_config or [])
        self.intervals = _normalize(intervals)
        # Per-day window cache: a given calendar day's working windows never change,
        # but scheduling asks for them millions of times. Memoizing the datetime
        # construction here is the single biggest scheduler speedup (results identical).
        self._day_cache: dict = {}
        # Only a window that runs past midnight (end > 24:00, e.g. the 08:00→05:00
        # two-shift) can make a PREVIOUS day's window end after "today's" cursor. When
        # no interval crosses midnight (single-shift/manual 09:00–18:00), _next_window
        # can start scanning at the cursor's own day and skip the wasted look-back.
        self._crosses_midnight = any(e > 24 * 60 for _s, e in self.intervals)

    @classmethod
    def from_config(cls, calendar, config):
        """The legacy single two-shift window (08:00 → 05:00 next day)."""
        return cls(calendar, config)

    def _windows_for_day(self, d):
        """All working windows opened on day ``d`` as (start, end) datetimes.
        Memoized per day (an immutable tuple, shared read-only by callers)."""
        cached = self._day_cache.get(d)
        if cached is not None:
            return cached
        if not self.cal.is_working_day(d):
            self._day_cache[d] = ()
            return ()
        base = datetime(d.year, d.month, d.day)
        wins = tuple((base + timedelta(minutes=s), base + timedelta(minutes=e))
                     for s, e in self.intervals)
        self._day_cache[d] = wins
        return wins

    def _next_window(self, cursor):
        """First working window (chronologically) whose end is strictly after
        ``cursor``. Starts one day back to catch a window that crosses midnight."""
        if not self.intervals:
            raise NoWorkingWindow("clock has no working intervals")
        # Look one day back only when a window can cross midnight; otherwise the
        # previous day's windows always end by this cursor and are pure waste to scan.
        d = (cursor - timedelta(days=1)).date() if self._crosses_midnight else cursor.date()
        for _ in range(800):  # generous cap; guards against an all-off calendar
            for ws, we in self._windows_for_day(d):  # sorted by start
                if we > cursor:
                    return ws, we
            d = d + timedelta(days=1)
        raise NoWorkingWindow("no working window found within horizon")

    def advance(self, start: datetime, minutes: float) -> datetime:
        cursor = start
        remaining = float(minutes)
        # A non-finite duration (NaN/inf) would spin forever — fail loud instead.
        if not math.isfinite(remaining):
            raise ValueError(f"advance() got a non-finite duration: {minutes!r}")
        win_start, win_end = self._next_window(cursor)  # raises NoWorkingWindow
        if cursor < win_start:
            cursor = win_start
        guard = 0
        while guard < 100000:
            guard += 1
            avail = (win_end - cursor).total_seconds() / 60.0
            if remaining <= avail + 1e-9:
                return cursor + timedelta(minutes=remaining)
            remaining -= avail
            win_start, win_end = self._next_window(win_end)
            cursor = win_start
        raise ValueError("advance() exceeded its window guard — duration too large")

    def working_minutes_between(self, a: datetime, b: datetime) -> float:
        """Working minutes in [a, b] — time a machine was available between two
        points (skipping nights & off days). 0 if the clock has no windows."""
        if a is None or b is None or b <= a or not self.intervals:
            return 0.0
        total = 0.0
        try:
            win_start, win_end = self._next_window(a)
        except NoWorkingWindow:
            return 0.0
        guard = 0
        while win_start < b and guard < 4000:
            guard += 1
            s = max(win_start, a)
            e = min(win_end, b)
            if e > s:
                total += (e - s).total_seconds() / 60.0
            try:
                win_start, win_end = self._next_window(win_end)
            except NoWorkingWindow:
                break
        return total
