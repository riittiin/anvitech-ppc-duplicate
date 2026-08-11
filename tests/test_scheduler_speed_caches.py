"""The scheduler speedups are pure memoization — they must not change ANY result.

These lock in that the cached helpers return exactly what a fresh recomputation would,
and that a WorkClock's per-day window cache matches the uncached construction. (The
golden trace + the real-data optimizer numbers are the end-to-end proof; these guard
the individual caches so a future edit can't silently diverge.)"""
from datetime import date, datetime, timedelta

from engine.loaders import (normalize_resource_id, parse_resource_candidates,
                            _normalize_resource_id_cached, _parse_resource_candidates_cached)
from engine.models import WorkCalendar
from engine.worktime import WorkClock


def test_normalize_resource_id_matches_uncached():
    import re
    def uncached(raw):
        if raw is None:
            return ""
        return re.sub(r"[^A-Z0-9]", "", str(raw).upper())
    for s in ("CNC 4", "cnc4", "VMC-1", "MI 3", "", None, "OS", "61243661-01..", 7):
        assert normalize_resource_id(s) == uncached(s)


def test_parse_resource_candidates_matches_uncached_and_returns_fresh_list():
    import re
    def uncached(raw):
        if raw is None:
            return []
        out = []
        for token in re.split(r"[/&,]| or ", str(raw)):
            cid = normalize_resource_id(token)
            if cid and cid not in out:
                out.append(cid)
        return out
    for s in ("CNC3/CNC6", "CNC 3 or CNC 7", "MI1/MI2/MI3", "OS", "", None,
              "CNC4/CNC4", "A,B & C"):
        assert parse_resource_candidates(s) == uncached(s)
    # Must return a NEW list each call (caller may own it) even though the parse is cached.
    a = parse_resource_candidates("CNC3/CNC6")
    b = parse_resource_candidates("CNC3/CNC6")
    assert a == b and a is not b
    a.append("ZZZ")
    assert parse_resource_candidates("CNC3/CNC6") == ["CNC3", "CNC6"]   # cache unpolluted


def test_workclock_day_cache_matches_uncached_windows():
    cal = WorkCalendar()
    # A two-shift (crosses midnight) and a manual (single) clock.
    two = WorkClock(cal, [(8 * 60, 29 * 60)])       # 08:00 -> 05:00 next day
    manual = WorkClock(cal, [(9 * 60, 18 * 60)])     # 09:00 -> 18:00
    assert two._crosses_midnight is True
    assert manual._crosses_midnight is False
    for clk in (two, manual):
        for i in range(20):
            d = date(2026, 7, 6) + timedelta(days=i)
            if not cal.is_working_day(d):
                assert clk._windows_for_day(d) == ()
                continue
            base = datetime(d.year, d.month, d.day)
            expect = tuple((base + timedelta(minutes=s), base + timedelta(minutes=e))
                           for s, e in clk.intervals)
            assert clk._windows_for_day(d) == expect
            assert clk._windows_for_day(d) is clk._windows_for_day(d)   # cached, stable


def test_advance_identical_with_and_without_midnight_lookback():
    # The manual clock (no midnight cross) uses the cheaper same-day scan; prove it
    # reaches exactly the same instant as a brute-force day-back scan would.
    cal = WorkCalendar()
    manual = WorkClock(cal, [(9 * 60, 18 * 60)])
    start = datetime(2026, 7, 6, 11, 30)
    # 9 working hours/day; advancing 600 min (10 h) crosses one day boundary.
    end = manual.advance(start, 600)
    # Independently: 6.5h left on day1 (11:30->18:00=390min), 210min into day2 from 09:00.
    assert end == datetime(2026, 7, 7, 9, 0) + timedelta(minutes=210)
