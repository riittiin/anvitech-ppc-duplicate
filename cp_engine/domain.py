"""The engine's own view of the shop and the work — built from the app's loaded
objects, never from the workbook.

Deliberately a SEPARATE model from engine.models: this engine reasons about
operations and machine options, while engine.models carries display and
persistence concerns. The translation lives in one place (build_shop/build_jobs)
so the rest of the package never touches an app type.

Adapted from the sibling greedy roster-first engine's domain model. One
deliberate difference: ``_candidates`` always returns the Allotted ∪ Suggested
union (spec §3) — the solver picks the machine, so there is no ``flexible``
parameter anywhere in this module and ``build_jobs`` takes two arguments, not
three.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from engine.loaders import normalize_process_name, parse_resource_candidates
from engine.orderbook import is_dispatch

MACHINING = "machining"
MANUAL = "manual"
INSPECTION = "inspection"
OUTSOURCED = "outsourced"
DISPATCH = "dispatch"

_OS = "OS"
_INSPECTION_PREFIXES = ("MI", "CMM", "DTC")


@dataclass(frozen=True)
class Op:
    seq: int
    name: str
    kind: str
    cycle_min: float
    machine_options: tuple


@dataclass(frozen=True)
class Job:
    key: str
    item_code: str
    qty: int
    due: date | None
    so_refs: tuple
    ops: tuple
    remaining: dict | None          # op_seq -> pieces still to make (re-plan)

    def qty_for(self, op_seq: int) -> int:
        """How many pieces this step still owes. Derived at BATCH level — a per-SO-line
        remainder is an INPUT to this, never the answer (2026-08-11: a frozen op ran
        one clubbed line's 88 pieces and left the other line's 281 in no plan at all)."""
        if self.remaining is None:
            return int(self.qty)
        return int(self.remaining.get(op_seq, self.qty))


@dataclass(frozen=True)
class Shop:
    machines: dict
    operators: tuple
    calendar: object
    machining_ids: frozenset
    absent: dict                    # operator name -> list[(start, end)] busy blocks


def is_machining_machine(machine) -> bool:
    """CNC/VMC by id or by Machine-master type — the same rule the 90-minute setup
    uses (RULES.md Rule 4, ``engine.rules.rule6_allocate._is_setup_machine``).
    Duplicated here rather than imported from the classic rules so this engine
    stands alone; a test pins the two in agreement.

    Mirrors ``_is_setup_machine`` exactly, including its SUBSTRING match on the
    machine type (not an exact-string match against a fixed tuple) — a type like
    "CNC lathe (old)" must classify the same way in both places, or the two would
    silently drift on any machine outside the pinned test's fixture set."""
    mid = (getattr(machine, "machine_no", "") or "").upper()
    if mid.startswith("CNC") or mid.startswith("VMC"):
        return True
    mtype = (getattr(machine, "machine_type", "") or "").upper()
    return "CNC" in mtype or "VMC" in mtype or "VERTICAL MACHINING" in mtype


def _kind_for_machine_id(mid: str, masters) -> str:
    """Classify a resolved machine id into an Op kind.

    Looks the id up in the Machine master when possible; falls back to
    pattern-matching the id itself when the machine is not (yet) registered there
    — routings may reference a PROVISIONAL machine (RULES.md: "machines not yet in
    the master... register as provisional... never drop the row"), and this engine
    must classify those the same way it would once the master catches up."""
    machine = masters.machines.get(mid)
    if machine is not None:
        if is_machining_machine(machine):
            return MACHINING
        mtype = (machine.machine_type or "").lower()
        if "insp" in mtype or mid.upper().startswith(_INSPECTION_PREFIXES):
            return INSPECTION
        return MANUAL
    up = mid.upper()
    if up.startswith("CNC") or up.startswith("VMC"):
        return MACHINING
    if up.startswith(_INSPECTION_PREFIXES):
        return INSPECTION
    return MANUAL


def _candidates(proc, masters) -> tuple:
    """Machine ids this step may run on: the Allotted ∪ Suggested union,
    Allotted first, deduped, dropping anything absent from the Machine master.

    Deliberately DIFFERENT from the roster-first engine's version, which returns
    Suggested only when Allotted is blank. That fallback-only reading is one of
    the restrictions this engine lifts (spec §3): the solver picks the machine,
    so it is handed every machine the routing actually lists.
    """
    ids: list[str] = []
    for raw in (proc.allotted_machine, proc.suggested_machine):
        for mid in parse_resource_candidates(raw or ""):
            if mid in masters.machines and mid not in ids:
                ids.append(mid)
    return tuple(ids)


def _is_os(proc) -> bool:
    for raw in (proc.allotted_machine, proc.suggested_machine):
        if (raw or "").strip().upper() == _OS:
            return True
    named_os = _OS in (proc.name or "").upper().split()
    return named_os and not (proc.allotted_machine or proc.suggested_machine)


def _ops_from_processes(processes, masters) -> tuple:
    out = []
    for proc in sorted(processes, key=lambda p: p.seq):
        cycle = float(proc.cycle_time or 0.0)
        if is_dispatch(proc.name or ""):
            kind, options = DISPATCH, ()
        elif _is_os(proc):
            kind, options = OUTSOURCED, ()
        else:
            options = _candidates(proc, masters)
            if not options:
                # No machine and no cycle time -> a visible zero-duration milestone
                # on the Off-machine lane, never silently dropped.
                kind = DISPATCH if cycle <= 0 else OUTSOURCED
            else:
                kind = _kind_for_machine_id(options[0], masters)
        out.append(Op(int(proc.seq), proc.name or "", kind, cycle, options))
    return tuple(out)


def build_shop(masters, absent_by_operator=None) -> Shop:
    machining = frozenset(
        mid for mid, m in masters.machines.items() if is_machining_machine(m))
    return Shop(machines=dict(masters.machines),
                operators=tuple(masters.operators),
                calendar=masters.calendar,
                machining_ids=machining,
                absent=dict(absent_by_operator or {}))


def _remaining_by_seq(batch, routing) -> dict | None:
    """Translate a real ``engine.models.Batch``'s ``process_qty`` (keyed by
    NORMALIZED PROCESS NAME, per RULES.md's per-process remaining) into
    ``Job.remaining`` (keyed by routing op_seq, what ``Job.qty_for`` reads).

    Mirrors ``engine.new_engine._orders_from_batches`` exactly — that is the
    reference implementation for this translation, right down to the
    normalizer (``engine.loaders.normalize_process_name``, the SAME one the
    order book used to key ``process_qty`` in the first place; any other rule
    — e.g. stripping spaces instead of collapsing them — silently drops every
    multi-word step like "CNC FIRST SIDE").

    Real ``process_qty`` wins when present. Falls back to a ``process_remaining``
    attribute (already seq-keyed) for lightweight test doubles that skip the
    name-keyed dict entirely."""
    process_qty = getattr(batch, "process_qty", None)
    if process_qty:
        return {
            proc.seq: int(round(process_qty[normalize_process_name(proc.name)]))
            for proc in routing.processes
            if normalize_process_name(proc.name) in process_qty
        }
    return getattr(batch, "process_remaining", None)


def build_jobs(batches, masters):
    """Batches (Rule 1's output, already clubbed) -> jobs. Never re-consolidates.

    Returns (jobs, batch_by_key, skipped_item_codes). An item with no routing is
    SKIPPED and reported, never raised — RULES.md's fail-localized rule.

    Reads ``so_delivery_date``/``source_so_refs`` (the real ``engine.models.Batch``
    field names) with a fallback to ``delivery_date``/``so_refs`` for any lighter
    test double that names them differently — both resolve the same due date /
    SO list either way.
    """
    jobs, by_key, skipped = [], {}, []
    for batch in batches:
        routing = masters.routings.get(batch.item_code)
        if routing is None:
            if batch.item_code not in skipped:
                skipped.append(batch.item_code)
            continue
        ops = _ops_from_processes(routing.processes, masters)
        if not ops:
            if batch.item_code not in skipped:
                skipped.append(batch.item_code)
            continue
        due = getattr(batch, "so_delivery_date", None)
        if due is None:
            due = getattr(batch, "delivery_date", None)
        so_refs = getattr(batch, "source_so_refs", None)
        if so_refs is None:
            so_refs = getattr(batch, "so_refs", None)
        jobs.append(Job(
            key=str(batch.batch_id),
            item_code=batch.item_code,
            qty=int(batch.qty),
            due=due,
            so_refs=tuple(so_refs or ()),
            ops=ops,
            remaining=_remaining_by_seq(batch, routing)))
        by_key[str(batch.batch_id)] = batch
    return jobs, by_key, skipped
