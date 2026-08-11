"""Plan start rounds up to the next hour (never plans in the past).

Bug (2026-08-03): the plan clock was always anchored to 08:00 of the date, ignoring the
current time — so a run at 11:30pm planned from 8am that morning (15h in the past). Fix:
in AUTO mode the API sets a per-run floor = the next full hour after now (IST), and the
engine starts at max(08:00-of-date, floor). Fixed/testing dates keep 08:00.

⚠️ THIS BEHAVIOUR IS SWITCHED OFF LIVE (owner decision 2026-08-11 — the plan starts at
the 08:00 shift start again; see ``api.main.PLAN_START_NEXT_HOUR`` and
``tests/test_plan_start_shift_start.py``). The MECHANISM is deliberately kept, and this
file keeps testing it so flipping the flag back on is safe. Only the one test that
drives ``_resolve_config``'s auto branch needs the flag; everything below exercises
``_plan_config``/``_ceil_next_hour`` directly, which the flag does not gate.
"""
from datetime import date, datetime

import pytest

from engine.config import Config
from engine.new_engine import _plan_config


# --- engine: _plan_config applies the floor ---------------------------------------

def test_plan_config_no_floor_starts_at_8am():
    assert _plan_config(Config(plan_start_date=date(2025, 3, 3))).plan_start \
        == datetime(2025, 3, 3, 8, 0)


def test_plan_config_floor_after_8am_wins():
    cfg = Config(plan_start_date=date(2025, 3, 3), plan_start_floor="2025-03-03T14:00")
    assert _plan_config(cfg).plan_start == datetime(2025, 3, 3, 14, 0)


def test_plan_config_floor_before_8am_keeps_shift_start():
    cfg = Config(plan_start_date=date(2025, 3, 3), plan_start_floor="2025-03-03T06:00")
    assert _plan_config(cfg).plan_start == datetime(2025, 3, 3, 8, 0)


def test_plan_config_late_run_floor_rolls_to_next_day():
    # 11:30pm run -> floor 00:00 next day -> plan starts then, not 8am the past morning.
    cfg = Config(plan_start_date=date(2025, 3, 3), plan_start_floor="2025-03-04T00:00")
    ps = _plan_config(cfg).plan_start
    assert ps == datetime(2025, 3, 4, 0, 0)


def test_plan_config_floor_moves_the_clock_and_there_is_no_rotation_anchor():
    # The floor rolls the CLOCK into the next day. There is no shift-rotation anchor
    # any more (2026-08-05): an admin's chosen shift holds every week.
    d = date(2025, 3, 6)
    rolled = _plan_config(Config(plan_start_date=d, plan_start_floor="2025-03-07T00:00"))
    assert rolled.plan_start == datetime(2025, 3, 7, 0, 0)
    assert rolled.week_anchor is None


# --- API: _ceil_next_hour + _resolve_config ---------------------------------------

def test_ceil_next_hour():
    from api.main import _ceil_next_hour
    assert _ceil_next_hour(datetime(2026, 8, 3, 9, 30)) == datetime(2026, 8, 3, 10, 0)
    assert _ceil_next_hour(datetime(2026, 8, 3, 23, 30)) == datetime(2026, 8, 4, 0, 0)
    assert _ceil_next_hour(datetime(2026, 8, 3, 8, 0)) == datetime(2026, 8, 3, 9, 0)


def test_resolve_config_sets_floor_in_auto_mode(monkeypatch):
    import api.main as m
    monkeypatch.setattr(m, "PLAN_START_NEXT_HOUR", True)   # off live since 2026-08-11
    monkeypatch.setattr(m, "_ist_now", lambda: datetime(2026, 8, 2, 23, 30))
    monkeypatch.setattr(m, "_ist_today", lambda: date(2026, 8, 2))
    c = m._resolve_config(Config(plan_start_date=None))
    assert c.plan_start_date == date(2026, 8, 2)          # resolved to today
    assert c.plan_start_floor == "2026-08-03T00:00"       # next hour after 23:30 -> next day


def test_resolve_config_no_floor_for_fixed_date():
    from api.main import _resolve_config
    c = _resolve_config(Config(plan_start_date=date(2025, 3, 1)))
    assert c.plan_start_floor is None                      # fixed date -> reproducible 08:00


def test_resolve_config_clears_stale_floor_for_fixed_date():
    from api.main import _resolve_config
    c = _resolve_config(Config(plan_start_date=date(2025, 3, 1),
                               plan_start_floor="2030-01-01T05:00"))
    assert c.plan_start_floor is None


def test_inputs_signature_ignores_floor():
    import api.main as m
    a = m._inputs_signature(Config(plan_start_date=None))
    b = m._inputs_signature(Config(plan_start_date=None, plan_start_floor="2099-01-01T05:00"))
    assert a == b
