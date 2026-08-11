"""The plan starts at the SHIFT START (08:00), not at the next full hour.

Owner decision 2026-08-11 (`docs/superpowers/specs/2026-08-11-plan-start-at-shift-start-design.md`):
the 2026-08-03 "next full hour" clock is reverted. Press Optimize at 09:30 and the
schedule starts 08:00 that morning, because the book needs the whole shift. It is
ALWAYS 08:00 of today — a late-evening run deliberately plans from this morning's
shift start, which the owner accepts.

The mechanism is not deleted, only switched off: ``api.main.PLAN_START_NEXT_HOUR``
flips it back in one line, and the tests below pin BOTH sides of that switch.
"""
import importlib
from datetime import date, datetime

import pytest

from engine.config import Config
from engine.new_engine import _plan_config


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _plan_start(m, now):
    """The instant the engine would actually begin planning, resolved at ``now``."""
    m._ist_now = lambda: now
    m._ist_today = lambda: now.date()
    return _plan_config(m._resolve_config(Config(plan_start_date=None))).plan_start


# --- the shift start, whatever the hour ------------------------------------- #

def test_a_morning_run_plans_from_the_shift_start_not_the_next_hour():
    """The owner's example: press Optimize at 09:30 Tuesday, the schedule starts
    08:00 Tuesday. It used to start 10:00."""
    m = _api()
    assert _plan_start(m, datetime(2026, 8, 11, 9, 30)) == datetime(2026, 8, 11, 8, 0)


def test_auto_mode_carries_no_plan_start_floor():
    m = _api()
    m._ist_now = lambda: datetime(2026, 8, 11, 10, 30)
    m._ist_today = lambda: date(2026, 8, 11)
    cfg = m._resolve_config(Config(plan_start_date=None))
    assert cfg.plan_start_date == date(2026, 8, 11)
    assert cfg.plan_start_floor is None


def test_a_late_evening_run_still_plans_from_this_mornings_shift_start():
    """Deliberate and owner-confirmed: 23:30 plans from 08:00 THAT morning, not from
    00:00 the next day. Accepting a stale first day is the price of the full shift."""
    m = _api()
    assert _plan_start(m, datetime(2026, 8, 11, 23, 30)) == datetime(2026, 8, 11, 8, 0)


def test_the_plan_start_is_identical_at_every_hour_of_the_day():
    """The 2026-08-07 guarantee — one plan, one set of dates — must survive the
    revert. It gets STRONGER: 08:00-of-today cannot drift within the day at all."""
    m = _api()
    # 09:30 FIRST on purpose: under the old behaviour that stamps a 10:00 clock which
    # then holds all day, so the set would be {10:00} and this test would fail. Leading
    # with an hour before 08:00 would make it pass vacuously under both behaviours.
    starts = {_plan_start(m, datetime(2026, 8, 11, h, mi))
              for h, mi in [(9, 30), (6, 0), (12, 15), (16, 45), (23, 59)]}
    assert starts == {datetime(2026, 8, 11, 8, 0)}


def test_a_new_day_moves_the_plan_start_to_that_days_shift_start():
    m = _api()
    # 10:20, not an early hour: the old behaviour would floor this to 11:00.
    assert _plan_start(m, datetime(2026, 8, 12, 10, 20)) == datetime(2026, 8, 12, 8, 0)


# --- a finished optimization no longer moves the clock ---------------------- #

def test_a_finished_optimization_does_not_move_the_plan_start():
    """The clock used to be stamped to the next full hour when a contest landed. With
    no clock there is nothing to stamp, and the plan start must not budge."""
    import time as _time
    m = _api()
    m._metrics_for_ranks = lambda *a, **k: None          # keep the contest's own numbers
    before = _plan_start(m, datetime(2026, 8, 11, 9, 1))

    m._ist_now = lambda: datetime(2026, 8, 11, 9, 1)
    m._ist_today = lambda: date(2026, 8, 11)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="job1", started_mono=_time.monotonic())
    m._finalize_optimize("job1", Config(plan_start_date=None), None, "deep",
                         winner_overlap=70, ranks={"k": 1}, best={"total_late_days": 1},
                         evals=1, table=[], cancelled=False)

    after = _plan_start(m, datetime(2026, 8, 11, 15, 40))
    assert after == before == datetime(2026, 8, 11, 8, 0)


def test_a_finished_optimization_writes_no_plan_clock_to_the_store():
    """Nothing may read a stale pinned clock later, so nothing may write one."""
    import time as _time
    from engine import book_store
    m = _api()
    m._metrics_for_ranks = lambda *a, **k: None
    m._ist_now = lambda: datetime(2026, 8, 11, 9, 1)
    m._ist_today = lambda: date(2026, 8, 11)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(job_id="job1", started_mono=_time.monotonic())
    m._finalize_optimize("job1", Config(plan_start_date=None), None, "deep",
                         winner_overlap=70, ranks={"k": 1}, best={"total_late_days": 1},
                         evals=1, table=[], cancelled=False)
    assert book_store.load_plan_start_floor() is None


# --- a fixed (testing) date is untouched ------------------------------------ #

def test_a_fixed_plan_start_date_still_starts_at_0800_of_that_date():
    m = _api()
    cfg = m._resolve_config(Config(plan_start_date=date(2025, 3, 3)))
    assert cfg.plan_start_floor is None
    assert _plan_config(cfg).plan_start == datetime(2025, 3, 3, 8, 0)


# --- the switch still works both ways --------------------------------------- #

def test_the_switch_restores_the_next_hour_behaviour(monkeypatch):
    """The mechanism is off, not gone. One line brings it back."""
    m = _api()
    monkeypatch.setattr(m, "PLAN_START_NEXT_HOUR", True)
    assert _plan_start(m, datetime(2026, 8, 11, 9, 30)) == datetime(2026, 8, 11, 10, 0)


def test_the_switch_is_off_by_default():
    m = _api()
    assert m.PLAN_START_NEXT_HOUR is False
