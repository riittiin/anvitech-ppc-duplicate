"""The pipeline + trace backbone — the engine's debug spine (Design spec §5).

``run_rule`` wraps every rule call and snapshots its input table, output table,
config, and decision notes into a trace dict. The per-rule frontend tabs render
that trace, so rules themselves need no UI code.

A rule that detects a contract violation raises ``RuleError``; the pipeline
records it in that rule's trace entry and STOPS the chain (fail loud, fail
localized). Downstream rules are marked "not reached".
"""
from __future__ import annotations

from datetime import date, datetime

from .config import Config
from .models import Masters, PlanRun
from .rules import (
    rule1_consolidate,
    rule2_sort_by_date,
    rule3_tiebreak_process_time,
    rule6_allocate,
)


class RuleError(Exception):
    """A typed, localized rule-contract violation (Design spec §8)."""

    def __init__(self, rule: str, record_id: str, message: str):
        self.rule = rule
        self.record_id = record_id
        self.message = message
        super().__init__(f"[{rule}] record {record_id}: {message}")


# --------------------------------------------------------------------------- #
# Trace serialization
# --------------------------------------------------------------------------- #
def _cell(value):
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")     # DD-MM-YYYY HH:MM (app-wide)
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")           # DD-MM-YYYY
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return value


def to_table(data):
    """Turn a model / list of models / list of dicts into ``{columns, rows}``.

    Every model exposes ``as_row()``; we union the keys across rows to form the
    column list so sparse rows still line up.
    """
    if data is None:
        return {"columns": [], "rows": []}

    items = data if isinstance(data, (list, tuple)) else [data]

    rows = []
    columns: list[str] = []
    seen = set()
    for item in items:
        if hasattr(item, "as_row"):
            row = item.as_row()
        elif isinstance(item, dict):
            row = item
        else:
            row = {"value": item}
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
        rows.append(row)

    norm_rows = [[_cell(row.get(c)) for c in columns] for row in rows]
    return {"columns": columns, "rows": norm_rows}


# --------------------------------------------------------------------------- #
# Rule wrapper
# --------------------------------------------------------------------------- #
def run_rule(trace: dict, name: str, fn, input_data, *, config=None, **kw):
    """Run one rule, snapshotting in/out/config/notes into ``trace[name]``.

    The rule may append human-readable strings to a ``notes`` list it receives
    via kw. On RuleError the entry records the error and the exception is
    re-raised so the pipeline can stop the chain.
    """
    notes: list[str] = []
    entry = {
        "input": to_table(input_data),
        "output": {"columns": [], "rows": []},
        "config": config.to_dict() if isinstance(config, Config) else config,
        "notes": notes,
        "error": None,
        "reached": True,
    }
    trace[name] = entry
    try:
        output = fn(input_data, config=config, notes=notes, **kw)
    except RuleError as err:
        entry["error"] = {
            "rule": err.rule,
            "record_id": err.record_id,
            "message": err.message,
        }
        raise
    entry["output"] = to_table(output)
    return output


# --------------------------------------------------------------------------- #
# Forward chain
# --------------------------------------------------------------------------- #
RULE_NAMES = [
    "rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "rule7", "rule8",
]

# Separator of the composite order key "<SO No>\x1f<Item Code>" — the same field
# separator book_store uses, so a persisted rank map round-trips unchanged.
KEY_SEP = "\x1f"


def batch_rank(batch, priority_rank: dict):
    """A batch's rank under a saved optimization = the best (minimum) rank among
    its member orders (each keyed "<so>\x1f<item>"). None when no member is ranked.
    Consolidation-safe: batches merge/shrink as orders arrive/complete, and the
    rank survives through any member still present."""
    ranks = [priority_rank[k] for k in
             (f"{so}{KEY_SEP}{batch.item_code}" for so in (batch.source_so_refs or []))
             if k in priority_rank]
    return min(ranks) if ranks else None


def scheduler_for(config):
    """The allocation engine for this plan: ``config.scheduler`` selects the
    classic Rule 6 non-delay scheduler (default — historical plans and the
    golden trace byte-identical) or the piece-flow scheduler
    (``engine/flow_scheduler.py``, 2026-07-19 spec). ONE dispatch point shared
    by run_forward and the optimizer, so search and replay always use the same
    engine as the plan."""
    sched = getattr(config, "scheduler", "classic")
    if sched == "new":
        # The new operator-stable engine (engine/new_engine.py), run behind this seam so
        # the whole UI keeps consuming the same ScheduleEntry list.
        from . import new_engine
        return new_engine.run
    if sched == "roster":
        # The roster-first engine (roster_engine/, 2026-08-12 spec): the crew is
        # rostered at the shift boundary and an operation runs to completion, so
        # "one operator per machine per shift" is structural rather than checked.
        # engine/roster_adapter.py is the only file that knows both worlds.
        from . import roster_adapter
        return roster_adapter.run
    if sched == "cp":
        # The CP-SAT engine (cp_engine/, 2026-08-14 spec). THE REPLAY, never a
        # solve: cp_adapter.run decodes the stored genome in milliseconds, which
        # is what /run needs on every page load. Wiring this to cp_adapter.solve
        # would start a CP solve on Render's 0.5-CPU instance on every refresh —
        # and pyjobshop is not installed there, so it would 500 rather than merely
        # be slow.
        from . import cp_adapter
        return cp_adapter.run
    if sched == "flow":
        from . import flow_scheduler
        return flow_scheduler.run
    return rule6_allocate.run


def apply_priority_rank(batches: list, priority_rank: dict | None) -> tuple[list, int]:
    """Replay a saved optimized sequence over Rule 3's output.

    Ranked batches are reordered (by rank, stable on their Rule-3 position) among
    the SLOTS they already occupy; unranked batches keep their own Rule-3 slots,
    so a new order uploaded after the optimization keeps its natural priority.
    Returns (reordered list, number of ranked batches). Empty/None rank map or
    nothing ranked -> the input list unchanged (byte-identical)."""
    if not priority_rank:
        return batches, 0
    ranked_slots = [i for i, b in enumerate(batches)
                    if batch_rank(b, priority_rank) is not None]
    if not ranked_slots:
        return batches, 0
    order = sorted(ranked_slots, key=lambda i: (batch_rank(batches[i], priority_rank), i))
    out = list(batches)
    for slot, src in zip(ranked_slots, order):
        out[slot] = batches[src]
    return out, len(ranked_slots)


def _mark_not_reached(trace: dict, from_index: int):
    for nm in RULE_NAMES[from_index:]:
        if nm not in trace:
            trace[nm] = {
                "input": {"columns": [], "rows": []},
                "output": {"columns": [], "rows": []},
                "config": None,
                "notes": [],
                "error": None,
                "reached": False,
            }


def run_forward(plan_run: PlanRun, config: Config, masters: Masters,
                machine_lost_min: dict | None = None,
                reserved: dict | None = None,
                priority_rank: dict | None = None,
                frozen: dict | None = None) -> dict:
    """Run the forward planning chain 1 → 2 → 3 → 6, returning the trace.

    Rules 4/5 are consumed inside Rule 6 (their effect is logged in rule6's
    notes); they are surfaced as helper trace entries by the API layer if needed.

    ``machine_lost_min`` (optional) maps machine id → working minutes lost to
    recorded downtime/setup overrun; Rule 6 seeds it into machine availability so
    lost time loops back into the plan. ``None`` → no effect (default).

    ``reserved`` (optional) maps machine id / operator name → busy intervals
    that Rule 6 must treat as already occupied — blocked machine/operator
    intervals (today: operator absences). ``None`` → no effect (default).

    ``priority_rank`` (optional) maps composite order keys "<so>\\x1f<item>" → rank
    from a saved Optimize run; ranked batches replay in that order among the slots
    they occupy after Rule 3 (see ``apply_priority_rank``). ``None`` → no effect.

    ``frozen`` (optional) maps machine id → list of FrozenOp specs; passed to the
    scheduler to pre-place locked operations. ``None`` → no effect (new-engine only).
    """
    config.validate()
    trace: dict = {}

    try:
        plan_run.batches = run_rule(
            trace, "rule1", rule1_consolidate.run, plan_run.so_lines,
            config=config, masters=masters,
        )
        plan_run.batches_sorted = run_rule(
            trace, "rule2", rule2_sort_by_date.run, plan_run.batches,
            config=config, masters=masters,
        )
        plan_run.batches_prioritized = run_rule(
            trace, "rule3", rule3_tiebreak_process_time.run, plan_run.batches_sorted,
            config=config, masters=masters,
        )
        replayed, n_ranked = apply_priority_rank(plan_run.batches_prioritized, priority_rank)
        if n_ranked:
            plan_run.batches_prioritized = replayed
            # Keep the Rule-3 tab honest: show the order Rule 6 will actually consume.
            trace["rule3"]["output"] = to_table(replayed)
            trace["rule3"]["notes"].append(
                f"Optimized sequence applied (saved by the Optimize feature): "
                f"{n_ranked} of {len(replayed)} batches follow the optimized order; "
                f"unranked (new) batches keep their Rule-3 position."
            )
        plan_run.schedule = run_rule(
            trace, "rule6", scheduler_for(config), plan_run.batches_prioritized,
            config=config, masters=masters, machine_lost_min=machine_lost_min,
            reserved=reserved, frozen=frozen,
        )
    except RuleError:
        # The failing rule's entry already holds the error; mark the rest unreached.
        failed_at = max(
            (i for i, nm in enumerate(RULE_NAMES) if nm in trace and trace[nm]["error"]),
            default=0,
        )
        _mark_not_reached(trace, failed_at + 1)
        return trace

    _mark_not_reached(trace, len(RULE_NAMES))  # fill rule4/5/7/8 placeholders
    return trace
