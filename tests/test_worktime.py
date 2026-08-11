"""WorkClock — shifts and the Thursday/holiday skip used by Rule 6."""
from datetime import datetime, date

import pytest

from engine.config import Config
from engine.models import WorkCalendar
from engine.worktime import WorkClock, NoWorkingWindow

# Minute-from-midnight windows (an end > 1440 crosses into the next day).
TWO_SHIFT = [(8 * 60, 24 * 60 + 5 * 60)]      # 08:00 -> 05:00 next day
MANUAL = [(9 * 60, 18 * 60)]                  # 09:00 -> 18:00
SECOND_ONLY = [(19 * 60, 24 * 60 + 5 * 60)]   # 19:00 -> 05:00 next day


def test_within_single_window():
    clock = WorkClock(WorkCalendar(), Config())
    # 2025-03-05 is a Wednesday (working). 120 min from 08:00 -> 10:00.
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 120)
    assert end == datetime(2025, 3, 5, 10, 0)


def test_skips_thursday():
    cfg = Config()
    clock = WorkClock(WorkCalendar(), cfg)
    # Wed 2025-03-05 08:00, the working window runs to Thu 05:00 (21h = 1260 min,
    # 2nd-shift spillover). 1260 + 60 more min must skip Thu (off) to Fri 09:00.
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 1260 + 60)
    assert end == datetime(2025, 3, 7, 9, 0)   # Friday
    assert end.weekday() != 3                  # not Thursday


def test_skips_holiday():
    cfg = Config()
    cal = WorkCalendar(holidays=[date(2025, 3, 5)])  # make that Wednesday a holiday
    clock = WorkClock(cal, cfg)
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 60)
    # Holiday -> first work happens next working day (Fri; Thu is weekly off).
    assert end == datetime(2025, 3, 7, 9, 0)


def test_advance_zero_snaps_to_working_window():
    clock = WorkClock(WorkCalendar(), Config())
    # 06:00 is before the 08:00 shift start -> snaps forward to 08:00.
    snapped = clock.advance(datetime(2025, 3, 5, 6, 0), 0)
    assert snapped == datetime(2025, 3, 5, 8, 0)


# --- interval-based clocks (the operator-logic generalization) ------------- #
def test_from_config_matches_legacy_two_shift():
    legacy = WorkClock(WorkCalendar(), Config())
    explicit = WorkClock(WorkCalendar(), TWO_SHIFT)
    start = datetime(2025, 3, 5, 8, 0)
    assert legacy.advance(start, 1500) == explicit.advance(start, 1500)


def test_manual_window_9_to_6():
    clock = WorkClock(WorkCalendar(), MANUAL)
    # 09:00 start; before that snaps to 09:00; window is 9h (540 min).
    assert clock.advance(datetime(2025, 3, 5, 7, 0), 0) == datetime(2025, 3, 5, 9, 0)
    assert clock.advance(datetime(2025, 3, 5, 9, 0), 60) == datetime(2025, 3, 5, 10, 0)
    # 540 min fills the day; +60 spills to the next working day (Thu off -> Fri).
    assert clock.advance(datetime(2025, 3, 5, 9, 0), 540 + 60) == datetime(2025, 3, 7, 10, 0)


def test_second_shift_only_crosses_midnight():
    clock = WorkClock(WorkCalendar(), SECOND_ONLY)
    # Wed 19:00 + 120 -> Wed 21:00; the window runs to Thu 05:00 (600 min).
    assert clock.advance(datetime(2025, 3, 5, 19, 0), 120) == datetime(2025, 3, 5, 21, 0)
    # 600 fills to Thu 05:00; +60 skips Thu (off) to Fri 19:00 + 60 = Fri 20:00.
    assert clock.advance(datetime(2025, 3, 5, 19, 0), 660) == datetime(2025, 3, 7, 20, 0)


def test_both_shifts_merge_to_continuous_window():
    split = WorkClock(WorkCalendar(), [(8 * 60, 19 * 60), (19 * 60, 24 * 60 + 5 * 60)])
    assert split.intervals == TWO_SHIFT          # touching intervals merged
    legacy = WorkClock(WorkCalendar(), Config())
    s = datetime(2025, 3, 5, 8, 0)
    assert split.advance(s, 1500) == legacy.advance(s, 1500)


def test_empty_intervals_raise_no_working_window():
    clock = WorkClock(WorkCalendar(), [])
    with pytest.raises(NoWorkingWindow):
        clock.advance(datetime(2025, 3, 5, 8, 0), 10)
    # working_minutes_between is graceful (0), never raises.
    assert clock.working_minutes_between(datetime(2025, 3, 5, 8, 0),
                                         datetime(2025, 3, 6, 8, 0)) == 0.0
