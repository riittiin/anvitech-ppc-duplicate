import pytest

from roster_engine import release
from roster_engine.domain import Job, Op


def _op(seq, kind, cycle=5.0, name="OP"):
    return Op(seq, name, kind, cycle, ("CNC1",) if kind == "machining" else ("MD1",))


def _job(ops, qty=100, remaining=None):
    return Job("B1", "ITEM", qty, None, ("SO1",), tuple(ops), remaining)


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


def test_release_is_computed_from_remaining_qty_not_batch_qty():
    """CLAUDE.md 2026-08-11: a re-plan's release must derive from what this STEP
    still owes (batch-level), never from the original batch qty a per-line number
    might reflect. Batch of 100, but this step (seq 1) has only 40 left — at
    overlap 0.8 the release must be ceil(0.8 * 40) = 32 pieces, NOT
    ceil(0.8 * 100) = 80. The two numbers are chosen far enough apart that only
    one can be right."""
    job = _job([_op(1, "machining", cycle=1.0)], qty=100, remaining={1: 40})
    # From remaining (correct): 90 setup + 32 pieces x 1 min = 122.0
    # From job.qty instead (the reverted mutant): 90 + 80 x 1 = 170.0
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.8, setup_min=90.0) == 122.0


def test_release_falls_back_to_batch_qty_when_step_absent_from_remaining():
    """The other direction of the same interaction: a step with NO entry in
    ``remaining`` (e.g. a step nothing has been punched against yet) must fall
    back to the full batch qty, not to zero or some other default."""
    op1 = _op(1, "machining", cycle=1.0)
    op2 = _op(2, "machining", cycle=1.0)
    job = _job([op1, op2], qty=100, remaining={1: 40})  # op 2 has no entry
    # op 2 falls back to the full batch qty of 100: 90 setup + ceil(0.8*100)=80 x 1
    assert release.work_min_before_release(
        job, op2, overlap=0.8, setup_min=90.0) == 170.0


def test_dispatch_as_predecessor_never_overlaps():
    """DISPATCH is a milestone, not just a non-overlapping successor (already
    covered) — it also never hands anything over gradually as a PREDECESSOR."""
    assert not release.overlaps(_op(1, "dispatch"), _op(2, "machining"))


def test_a_no_cutting_step_waits_for_full_completion_not_a_partial_release():
    """Direct test of work_min_before_release's full-completion-wait branch
    (RULES.md:127) — ``overlaps()`` above only covers the start GATE; this covers
    the minutes-before-release CALCULATION for the same rule: a step that does not
    hand pieces over gradually must not use ``released_pieces`` at all.

    THE FIXTURE IS THE WHOLE TEST (2026-08-13 review). It used to use a manual
    step at ``cycle=0.0``, where BOTH branches evaluate to 0.0 — so it could not
    discriminate, and deleting the branch it is named for kept it green. This
    repo's rule is that a test passing under mutation tests nothing.

    An OUTSOURCED block at a nonzero cycle is the discriminating case, and it is
    the real one: a vendor delivers the whole lot at once, so its successor waits
    for all 350 minutes. Under the deleted branch it would be released after
    ``ceil(0.2 x 50) = 10`` pieces, i.e. 70 minutes — the successor starting on
    parts still at the vendor. (The branch is unreachable through the scheduler,
    which gates both call sites on ``overlaps()``; it is kept because it is the
    rule, and this is what makes keeping it honest.)"""
    job = _job([_op(1, "outsourced", cycle=7.0)], qty=50)
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.2, setup_min=90.0) == 350.0
    # ...and the zero-cycle in-house case the old fixture covered, still pinned.
    job = _job([_op(1, "manual", cycle=0.0)], qty=50)
    assert release.work_min_before_release(
        job, job.ops[0], overlap=0.2, setup_min=90.0) == 0.0
