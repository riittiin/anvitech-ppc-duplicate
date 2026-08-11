"""The Optimize settings sweep (2026-07-15 spec; fair-contest contract per the
owner's decisions the same day): one click tunes the overlap % inside ONE
total budget (~1,000 plans live). ``budget_evals`` is the TOTAL; it is split
EQUALLY across the contenders — the current setting plus OVERLAP_CANDIDATES
(50–80; 90/100 were dropped after losing every measured contest, but a current
setting outside the list always joins its own contest) — and the best plan
wins. The current setting has no depth advantage; it just runs first and wins
exact ties (no churn). Promise-guard hook can veto candidates; Apply persists
the winning overlap into the saved plan config with a matching fingerprint."""
import io
from dataclasses import replace
from datetime import date

import pytest

from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.optimizer import score
from tests.sample_workbook import build_sample_bytes


def _book(overlap=80):
    so_lines, masters = load_all(io.BytesIO(build_sample_bytes()))
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return so_lines, cfg, masters


# --------------------------------------------------------------------------- #
# Pure sweep mechanics
# --------------------------------------------------------------------------- #
def test_sweep_is_a_fair_contest_equal_split_of_one_total_budget():
    """The fair-contest contract (owner decisions after a live regression —
    unequal depths misrank settings, so every contender gets the SAME share of
    ONE total budget and the best plan wins)."""
    so, cfg, masters = _book(overlap=80)
    budget = 80                     # total; 4 contenders (80 ∈ candidates) → 20 each
    sw = optimizer.sweep_optimize(so, cfg, masters, budget_evals=budget, seed=42)

    # Every eligible contender ran the same equal share: no favoritism.
    eligible = [t for t in sw.table if t.get("eligible")]
    assert {t["overlap"] for t in eligible} == set(optimizer.OVERLAP_CANDIDATES)
    each = budget // len(optimizer.sweep_contenders(cfg.overlap_percent))
    assert all(t["evals"] == each for t in eligible)
    assert sw.evals == optimizer.sweep_total_evals(budget, cfg.overlap_percent)

    # The winner is the best-scoring contender outright; equal seeds+depth mean
    # the current setting's own run is reproducible by plain optimize().
    best_row = min(eligible, key=lambda t: score(t["best"]))
    assert score(sw.result.best) == score(best_row["best"])
    cur_same_depth = optimizer.optimize(so, cfg, masters, budget_evals=each, seed=42)
    assert score(sw.result.best) <= score(cur_same_depth.best)
    if sw.overlap_percent != 80:
        assert score(sw.result.best) < score(cur_same_depth.best)  # ties keep current
    assert sw.result.ranks                                         # persistable artifact


def test_sweep_includes_a_current_setting_outside_the_candidate_list():
    """A saved overlap not in OVERLAP_CANDIDATES (e.g. 90) must still be a
    contender in its own contest — never silently excluded."""
    so, cfg, masters = _book(overlap=90)
    sw = optimizer.sweep_optimize(so, cfg, masters, budget_evals=100, seed=42)
    eligible = {t["overlap"] for t in sw.table if t.get("eligible")}
    assert eligible == set(optimizer.OVERLAP_CANDIDATES) | {90}
    each = 100 // 5
    assert all(t["evals"] == each for t in sw.table if t.get("eligible"))


def test_sweep_is_deterministic():
    so, cfg, masters = _book()
    a = optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42)
    b = optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42)
    assert (a.overlap_percent, a.result.best, a.result.ranks) == \
           (b.overlap_percent, b.result.best, b.result.ranks)


def test_sweep_progress_is_monotonic_across_candidates():
    so, cfg, masters = _book()
    seen = []
    optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42,
                             on_progress=lambda done, best: seen.append(done))
    assert seen == sorted(seen)
    assert seen[-1] <= optimizer.sweep_total_evals(60)


# --------------------------------------------------------------------------- #
# API wiring: result carries best_overlap; Apply persists overlap + fingerprint
# --------------------------------------------------------------------------- #
pytest.importorskip("fastapi")
from engine import book_store
from engine.models import Order
from tests.sample_workbook import ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def test_optimize_result_carries_best_overlap_and_apply_persists_it():
    m = _api()
    _seed_book()
    st = m._start_optimize(budget_evals=60, label="quick", background=False)
    assert st["state"] == "done"
    assert "best_overlap" in st

    meta = m._optimize_apply()
    assert meta["best_overlap"] == st["best_overlap"]
    # The winning overlap is now THE saved plan setting (visible in Settings).
    saved = m._load_plan_config()
    assert saved.overlap_percent == st["best_overlap"]
    # Right after Apply the fingerprint matches -> no staleness warning.
    assert m._plan(saved)["optimize_meta"]["inputs_changed"] is False


def test_apply_with_current_setting_winning_changes_nothing():
    """If the current overlap wins, Apply must not churn the saved config."""
    m = _api()
    _seed_book()
    before = m._load_plan_config()
    st = m._start_optimize(budget_evals=60, label="quick", background=False)
    m._optimize_apply()
    after = m._load_plan_config()
    if st["best_overlap"] == before.overlap_percent:
        assert after.to_dict() == before.to_dict()
    else:
        assert after.overlap_percent == st["best_overlap"]
