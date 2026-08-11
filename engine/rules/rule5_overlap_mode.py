"""Rule 5 — Operation overlap mode (calc helper, consumed inside Rule 6).

Global per-run toggle deciding when the NEXT process may start relative to the
previous one:
  * sequential  — next starts only after the previous is fully complete (100%).
  * overlap     — next starts after overlap_percent (default 50%) of the
                  previous process's CUTTING time has elapsed.

The percentage applies to the **cutting time only** (cycle x qty), NOT the
90-min setup: while the previous operation cuts, the next machine can be set up
in parallel, so the previous setup is not counted. A step with **no cutting
time** (a finishing / inspection step) cannot meaningfully overlap — its
successor waits for it to fully complete.

``elapsed_before_next(prev_occupancy_min, prev_run_min, config)`` returns how
many minutes after the previous process STARTS the next one may begin. Rule 6
then walks that many *working* minutes forward on the working calendar.
"""
from __future__ import annotations

from ..config import OVERLAP_SEQUENTIAL


def elapsed_before_next(prev_occupancy_min: float, prev_run_min: float = None, config=None) -> float:
    # Sequential mode: wait for the previous operation to fully complete.
    if config is None or config.overlap_mode == OVERLAP_SEQUENTIAL:
        return prev_occupancy_min
    # No-cutting step (e.g. deburring / inspection): nothing to overlap — the
    # successor waits for full completion.
    if not prev_run_min or prev_run_min <= 0:
        return prev_occupancy_min
    # Overlap: the percentage applies to the cutting time only (setup excluded).
    return prev_run_min * (config.overlap_percent / 100.0)


def build_overlap_view(schedule, config):
    """Per-handoff view of Rule 5 *as applied to the real plan* — for each pair of
    consecutive operations within a batch, what the next op had to wait (full
    occupancy under sequential) vs what it actually waited this run, and how much
    that pulled the next op earlier. Drives the Rule 5 tab so the rule is visibly
    applied, not a static illustration.
    """
    setup = config.setup_time_min if config else 90
    pct = config.overlap_percent if config else 50
    overlap_on = bool(config) and config.overlap_mode != OVERLAP_SEQUENTIAL

    # Group the schedule into per-batch ordered operations.
    by_batch = {}
    for e in schedule:
        by_batch.setdefault(e.batch_id, []).append(e)

    rows = []
    for bid, ops in by_batch.items():
        # Collapse a parallel-split step (two entries, same seq) to its slowest half
        # so the handoff view pairs distinct process steps, not the two split halves.
        by_seq = {}
        for e in ops:
            cur = by_seq.get(e.process_seq)
            if cur is None or e.end > cur.end:
                by_seq[e.process_seq] = e
        ops = sorted(by_seq.values(), key=lambda e: e.process_seq)
        for prev, nxt in zip(ops, ops[1:]):
            cutting = max(prev.occupancy_min - setup, 0.0)
            seq_gap = prev.occupancy_min                       # full wait (sequential)
            applied = elapsed_before_next(prev.occupancy_min, cutting, config)
            pulled = seq_gap - applied
            if not overlap_on:
                label = "no, sequential mode"
            elif cutting > 0:
                label = f"yes, {pct}% of cutting"
            else:
                label = "no, finishing step (no cutting)"
            rows.append({
                "Batch": prev.batch_id,
                "Item Code": prev.item_code,
                "From process": f"{prev.process_seq}. {prev.process_name}",
                "To process": f"{nxt.process_seq}. {nxt.process_name}",
                "Cutting (min)": round(cutting, 1),
                "Setup (min)": setup,
                "Next allowed after: sequential (min)": round(seq_gap, 1),
                "Next allowed after: this run (min)": round(applied, 1),
                "Pulled earlier (min)": round(pulled, 1),
                "Overlap applied": label,
            })
    return rows


def run(item, config=None, notes=None, masters=None, **kw):
    """Test/trace shim: ``item`` is a dict with prev_occupancy_min and (optional)
    prev_run_min (the cutting-only minutes)."""
    notes = notes if notes is not None else []
    prev = item.get("prev_occupancy_min", 0.0)
    run_min = item.get("prev_run_min", prev)
    offset = elapsed_before_next(prev, run_min, config)
    mode = config.overlap_mode if config else "sequential"
    notes.append(
        f"overlap mode='{mode}': next process may start {offset} min after the "
        f"previous one starts (cutting {run_min} min of {prev} min occupancy)"
    )
    return {"elapsed_before_next_min": offset}
