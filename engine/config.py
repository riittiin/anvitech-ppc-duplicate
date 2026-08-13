"""Tunable parameters for the PPC engine.

Every configurable knob from RULES.md lives here, with validation. The pipeline
validates the config once at run start (Design spec §7). Rules receive this
object and never read global state.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional


# Overlap modes for Rule 5.
OVERLAP_SEQUENTIAL = "sequential"   # next process starts only after previous fully done
OVERLAP_PERCENT = "overlap"         # next process starts after overlap_percent of previous

# Rule 3 priority metrics (operations-management dispatching rules).
PRIORITY_SLACK = "slack"            # least slack first (time-to-due minus work-needed)
PRIORITY_CRITICAL_RATIO = "critical_ratio"  # lowest critical ratio first (time / work)
PRIORITY_PROCESS_TIME = "process_time"      # legacy: more process time first (exact-date ties)
PRIORITY_METRICS = (PRIORITY_SLACK, PRIORITY_CRITICAL_RATIO, PRIORITY_PROCESS_TIME)


@dataclass
class Config:
    """All tunable params. Defaults match RULES.md "Configurable parameters"."""

    # Rule 1 — consolidation window (days). SO lines for the same item whose
    # delivery dates fall within this many days are merged into one batch.
    consolidation_window_days: int = 10

    # Rule 4 — setup time added to every process's machine occupancy (minutes).
    setup_time_min: int = 90

    # Rule 5 — operation overlap.
    overlap_mode: str = OVERLAP_SEQUENTIAL
    overlap_percent: int = 50  # only used when overlap_mode == OVERLAP_PERCENT

    # NOTE: recorded times (downtime categories, actual setup) are captured for the
    # record ONLY and never affect the schedule — the feedback loop is driven purely
    # by quantity produced/rejected per process (per the director's spec).

    # Rule 3 — smart priority. The metric folds due date + workload into urgency.
    #   slack          = (working time until SO delivery date) − (work needed)
    #   critical_ratio = (time until due) / (work needed)
    #   process_time   = legacy: more process time first
    # priority_window_days bounds how far apart two SO delivery dates may be for
    # the metric to reorder them; None = no limit (pure metric sort), 0 = only
    # exact-same-date ties (legacy behaviour).
    priority_metric: str = PRIORITY_SLACK
    priority_window_days: Optional[int] = None

    # Rule 6 — where the schedule clock starts. The first process of the
    # highest-priority batch can begin no earlier than this date (08:00, first
    # shift). None = "auto: start from today (IST)" — the LIVE default. A fixed
    # date is a testing/reproducibility override. The ENGINE must never see None:
    # the API boundary resolves None -> today (api.main._resolve_config) at every
    # planning entry before any rule reads this field.
    plan_start_date: Optional[date] = None

    # Auto-mode plan-start FLOOR (transient, per-run, never persisted): an ISO datetime
    # string. When plan_start_date is auto (None -> today), the API sets this to the NEXT
    # full hour after 'now' (IST) so the plan never starts in the past (a late run rolls to
    # the next hour instead of 08:00 that morning). The engine starts at
    # max(08:00-of-date, this floor). None = no floor (08:00 of the date — the fixed-date /
    # testing behaviour, e.g. the golden trace). Excluded from _inputs_signature.
    plan_start_floor: Optional[str] = None

    # Which allocation engine plans the book (2026-07-19 flow-scheduler spec):
    #   "classic" = the original Rule 6 non-delay scheduler (engine default —
    #               keeps the golden trace and every historical plan identical)
    #   "flow"    = the piece-flow scheduler (engine/flow_scheduler.py): chunked
    #               WIP flow, no resource-holding, setup per re-engagement.
    # Not a Settings knob (the owner's no-knobs rule) — the LIVE saved config
    # carries "flow" after cutover; flow_chunks is tuned by the Optimize contest.
    scheduler: str = "classic"
    flow_chunks: int = 4
    # Flow scheduler only (2026-07-19): rank a candidate that needs a fresh 90-min
    # CNC/VMC setup as if it started 90 min later, so a machine keeps running the
    # SAME job's consecutive chunks (one setup) instead of ping-ponging between
    # jobs (a setup each time). Attacks the ~9% of machine time lost to re-setups
    # that chunking inflates. Off = the setup-blind earliest-start rule.
    flow_setup_aware: bool = True

    # Shift windows (24h clock). 1st shift 08:00-19:00, 2nd 19:00-05:00 (next day).
    first_shift_start_hour: int = 8
    second_shift_end_hour: int = 5  # on the following calendar day
    first_shift_end_hour: int = 19  # 1st/2nd boundary

    # Operator logic. A machine runs both shifts when its Available Hrs/Day is at
    # least this threshold (CNC/VMC ≈19.5); otherwise it is a single-shift resource
    # working only the manual window below (the shop's "treat 9.5 as 9am-6pm").
    two_shift_threshold_hours: float = 12.0
    manual_start_hour: int = 9      # single-shift / manual window start (09:00)
    manual_end_hour: int = 18       # single-shift / manual window end (18:00)

    # Parallel split — when a process lists alternative machines ("CNC3/CNC7") and
    # 2+ of them are free when the op can start, split the quantity evenly across
    # the free ones to run in parallel and finish faster (each pays its own setup;
    # the next process waits for the slowest half — split-then-recombine).
    # Split is applied ONLY to LARGE batches — quantities >= split_min_qty (401, i.e.
    # more than 400). A batch of 400 or fewer is NOT split; it runs entirely on the
    # single least-queued (earliest-free) alternative, so small jobs load-balance
    # without paying an extra setup on a second machine. Engine default OFF
    # (golden/tests stay stable); the web UI defaults it ON.
    split_parallel: bool = False
    split_min_qty: int = 401

    # Machine set the OPTIMIZER may use for each in-house machining/manual/inspection
    # step (2026-07-29). False = the Allotted machine only (Suggested used only as a
    # blank-Allotted fallback) — today's behaviour, byte-identical. True = the deduped
    # union of Allotted + Suggested, letting the scheduler load-balance onto Suggested
    # machines. Owned by the Optimize contest (swept, applied, persisted) exactly like
    # overlap_percent — NOT a user knob. New engine only; classic/flow ignore it.
    flexible_machines: bool = False

    # Rule 6 — balance operator workload (default off). A schedule-neutral POST-PROCESS:
    # after the plan is built, each already-scheduled operation is reassigned to the
    # qualified, same-shift operator who is free at that moment and has the least work
    # so far — spreading load evenly across interchangeable people. It never changes any
    # start/end time, so makespan and lateness are unaffected; only *who* runs each job
    # changes. The web UI can enable it as a fairness toggle.
    balance_operator_load: bool = False

    # Rule 6 — expedite window (minutes). Pure non-delay scheduling only breaks a tie
    # when two ops can start at the exact same instant; a more-urgent op that is
    # feasible a few minutes later keeps losing every near-race for a shared
    # machine/operator. When > 0, among the ops that could start within this many
    # minutes of the EARLIEST startable one, the scheduler picks the least-slack
    # (most at-risk of its SO delivery date) instead of the merely-earliest. 0 = off
    # → byte-identical to the legacy non-delay tie-break (golden/tests stay stable);
    # the web UI can enable a small window (≈45).
    expedite_window_min: int = 0

    # Worst-order ceiling (2026-07-24 amendment) — TRANSIENT, per optimize run, never
    # saved. Set by api._start_optimize to the currently-applied plan's max lateness
    # (days) so the search/apply refuses to push any order past the current worst-case.
    # None = no ceiling (byte-identical). Excluded from _inputs_signature.
    worst_ceiling_days: float | None = None

    # Committed promise slack (default 3 days). When scoring committed orders during
    # the optimizer's search, add this many days to the promised delivery date as a
    # buffer so the plan protects commitments with headroom, not just at the edge.
    # Included in _inputs_signature (a plan-shaping knob).
    committed_promise_slack_days: int = 3

    # When True, the full operator/shift model applies in Rule 6:
    #   * each machine uses a per-availability working window (≥threshold → two
    #     shifts 08:00-05:00; else single-shift manual 09:00-18:00), AND
    #   * an op may only run during a shift that has a qualified operator
    #     (specialty + that shift); uncovered machines are reported, not scheduled.
    # Engine default OFF (legacy single window, no coverage → keeps the golden trace
    # + existing rule tests stable); the web UI defaults it ON.
    apply_operator_logic: bool = False

    def validate(self) -> None:
        """Fail loud on a bad config (Design spec §8 — config validation)."""
        errs = []
        if self.consolidation_window_days < 0:
            errs.append("consolidation_window_days must be >= 0")
        if self.setup_time_min < 0:
            errs.append("setup_time_min must be >= 0")
        if self.overlap_mode not in (OVERLAP_SEQUENTIAL, OVERLAP_PERCENT):
            errs.append(
                f"overlap_mode must be '{OVERLAP_SEQUENTIAL}' or '{OVERLAP_PERCENT}'"
            )
        if not (0 <= self.overlap_percent <= 100):
            errs.append("overlap_percent must be within 0..100")
        if self.priority_metric not in PRIORITY_METRICS:
            errs.append(f"priority_metric must be one of {PRIORITY_METRICS}")
        if self.priority_window_days is not None and self.priority_window_days < 0:
            errs.append("priority_window_days must be >= 0 or null (no limit)")
        if not (0 <= self.first_shift_start_hour <= 23):
            errs.append("first_shift_start_hour must be within 0..23")
        if not (0 <= self.second_shift_end_hour <= 23):
            errs.append("second_shift_end_hour must be within 0..23")
        if not (0 <= self.manual_start_hour <= 23):
            errs.append("manual_start_hour must be within 0..23")
        if not (0 < self.manual_end_hour <= 24):
            errs.append("manual_end_hour must be within 1..24")
        if self.manual_end_hour <= self.manual_start_hour:
            errs.append("manual_end_hour must be after manual_start_hour")
        if self.two_shift_threshold_hours < 0:
            errs.append("two_shift_threshold_hours must be >= 0")
        if not isinstance(self.apply_operator_logic, bool):
            errs.append("apply_operator_logic must be true or false")
        # "roster" = the roster-first engine (roster_engine/, 2026-08-12 spec),
        # dispatched by pipeline.scheduler_for -> engine/roster_adapter.py. It must
        # be listed here or NOTHING can plan on it: run_forward, optimizer.optimize
        # and optimize_service all call validate(), and api._load_plan_config drops
        # a config that fails validation back to a bare Config() — i.e. an admin's
        # saved choice would be silently downgraded to classic.
        if self.scheduler not in ("classic", "flow", "new", "roster"):
            errs.append("scheduler must be 'classic', 'flow', 'new', or 'roster'")
        if not (1 <= int(self.flow_chunks) <= 50):
            errs.append("flow_chunks must be within 1..50")
        if not isinstance(self.split_parallel, bool):
            errs.append("split_parallel must be true or false")
        if not isinstance(self.balance_operator_load, bool):
            errs.append("balance_operator_load must be true or false")
        if not isinstance(self.flexible_machines, bool):
            errs.append("flexible_machines must be true or false")
        if self.split_min_qty < 1:
            errs.append("split_min_qty must be >= 1")
        if self.expedite_window_min < 0:
            errs.append("expedite_window_min must be >= 0")
        if self.worst_ceiling_days is not None and self.worst_ceiling_days < 0:
            errs.append("worst_ceiling_days must be >= 0 or None")
        if self.committed_promise_slack_days < 0:
            errs.append("committed_promise_slack_days must be >= 0")
        if errs:
            raise ValueError("Invalid config: " + "; ".join(errs))

    def to_dict(self) -> dict:
        d = asdict(self)
        # None (auto: start from today) stays None -> JSON null. The saved config
        # keeps None so a moving 'today' is never mistaken for a settings change.
        d["plan_start_date"] = (self.plan_start_date.isoformat()
                                if self.plan_start_date else None)
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Config":
        """Build a Config from a (possibly partial) dict, e.g. API query params."""
        cfg = cls()
        if not data:
            return cfg
        for key, value in data.items():
            if not hasattr(cfg, key):
                continue
            if key == "plan_start_date":
                # None / "" / missing -> None (auto: start from today, resolved at
                # the API boundary). A non-empty ISO string -> a fixed date.
                if value in (None, "", "none", "null"):
                    value = None
                elif isinstance(value, str):
                    value = date.fromisoformat(value)
            if key == "priority_window_days":
                # "" / "none" / null from the UI means "no limit".
                if value in (None, "", "none", "null"):
                    value = None
                else:
                    value = int(value)
            setattr(cfg, key, value)
        return cfg
