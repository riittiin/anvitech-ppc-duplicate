import pytest

from roster_engine import release
from roster_engine.domain import Job, Op


def _op(seq, kind, cycle=5.0, name="OP"):
    return Op(seq, name, kind, cycle, ("CNC1",) if kind == "machining" else ("MD1",))


def _job(ops, qty=100):
    return Job("B1", "ITEM", qty, None, ("SO1",), tuple(ops), None)


@pytest.mark.parametrize("overlap,qty,expected", [
    (0.8, 100, 80),        # the owner's example: 80 of 100 pieces
    (0.5, 100, 50),
    (1.0, 100, 100),       # fully sequential
    (0.8, 7, 6),           # ceil(5.6) -> a WHOLE piece, never 5.6
    (0.55, 3, 2),          # ceil(1.65)
    (0.8, 1, 1),           # a single piece can never release early
])
def test_release_is_always_a_whole_number_of_pieces(overlap, qty, expected):
    assert release.released_pieces(overlap, qty) == expected


def test_eighty_percent_means_eighty_pieces_not_twenty():
    """The live engine computes (1 - overlap) and so releases at 20 pieces for an
    overlap of 0.8 — the complement of RULES.md:114 and of its own docstring.
    This pins the correct direction."""
    assert release.released_pieces(0.8, 100) == 80
    assert release.released_pieces(0.9, 100) == 90


def test_setup_is_excluded_from_the_percentage_but_still_precedes_cutting():
    job = _job([_op(1, "machining", cycle=5.0)])
    # 90 setup + 80 pieces x 5 min of cutting
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.8, setup_min=90.0) == 90.0 + 400.0


def test_manual_steps_carry_no_setup():
    job = _job([_op(1, "manual", cycle=2.0)])
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.5, setup_min=90.0) == 100.0


def test_os_and_dispatch_never_overlap_in_either_direction():
    assert not release.overlaps(_op(1, "machining"), _op(2, "outsourced"))
    assert not release.overlaps(_op(1, "outsourced"), _op(2, "machining"))
    assert not release.overlaps(_op(1, "machining"), _op(2, "dispatch"))
    assert release.overlaps(_op(1, "machining"), _op(2, "manual"))


def test_a_no_cutting_step_does_not_overlap():
    """RULES.md:127 — a step with no cycle time produces nothing gradually, so its
    successor waits for it to finish."""
    assert not release.overlaps(_op(1, "manual", cycle=0.0), _op(2, "inspection"))


def test_a_finished_step_releases_immediately():
    job = _job([_op(1, "machining")], qty=0)
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.8, setup_min=90.0) == 0.0
