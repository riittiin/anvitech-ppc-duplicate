"""The contest searches SEEDS as well as overlap and machine-set.

The search is an iterated local search from a dispatch-rule seed, so its answer
depends on the RNG. Measured on the live book at a fixed overlap (86), three
seeds gave 389 / 365 / 365 late-days — a 24-day spread from nothing but the
random stream. Production hardcodes one seed (``api.main._OPT_SEED = 42``) and
spends its whole 16,800-plan budget sweeping overlap × machine-set, so which
answer it lands on is luck.

The repo already knew: ``ppc_engine/optimize/offload.py`` records "seeds spread
~4% in final score, and a lucky seed at 600 plans beat an unlucky one at 1500.
Multi-start (many seeds, keep the best) is the real lever."

``contest_jobs`` therefore yields ``(overlap, flexible, seed)`` triples — still
the SINGLE source of truth shared by ``run_contest`` and the sharded worker, so
the two can never drift.

BACKWARD COMPATIBILITY is the safety property: a payload with no ``seeds`` key
produces exactly the jobs it always did, at the payload's own seed, so an
in-flight cloud job from an older deploy still runs correctly.
"""
import io
from datetime import date

import pytest

from engine import optimize_service as osvc
from engine import optimizer
from engine.config import Config
from engine.models import Order


def _payload(*, budget=6, candidates=(60, 80), seeds=None):
    """Real payload on the fully-staffed new-engine sample book (mirrors
    tests/test_optimize_shard.py::_new_engine_payload)."""
    from engine.loaders import load_all
    from tests.new_sample_workbook import build_new_sample_bytes
    raw = build_new_sample_bytes()
    so_lines, _ = load_all(io.BytesIO(raw))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                 apply_operator_logic=True, overlap_percent=candidates[0])
    cfg.validate()
    kw = {"seeds": list(seeds)} if seeds is not None else {}
    return osvc.build_payload(orders, [], raw, cfg, seed=1,
                              candidates=list(candidates),
                              budget_per_candidate=budget, **kw)


# --------------------------------------------------------------------------- #
# Job construction
# --------------------------------------------------------------------------- #

class TestContestJobs:

    def test_jobs_are_overlap_flexible_seed_triples(self):
        for job in osvc.contest_jobs(_payload()):
            assert len(job) == 3

    def test_without_seeds_the_payload_seed_is_used(self):
        payload = _payload()
        assert {seed for _, _, seed in osvc.contest_jobs(payload)} == {payload["seed"]}

    def test_without_seeds_the_job_COUNT_is_unchanged(self):
        """The compatibility guarantee: an old payload costs exactly what it did."""
        payload = _payload()
        cfg = Config.from_dict(payload["config"])
        knob, _ = optimizer.knob_for(cfg)
        contenders = optimizer.sweep_contenders(getattr(cfg, knob), payload["candidates"])
        assert len(osvc.contest_jobs(payload)) == len(contenders) * 2   # x2 machine-sets

    def test_seeds_multiply_the_jobs(self):
        one = len(osvc.contest_jobs(_payload()))
        three = len(osvc.contest_jobs(_payload(seeds=[1, 2, 3])))
        assert three == one * 3

    def test_every_overlap_is_paired_with_every_seed(self):
        jobs = osvc.contest_jobs(_payload(seeds=[1, 2, 3]))
        overlaps = {ov for ov, _, _ in jobs}
        for ov in overlaps:
            assert {s for o, _, s in jobs if o == ov} == {1, 2, 3}

    def test_jobs_are_unique(self):
        jobs = osvc.contest_jobs(_payload(seeds=[1, 2, 3]))
        assert len(set(jobs)) == len(jobs)

    def test_the_base_seed_runs_first(self):
        """Order matters: an early Stop must keep the current-settings answer
        fully searched, which is why the payload's own seed leads."""
        jobs = osvc.contest_jobs(_payload(seeds=[7, 1, 9]))
        assert jobs[0][2] == 1                       # payload seed == 1

    def test_a_duplicate_of_the_base_seed_is_not_run_twice(self):
        jobs = osvc.contest_jobs(_payload(seeds=[1, 1, 2]))
        assert len(set(jobs)) == len(jobs)

    def test_order_is_deterministic(self):
        p = _payload(seeds=[1, 2, 3])
        assert osvc.contest_jobs(p) == osvc.contest_jobs(p)


# --------------------------------------------------------------------------- #
# Running a candidate at a given seed
# --------------------------------------------------------------------------- #

class TestRunCandidateSeed:

    def test_the_row_reports_the_seed_it_ran(self):
        p = _payload()
        row = osvc.run_candidate(p, 60, True, seed=5)
        assert row["seed"] == 5

    def test_no_seed_argument_falls_back_to_the_payload_seed(self):
        p = _payload()
        assert osvc.run_candidate(p, 60, True)["seed"] == p["seed"]

    def test_the_same_seed_reproduces_the_same_ranks(self):
        p = _payload()
        a = osvc.run_candidate(p, 60, True, seed=5)
        b = osvc.run_candidate(p, 60, True, seed=5)
        assert a["ranks"] == b["ranks"]

    def test_the_seed_actually_REACHES_the_search(self, monkeypatch):
        """The load-bearing check. Two seeds cannot be compared behaviourally on
        this fixture — it has 2 orders, so every seed exhausts the permutations
        and converges on the same answer. Asserting on outputs would pass even if
        the seed were silently dropped (the mistake made earlier in this project
        with operator qualifications). So assert the plumbing directly."""
        seen = {}
        real = optimizer.optimize

        def spy(*a, **kw):
            seen["seed"] = kw.get("seed")
            return real(*a, **kw)

        monkeypatch.setattr(optimizer, "optimize", spy)
        osvc.run_candidate(_payload(), 60, True, seed=4242)
        assert seen["seed"] == 4242

    def test_without_an_explicit_seed_the_payload_seed_reaches_the_search(self, monkeypatch):
        seen = {}
        real = optimizer.optimize

        def spy(*a, **kw):
            seen["seed"] = kw.get("seed")
            return real(*a, **kw)

        monkeypatch.setattr(optimizer, "optimize", spy)
        p = _payload()
        osvc.run_candidate(p, 60, True)
        assert seen["seed"] == p["seed"]


# --------------------------------------------------------------------------- #
# Winner selection
# --------------------------------------------------------------------------- #

class TestPickWinner:

    def test_the_best_score_wins_regardless_of_seed(self):
        rows = [{"overlap": 60, "flexible": True, "seed": 1, "eligible": True,
                 "best": {"ontime_breach": 500.0, "makespan_days": 10.0}},
                {"overlap": 60, "flexible": True, "seed": 9, "eligible": True,
                 "best": {"ontime_breach": 100.0, "makespan_days": 10.0}}]
        assert osvc.pick_winner(60, True, rows)["seed"] == 9

    def test_an_exact_tie_keeps_the_base_seed(self):
        """No churn: if another seed merely equals the incumbent, do not switch."""
        same = {"ontime_breach": 100.0, "makespan_days": 10.0}
        rows = [{"overlap": 60, "flexible": True, "seed": 9, "eligible": True, "best": dict(same)},
                {"overlap": 60, "flexible": True, "seed": 1, "eligible": True, "best": dict(same)}]
        assert osvc.pick_winner(60, True, rows, base_seed=1)["seed"] == 1

    def test_rows_without_a_seed_still_work(self):
        """Rows from an older worker carry no seed — must not crash."""
        rows = [{"overlap": 60, "flexible": True, "eligible": True,
                 "best": {"ontime_breach": 100.0, "makespan_days": 10.0}}]
        assert osvc.pick_winner(60, True, rows) is not None


# --------------------------------------------------------------------------- #
# Sharding still covers every job exactly once
# --------------------------------------------------------------------------- #

class TestSharding:

    def test_the_shards_partition_the_jobs(self):
        p = _payload(seeds=[1, 2, 3])
        jobs = osvc.contest_jobs(p)
        total = 4
        seen = []
        for i in range(total):
            seen.extend(jobs[i::total])
        assert sorted(seen) == sorted(jobs)
        assert len(seen) == len(jobs)

    def test_more_shards_than_jobs_leaves_some_empty_but_loses_nothing(self):
        p = _payload(seeds=[1, 2])
        jobs = osvc.contest_jobs(p)
        total = len(jobs) + 3
        seen = []
        for i in range(total):
            seen.extend(jobs[i::total])
        assert sorted(seen) == sorted(jobs)


# --------------------------------------------------------------------------- #
# The activation knob
# --------------------------------------------------------------------------- #

class TestCloudSeedsKnob:

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("OPTIMIZE_CLOUD_SEEDS", raising=False)
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", ())
        assert osvc.cloud_seeds() == []

    def test_the_constant_is_used_when_no_env_is_set(self, monkeypatch):
        monkeypatch.delenv("OPTIMIZE_CLOUD_SEEDS", raising=False)
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7, 99))
        assert osvc.cloud_seeds() == [7, 99]

    def test_env_overrides_the_constant(self, monkeypatch):
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7,))
        monkeypatch.setenv("OPTIMIZE_CLOUD_SEEDS", "11,22,33")
        assert osvc.cloud_seeds() == [11, 22, 33]

    def test_whitespace_and_blanks_are_tolerated(self, monkeypatch):
        monkeypatch.setenv("OPTIMIZE_CLOUD_SEEDS", " 5 , ,6 ")
        assert osvc.cloud_seeds() == [5, 6]

    def test_junk_never_breaks_a_contest(self, monkeypatch):
        monkeypatch.setenv("OPTIMIZE_CLOUD_SEEDS", "5,abc,6")
        assert osvc.cloud_seeds() == [5, 6]

    def test_entirely_unparseable_falls_back_to_the_constant(self, monkeypatch):
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7,))
        monkeypatch.setenv("OPTIMIZE_CLOUD_SEEDS", "abc")
        assert osvc.cloud_seeds() == [7]


# --------------------------------------------------------------------------- #
# The API actually passes the knob through
# --------------------------------------------------------------------------- #

class TestApiWiring:
    """A knob nothing reads is worse than no knob — it looks configured and
    does nothing (the exact shape of the balance_operator_load and
    split_parallel findings on this project)."""

    def test_start_optimize_passes_cloud_seeds_into_the_payload(self, monkeypatch):
        import inspect
        pytest.importorskip("fastapi")
        from api import main
        src = inspect.getsource(main._start_optimize)
        assert "optimize_service.cloud_seeds()" in src
        assert "seeds=_seeds" in src

    def test_the_progress_denominator_counts_seed_jobs(self):
        """denom = budget x len(contest_jobs), so it scales with seeds on its own —
        pinned so a future refactor back to sweep_contenders is caught."""
        import inspect
        pytest.importorskip("fastapi")
        from api import main
        src = inspect.getsource(main._start_optimize)
        assert "len(optimize_service.contest_jobs(payload))" in src


# --------------------------------------------------------------------------- #
# Turning seeds ON must not cost more wall-clock
# --------------------------------------------------------------------------- #

class TestWallClockSafety:
    """Seeds are ADDED to the full overlap grid, not traded against it.

    The earlier design halved the grid to hold the job count fixed, on an
    estimate that a contest takes ~25 minutes against OPTIMIZE_CLOUD_TIMEOUT_MIN
    (40). A measured live run does 24 jobs in 391.8 s — ~6.5 min across 20
    parallel shards, i.e. ~196 s per job — so the trade was unnecessary, and it
    cost real coverage: the thinned grid dropped overlap 93, which measured
    objective 2,981 on the live book.

    What must hold instead is that the contest still finishes inside the
    watchdog, with margin, on the slowest shard.
    """

    MEASURED_SECONDS_PER_JOB = 196      # live run: 16,800 plans / 24 jobs / 391.8 s
    SHARDS = 20                         # .github/workflows/optimize.yml matrix
    WATCHDOG_MIN = 40                   # OPTIMIZE_CLOUD_TIMEOUT_MIN default

    def _cfg(self, overlap=82):
        cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                     apply_operator_logic=True, overlap_percent=overlap)
        cfg.validate()
        return cfg

    def _jobs(self, cfg):
        contenders = optimizer.sweep_contenders(
            cfg.overlap_percent, osvc.cloud_candidates(cfg))
        return len(contenders) * 2 * len(osvc.contest_seeds(
            {"seed": 42, "seeds": osvc.cloud_seeds()}))

    def test_the_contest_fits_inside_the_watchdog_with_margin(self):
        jobs = self._jobs(self._cfg())
        per_shard = -(-jobs // self.SHARDS)          # ceil
        minutes = per_shard * self.MEASURED_SECONDS_PER_JOB / 60
        assert minutes < self.WATCHDOG_MIN / 2, (
            f"{jobs} jobs -> {per_shard}/shard -> ~{minutes:.0f} min, "
            f"too close to the {self.WATCHDOG_MIN} min watchdog")

    def test_the_full_overlap_grid_is_searched(self):
        """The thinning is gone: no overlap value may be dropped."""
        assert osvc.cloud_candidates(self._cfg()) == osvc.CLOUD_NEW_OVERLAP_CANDIDATES

    def test_the_top_overlap_is_not_lost(self):
        """93 measured objective 2,981 — better than the incumbent's region.
        The thinned grid dropped it; regression guard."""
        assert 93 in osvc.cloud_candidates(self._cfg())

    def test_seeds_do_not_change_the_overlap_grid(self, monkeypatch):
        cfg = self._cfg()
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", ())
        off = osvc.cloud_candidates(cfg)
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7, 99))
        assert osvc.cloud_candidates(cfg) == off

    def test_the_incumbent_overlap_is_always_searched(self):
        """sweep_contenders injects the current value even when off-grid."""
        for overlap in (82, 86, 77):
            cfg = self._cfg(overlap)
            assert overlap in optimizer.sweep_contenders(
                overlap, osvc.cloud_candidates(cfg))

    def test_classic_mode_is_untouched_by_seeds(self, monkeypatch):
        cfg = Config(scheduler="classic", plan_start_date=date(2025, 3, 3))
        cfg.validate()
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", ())
        before = osvc.cloud_candidates(cfg)
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7, 99))
        assert osvc.cloud_candidates(cfg) == before

    def test_flow_mode_is_untouched_by_seeds(self, monkeypatch):
        cfg = Config(scheduler="flow", plan_start_date=date(2025, 3, 3), flow_chunks=4)
        cfg.validate()
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", ())
        before = osvc.cloud_candidates(cfg)
        monkeypatch.setattr(osvc, "CLOUD_EXTRA_SEEDS", (7, 99))
        assert osvc.cloud_candidates(cfg) == before

    def test_multi_seed_is_OFF(self):
        """Switched off after measurement: a 2-seed contest (16,800 plans) and a
        3-seed contest (50,400) both returned 389 late-days on the live book.
        Variance between individual searches did not survive into the best-of-N."""
        assert osvc.CLOUD_EXTRA_SEEDS == ()
        assert osvc.contest_seeds({"seed": 42, "seeds": osvc.cloud_seeds()}) == [42]
