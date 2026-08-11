"""Pure freeze logic (reporting/derivation only — never mutates a plan).

Two pure functions:
  - ``schedule_projection(schedule)`` — the applied plan's per-op assignment, the durable
    record of "the plan the floor is following" (machine + operator + time per op).
  - ``compute_frozen_set(applied_rows, so_lines, good_by_step, masters)`` — from that
    record + the punches, the in-progress ops to FREEZE (machine/operator from the plan,
    remaining qty from the punches). See the 2026-07-29 spec.
"""
from __future__ import annotations
from engine.loaders import normalize_process_name as _norm

_OS_LANES = {"OS / Outsourced", "Off-machine"}


def schedule_projection(schedule) -> list[dict]:
    """One row per real (machine) operation in the applied plan. OS/off-lane entries are
    skipped (no in-house machine to pin)."""
    rows = []
    for e in schedule:
        if e.machine in _OS_LANES:
            continue
        rows.append({
            "batch_id": e.batch_id,
            "item_code": e.item_code,
            "process_seq": e.process_seq,
            "process_name": e.process_name,
            "machine": e.machine,
            "operator": e.operator or "",
            "start": e.start.isoformat(timespec="seconds"),
            "end": e.end.isoformat(timespec="seconds"),
            "so_refs": list(e.so_refs or []),
        })
    return rows


def compute_frozen_set(applied_rows, so_lines, good_by_step, masters) -> list[dict]:
    """Frozen (in-progress) ops: partially-punched steps (good>0 and remaining>0),
    with machine + operator looked up from the applied plan. Steps not present in the
    applied plan, or whose applied machine is OS/off-lane, are not frozen."""
    # Index applied rows: (item_code, process_seq) -> list of rows (with so_refs).
    by_item_seq: dict[tuple[str, int], list[dict]] = {}
    for r in applied_rows or []:
        by_item_seq.setdefault((r["item_code"], r["process_seq"]), []).append(r)

    out = []
    for line in so_lines:
        routing = masters.routings.get(line.item_code)
        if routing is None:
            continue
        pq = line.process_qty or {}
        for op in routing.processes:
            nkey = _norm(op.name)
            remaining = int(round(float(pq.get(nkey, 0))))
            good = int(round(float(good_by_step.get((line.so_no, line.item_code, nkey), 0))))
            if good <= 0 or remaining <= 0:
                continue  # not started, or fully done → not frozen
            # Machine/operator from the applied plan row covering this SO for this op.
            cand = by_item_seq.get((line.item_code, op.seq), [])
            row = next((r for r in cand if line.so_no in (r.get("so_refs") or [])), None)
            if row is None or row["machine"] in _OS_LANES:
                continue  # not in last plan / outsourced → not frozen
            out.append({
                "so_no": line.so_no, "item_code": line.item_code,
                "process": op.name, "op_seq": op.seq,
                "machine": row["machine"], "operator": row.get("operator", "") or "",
                "remaining_qty": remaining, "prev_start": row["start"],
            })
    return out
