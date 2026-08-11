"""Short-job staffing exception (2026-07-24 overhaul): an operator is reserved for a
machine only for the DURATION of the work (interval-based), so a short job frees them to
man another idle machine later that shift — while a full-shift job still locks them
(one-operator-per-machine-per-shift preserved by construction). Never double-booked."""
from datetime import date, datetime

from ppc_engine.domain.resources import Shift
from ppc_engine.scheduler.staffing import StaffingBoard

_D = date(2025, 3, 5)
_F = Shift.FIRST


def _t(h, m=0):
    return datetime(2025, 3, 5, h, m)


def test_short_job_frees_the_operator_afterwards():
    b = StaffingBoard()
    b.commit("X", _D, _F, "O", _t(9), _t(10))          # 1-hour job on machine X
    assert b.free_during("O", _t(10), _t(12))          # free after -> can man another machine
    assert not b.free_during("O", _t(9, 30), _t(10, 30))  # busy during the job


def test_operator_can_chain_two_short_jobs_without_double_booking():
    b = StaffingBoard()
    b.commit("X", _D, _F, "O", _t(9), _t(10))
    b.commit("Y", _D, _F, "O", _t(10), _t(11))          # a second machine in the free window
    assert not b.free_during("O", _t(9, 30), _t(10, 30))   # both intervals block
    assert not b.free_during("O", _t(10, 30), _t(11))
    assert b.free_during("O", _t(11), _t(19))              # free after both


def test_full_shift_job_keeps_the_operator_locked_all_shift():
    b = StaffingBoard()
    b.commit("X", _D, _F, "O", _t(8), _t(19))           # fills the whole first shift
    # No free window remains this shift -> stability preserved (can't man another machine).
    assert not b.free_during("O", _t(12), _t(13))
    assert not b.free_during("O", _t(18), _t(19))
