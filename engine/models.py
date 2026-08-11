"""Typed data models for the PPC engine.

Every model exposes ``as_row()`` returning an ordered dict of plain,
JSON-serializable values. The pipeline's ``to_table`` helper uses that to turn
any model (or list of models) into a ``{columns, rows}`` table for the trace —
so the per-rule frontend tabs need no per-model code (Design spec §5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


def _fmt(value):
    """Render a value as a plain, JSON-friendly scalar for a trace table cell.

    Dates display as DD-MM-YYYY (datetimes as DD-MM-YYYY HH:MM) everywhere in the
    app — see ``fmt_date`` / ``fmt_datetime``."""
    if isinstance(value, datetime):
        return fmt_datetime(value)
    if isinstance(value, date):
        return fmt_date(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value


# Display format for every date / datetime shown to the user (DD-MM-YYYY).
def fmt_date(d) -> str:
    return d.strftime("%d-%m-%Y")


def fmt_datetime(dt) -> str:
    return dt.strftime("%d-%m-%Y %H:%M")


# --------------------------------------------------------------------------- #
# Demand
# --------------------------------------------------------------------------- #
@dataclass
class SOLine:
    """One raw sales-order line from the SO list sheet."""

    so_no: str
    item_code: str
    item_name: str
    qty: float
    delivery_date: date
    pending_qty: Optional[float] = None
    customer: str = ""
    remarks: str = ""
    # Per-process remaining {normalized process name -> qty still to run there} =
    # ordered − already done at that step. None = no progress (run the full qty at
    # every step, today's behaviour). Internal scheduling input — not serialized.
    process_qty: Optional[dict] = None
    # Commitment lane carried into planning: "open" (default) | "committed".
    commitment: str = "open"
    promised_date: Optional[date] = None

    @property
    def key(self):
        """The unique identity of this line: (SO number, item code)."""
        return (self.so_no, self.item_code)

    def as_row(self):
        return {
            "SO No": self.so_no,
            "Item Code": self.item_code,
            "Item Name": self.item_name,
            "Qty": self.qty,
            "SO Delivery Date": _fmt(self.delivery_date),
            "Pending Qty": self.pending_qty,
            "Customer": self.customer,
            "Remarks": self.remarks,
        }


@dataclass
class Batch:
    """A consolidated production batch (Rule 1 output)."""

    batch_id: str
    item_code: str
    item_name: str
    qty: float
    so_delivery_date: date         # the SO Delivery Date that governs the batch
                                   # (earliest SO delivery date of the merged lines)
    source_so_refs: list = field(default_factory=list)
    total_process_time: Optional[float] = None  # filled by Rule 3: per-piece
                                                 # cycle-time sum (NOT x qty — that
                                                 # is "work needed" in the breakdown)
    # Per-process remaining {normalized process name -> qty}, summed across the
    # merged lines (Rule 1). None = run the full batch qty at every step. Internal
    # scheduling input consumed by Rule 6 — not serialized into the trace.
    process_qty: Optional[dict] = None
    # Commitment lane carried from the merged SO-lines: "open" (default) |
    # "committed". All lines in a batch share the same lane (Rule 1
    # never merges across lanes).
    commitment: str = "open"
    promised_date: Optional[date] = None

    def as_row(self):
        return {
            "Batch ID": self.batch_id,
            "Item Code": self.item_code,
            "Item Name": self.item_name,
            "Qty": self.qty,
            "SO Delivery Date": _fmt(self.so_delivery_date),
            "Source SOs": _fmt(self.source_so_refs),
            "Cycle time per piece (min)": self.total_process_time,
        }


# --------------------------------------------------------------------------- #
# Routing master
# --------------------------------------------------------------------------- #
@dataclass
class Process:
    """One step in an item's routing."""

    seq: int
    name: str
    cycle_time: Optional[float]      # minutes per piece
    total_time: Optional[float]      # minutes (as recorded; may be sparse)
    suggested_machine: Optional[str]  # raw label from the sheet
    allotted_machine: Optional[str]

    def as_row(self):
        return {
            "Seq": self.seq,
            "Process": self.name,
            "Cycle Time (min)": self.cycle_time,
            "Total Time (min)": self.total_time,
            "Suggested M/c": self.suggested_machine,
            "Allotted M/c": self.allotted_machine,
        }


@dataclass
class Routing:
    """An item's full recipe: ordered processes + RM info."""

    item_code: str
    description: str
    customer: str
    rm_type: str
    moq: Optional[float]
    processes: list = field(default_factory=list)  # list[Process]

    def total_cycle_time(self) -> float:
        """Rule 3 metric: sum of per-process cycle times (minutes).

        Confirmed against the SO Remarks oracle: this ranking (not the sparse
        'Total time' column) reproduces the documented priorities.
        """
        return sum(p.cycle_time for p in self.processes if isinstance(p.cycle_time, (int, float)))

    def as_row(self):
        return {
            "Item Code": self.item_code,
            "Description": self.description,
            "Customer": self.customer,
            "RM Type": self.rm_type,
            "MOQ": self.moq,
            "# Processes": len(self.processes),
            "Total Cycle Time (min)": self.total_cycle_time(),
        }


# --------------------------------------------------------------------------- #
# Resource masters
# --------------------------------------------------------------------------- #
@dataclass
class Machine:
    """A machine/resource. ``provisional`` = referenced by a routing but not yet
    in Machine master (PENDING_MASTER_DATA). Adding it to the Excel later needs
    no code change — it simply stops being provisional."""

    machine_no: str        # canonical id (normalized, e.g. 'CNC4')
    display_name: str      # as written in the master, e.g. 'CNC 4'
    machine_type: str
    hr_rate: Optional[float] = None
    provisional: bool = False
    available_hrs_per_day: Optional[float] = None  # from the master; drives the
                                                   # working window (>=threshold → two-shift)

    def is_two_shift(self, threshold: float = 12.0) -> bool:
        """A machine runs both shifts when its Available Hrs/Day is large (CNC/VMC
        ≈19.5). A small value (≈9.5) means single-shift 9am-6pm. Blank/None →
        two-shift (back-compat); explicit 0 stays single-shift."""
        if self.available_hrs_per_day is None:
            return True
        return self.available_hrs_per_day >= threshold

    def as_row(self):
        return {
            "Machine No": self.display_name,
            "Canonical": self.machine_no,
            "Type": self.machine_type,
            "Hr Rate (Rs)": self.hr_rate,
            "Available Hrs/Day": self.available_hrs_per_day,
            "Provisional": self.provisional,
        }


@dataclass
class Operator:
    name: str
    preferred_machines_raw: str = ""
    machines: list = field(default_factory=list)  # best-effort canonical ids
    shift: str = ""                                # "First shift" / "Second shift" / ""

    def as_row(self):
        return {
            "Operator": self.name,
            "Preferred Machines": self.preferred_machines_raw,
            "Shift": self.shift,
            "Parsed": _fmt(self.machines),
        }


@dataclass
class WorkCalendar:
    """Non-working days. Weekly off = every Thursday (weekday 3) + holidays +
    operator leaves."""

    weekly_off_weekday: int = 3  # Thursday
    holidays: list = field(default_factory=list)  # list[date]
    leaves: list = field(default_factory=list)     # list[(name, date)]

    def is_working_day(self, d: date) -> bool:
        if d.weekday() == self.weekly_off_weekday:
            return False
        if d in self.holidays:
            return False
        return True


# --------------------------------------------------------------------------- #
# Schedule + actuals
# --------------------------------------------------------------------------- #
@dataclass
class ScheduleEntry:
    """One process of one batch placed on a machine (Rule 6 output)."""

    batch_id: str
    item_code: str
    process_seq: int
    process_name: str
    machine: str            # canonical id chosen
    qty: float
    occupancy_min: float
    start: datetime
    end: datetime
    notes: str = ""
    so_refs: list = field(default_factory=list)   # SO number(s) this batch came from
    operator: str = ""                            # assigned operator (operator logic on)
    # Per-shift operator bookings for this machine-op (operator logic on): a list of
    # (segment_start, segment_end, operator) — the op is handed off to a fresh
    # qualified operator on each shift it crosses (an operator is booked only for
    # their own shift, never stretched across the 19:00/05:00 change). With operator
    # logic OFF (or a provisional/no-crew machine) it is a SINGLE operator-less
    # segment ``[(start, end, "")]`` — one block, no handoff — which as_row omits
    # because the operator is "" (it is NOT an empty list; only OS/off-machine
    # milestone entries, built without segments, carry an empty list). `operator`
    # above mirrors the FIRST segment's person for a single-line display; the
    # segments are the truth.
    op_segments: list = field(default_factory=list)

    def operator_label(self):
        """The operator(s) working this op, in start order, joined for display
        ("Alpha → Bravo" for a shift handoff). Falls back to the single ``operator``
        field when there are no per-shift segments (OS/off-machine milestones, or
        operator logic off). Every operator-naming surface — the Schedule table
        (``as_row``), the machine-wise view, and the Gantt — routes through this, so
        they can never disagree on who runs a job (cross-tab integration, 2026-07-26)."""
        seen = []
        for _s, _e, op in sorted(self.op_segments or [], key=lambda x: x[0]):
            if op and op not in seen:
                seen.append(op)
        return " → ".join(seen) if seen else (self.operator or "")

    def as_row(self):
        row = {
            "SO No": _fmt(self.so_refs),
            "Batch ID": self.batch_id,
            "Item Code": self.item_code,
            "Seq": self.process_seq,
            "Process": self.process_name,
            "Machine": self.machine,
        }
        _op = self.operator_label()
        if _op:
            row["Operator"] = _op   # column appears only when assigned (full handoff)
        row.update({
            "Qty": self.qty,
            "Occupancy (min)": round(self.occupancy_min, 2),
            "Start": _fmt(self.start),
            "End": _fmt(self.end),
            "Notes": self.notes,
        })
        return row


@dataclass
class Actual:
    """A daily production entry (Rule 8). Persisted via the durable store (book_store/storage).

    Mirrors the shop's 'Daily Production Entry' form: identity (date / shift / SO /
    item / process), output (produced / rejected), the actual setup time, and the
    six downtime/loss categories in minutes, plus free-text remarks.
    """

    # --- identity / context ------------------------------------------------- #
    so_no: str
    item_code: str
    entry_date: date
    qty_produced: float = 0.0
    qty_rejected: float = 0.0
    shift: str = ""
    item_name: str = ""      # auto-prompted from the routing (display only)
    process: str = ""        # which routing process this entry reports
    operator: str = ""       # who ran this process (required at capture time; legacy rows default "")
    id: str = ""             # stable unique id (for targeted rollback)
    # --- actual setup time (planned default lives in config.setup_time_min) -- #
    actual_setup_min: float = 0.0
    # --- downtime / loss categories (minutes) ------------------------------- #
    no_power_min: float = 0.0
    no_operator_min: float = 0.0
    tool_problem_min: float = 0.0
    machine_breakdown_min: float = 0.0
    no_load_min: float = 0.0
    other_work_min: float = 0.0
    remarks: str = ""
    mark_complete: bool = False   # user denotes the order complete on this entry

    # Loss categories that count as wasted machine time.
    DOWNTIME_FIELDS = (
        ("no_power_min", "No Power"),
        ("no_operator_min", "No Operator"),
        ("tool_problem_min", "Tool Problem"),
        ("machine_breakdown_min", "M/c Breakdown"),
        ("no_load_min", "No Load"),
        ("other_work_min", "Other Work"),
    )

    def total_downtime_min(self) -> float:
        return sum(getattr(self, f) for f, _ in self.DOWNTIME_FIELDS)

    @property
    def key(self):
        """The order this entry belongs to: (SO number, item code)."""
        return (self.so_no, self.item_code)

    def good_qty(self) -> float:
        """Pieces that actually fulfil the order = produced − rejected."""
        return max(self.qty_produced - self.qty_rejected, 0.0)

    def as_row(self):
        return {
            "Date": _fmt(self.entry_date),
            "Shift": self.shift,
            "SO No": self.so_no,
            "Item Code": self.item_code,
            "Item Name": self.item_name,
            "Process": self.process,
            "Operator": self.operator,
            "Qty Produced": self.qty_produced,
            "Qty Rejected": self.qty_rejected,
            "Good Qty": self.good_qty(),
            "Setup (min)": self.actual_setup_min,
            "No Power": self.no_power_min,
            "No Operator": self.no_operator_min,
            "Tool Problem": self.tool_problem_min,
            "M/c Breakdown": self.machine_breakdown_min,
            "No Load": self.no_load_min,
            "Other Work": self.other_work_min,
            "Total Downtime (min)": self.total_downtime_min(),
            "Remarks": self.remarks,
        }

    def to_json(self):
        return {
            "id": self.id,
            "so_no": self.so_no,
            "item_code": self.item_code,
            "entry_date": self.entry_date.isoformat(),
            "qty_produced": self.qty_produced,
            "qty_rejected": self.qty_rejected,
            "shift": self.shift,
            "item_name": self.item_name,
            "process": self.process,
            "operator": self.operator,
            "actual_setup_min": self.actual_setup_min,
            "no_power_min": self.no_power_min,
            "no_operator_min": self.no_operator_min,
            "tool_problem_min": self.tool_problem_min,
            "machine_breakdown_min": self.machine_breakdown_min,
            "no_load_min": self.no_load_min,
            "other_work_min": self.other_work_min,
            "remarks": self.remarks,
            "mark_complete": self.mark_complete,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Actual":
        return cls(
            id=d.get("id", ""),
            so_no=d.get("so_no", ""),
            item_code=d.get("item_code", ""),
            entry_date=date.fromisoformat(d["entry_date"]),
            qty_produced=d.get("qty_produced", 0.0),
            qty_rejected=d.get("qty_rejected", 0.0),
            shift=d.get("shift", ""),
            item_name=d.get("item_name", ""),
            process=d.get("process", ""),
            operator=d.get("operator", ""),
            # accept the old key name for forward-compat with any saved file.
            actual_setup_min=d.get("actual_setup_min", d.get("setup_time_min", 0.0)),
            no_power_min=d.get("no_power_min", 0.0),
            no_operator_min=d.get("no_operator_min", 0.0),
            tool_problem_min=d.get("tool_problem_min", 0.0),
            machine_breakdown_min=d.get("machine_breakdown_min", 0.0),
            no_load_min=d.get("no_load_min", 0.0),
            other_work_min=d.get("other_work_min", 0.0),
            remarks=d.get("remarks", d.get("downtime_reason", "")),
            mark_complete=d.get("mark_complete", False),
        )


@dataclass
class Order:
    """One order in the persistent order book, uniquely identified by the
    **(SO number, item code)** pair — one SO number may carry several item lines,
    each tracked as its own order.

    Status is DERIVED, never stored: COMPLETE if ``completed`` (set only when the
    user denotes it on a Rule 8 entry), else RUNNING if it has any actuals, else
    PENDING. ``produced_good`` / ``remaining`` are computed from the actuals."""

    so_no: str
    item_code: str
    item_name: str
    ordered_qty: float
    delivery_date: date
    completed: bool = False
    first_seen: str = ""
    commitment: str = "open"            # "open" | "committed"
    promised_date: Optional[date] = None  # locked promise (None while open)
    committed_at: Optional[str] = None    # ISO datetime string, snapshot time

    @property
    def key(self):
        """The unique identity of this order: (SO number, item code)."""
        return (self.so_no, self.item_code)

    def to_json(self):
        return {
            "so_no": self.so_no,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "ordered_qty": self.ordered_qty,
            "delivery_date": self.delivery_date.isoformat(),
            "completed": self.completed,
            "first_seen": self.first_seen,
            "commitment": self.commitment,
            "promised_date": self.promised_date.isoformat() if self.promised_date else None,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Order":
        # Legacy stored rows may carry the retired "urgent" lane (Urgent lane
        # removed 2026-07-29) — normalize it to "committed" on load so every
        # consumer only ever sees "open" | "committed".
        _c = d.get("commitment", "open")
        commitment = "committed" if _c == "urgent" else _c
        return cls(
            so_no=d["so_no"],
            item_code=d["item_code"],
            item_name=d.get("item_name", ""),
            ordered_qty=d.get("ordered_qty", 0.0),
            delivery_date=date.fromisoformat(d["delivery_date"]),
            completed=d.get("completed", False),
            first_seen=d.get("first_seen", ""),
            commitment=commitment,
            promised_date=(date.fromisoformat(d["promised_date"])
                           if d.get("promised_date") else None),
            committed_at=d.get("committed_at"),
        )


@dataclass
class Masters:
    """Everything loaded from the uploaded workbook, plus the loader report."""

    machines: dict = field(default_factory=dict)    # canonical id -> Machine
    routings: dict = field(default_factory=dict)     # item_code -> Routing
    operators: list = field(default_factory=list)    # list[Operator]
    calendar: WorkCalendar = field(default_factory=WorkCalendar)
    report: list = field(default_factory=list)       # list[dict] non-blocking issues

    def add_report(self, kind: str, ref: str, message: str):
        self.report.append({"kind": kind, "ref": ref, "message": message})


@dataclass
class PlanRun:
    """Carrier object threaded through the pipeline (Design spec §4)."""

    so_lines: list = field(default_factory=list)
    batches: list = field(default_factory=list)
    batches_sorted: list = field(default_factory=list)
    batches_prioritized: list = field(default_factory=list)
    schedule: list = field(default_factory=list)
    actuals: list = field(default_factory=list)
