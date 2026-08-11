"""The plan must re-optimize when its START DATE moves, not only when quantities do.

`_maybe_auto_optimize` decides "has anything changed since the last search?" from
two fingerprints:

  * `_current_book_sig()`   — derived from remaining QUANTITIES
  * `_inputs_signature()`   — computed on the SAVED (unresolved) config, so an
                              auto plan_start_date is None there. That is
                              deliberate, so a moving 'today' never looks like a
                              settings change to the staleness banner.

Neither moves when the calendar rolls over. So on any day the plan start advances
without new production quantities — a Monday after the weekend, a holiday, a day
someone presses "Done entering" before punching — the app answers "nothing new to
re-plan" and keeps replaying a sequence optimized for a different start date.

Measured on the live book (2026-08-12): the applied ranks were computed on
08-11 and gave 397 late-days replayed against the 08-12 book; the SAME optimizer
re-run on 08-12 gave 365. A day of drift cost 32 late-days.

The fix records the EFFECTIVE plan start alongside the two fingerprints and
treats a change in it as a real change.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from api import main  # noqa: E402


class TestPlanStartIsPartOfTheFingerprint:

    def test_helper_exists(self):
        assert hasattr(main, "_current_plan_start_sig")

    def test_it_returns_the_effective_start_as_an_iso_date(self):
        got = main._current_plan_start_sig()
        assert isinstance(got, str)
        assert date.fromisoformat(got)          # parses

    def test_it_moves_when_the_plan_start_moves(self, monkeypatch):
        seen = []

        def fake(actuals, start, calendar):
            seen.append(start)
            return date(2026, 8, 13) if len(seen) > 1 else date(2026, 8, 12)

        monkeypatch.setattr(main.orderbook, "effective_plan_start_date", fake)
        first = main._current_plan_start_sig()
        second = main._current_plan_start_sig()
        assert first != second


class TestTheTriggerReactsToADateChange:
    """`_matches_last_search` is the single place the 'nothing changed' answer is
    decided; it must consider all three fingerprints."""

    def test_helper_exists(self):
        assert hasattr(main, "_matches_last_search")

    def test_identical_fingerprints_mean_nothing_changed(self):
        rec = {"book_sig": "B", "inputs_sig": "I", "plan_start": "2026-08-12"}
        assert main._matches_last_search(rec, "B", "I", "2026-08-12") is True

    def test_a_changed_book_is_a_change(self):
        rec = {"book_sig": "B", "inputs_sig": "I", "plan_start": "2026-08-12"}
        assert main._matches_last_search(rec, "B2", "I", "2026-08-12") is False

    def test_a_changed_setting_is_a_change(self):
        rec = {"book_sig": "B", "inputs_sig": "I", "plan_start": "2026-08-12"}
        assert main._matches_last_search(rec, "B", "I2", "2026-08-12") is False

    def test_A_NEW_DAY_IS_A_CHANGE(self):
        """The whole point: same book, same settings, new plan start."""
        rec = {"book_sig": "B", "inputs_sig": "I", "plan_start": "2026-08-12"}
        assert main._matches_last_search(rec, "B", "I", "2026-08-13") is False

    def test_a_record_predating_the_fix_does_not_block_a_re_plan(self):
        """Old records carry no plan_start. Treat that as 'unknown, so re-plan'
        rather than 'matches' — one extra search after deploy, then self-healing."""
        rec = {"book_sig": "B", "inputs_sig": "I"}
        assert main._matches_last_search(rec, "B", "I", "2026-08-12") is False

    def test_an_absent_inputs_sig_is_still_tolerated(self):
        """Pre-existing behaviour: applied meta without inputs_sig matched on book
        alone. Keep that, so the fix does not change an unrelated path."""
        rec = {"book_sig": "B", "plan_start": "2026-08-12"}
        assert main._matches_last_search(rec, "B", "I", "2026-08-12") is True

    def test_an_empty_record_never_matches(self):
        assert main._matches_last_search({}, "B", "I", "2026-08-12") is False
        assert main._matches_last_search(None, "B", "I", "2026-08-12") is False


class TestWhatGetsRecorded:

    def test_the_optimize_state_carries_the_plan_start(self):
        assert "searched_plan_start" in main._OPTIMIZE

    def test_applied_meta_records_the_plan_start(self, monkeypatch):
        """So a later day can tell the applied ranks were built for another date."""
        import inspect
        src = inspect.getsource(main)
        assert '"plan_start": _current_plan_start_sig()' in src, \
            "the applied-optimization meta must record the plan start"
