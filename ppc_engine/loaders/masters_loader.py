"""Build the shop Masters (machines, operators, calendar, routings) from the workbook.

Header-driven and tolerant. Records data gaps into the DataReport instead of crashing.
"""

from __future__ import annotations

from datetime import date, datetime

from ppc_engine.domain.calendar import ShopCalendar, THURSDAY
from ppc_engine.domain.resources import Machine, MachineKind, Operator
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.loaders.normalize import (
    canon_machine,
    classify_operation,
    machine_kind_from_type,
    parse_machine_options,
    parse_role,
    parse_shift,
)
from ppc_engine.loaders.report import DataReport, GapKind
from ppc_engine.loaders.workbook import Table, find_sheet, locate_header_row, rows_of

# Default daily hours when a machine's cell is blank.
_DEFAULT_HRS = {MachineKind.MACHINING: 19.5}


def _as_date(value):
    """Coerce an Excel cell to a date, or None if it isn't a recognisable date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def load_machines(wb) -> dict[str, Machine]:
    """Read the 'Machine master' sheet into {canonical id → Machine}."""
    ws = find_sheet(wb, "Machine master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Machine No", "Machine Type")
    t = Table.from_rows(rows, h)
    c_type = t.col("Machine Type")
    c_no = t.col("Machine No")
    c_hrs = t.col("Available Hrs/Day", "Available Hrs", "Available Hrs / Day")

    machines: dict[str, Machine] = {}
    for row in t.data_rows:
        mid = canon_machine(t.get(row, c_no))
        if not mid:
            continue
        type_text = str(t.get(row, c_type) or "").strip()
        kind = machine_kind_from_type(type_text)
        hrs = t.get(row, c_hrs)
        hrs = float(hrs) if isinstance(hrs, (int, float)) else _DEFAULT_HRS.get(kind, 9.5)
        machines[mid] = Machine(mid, type_text, kind, hrs)
    return machines


def load_operators(wb) -> tuple[Operator, ...]:
    """Read the 'Operator & shift Master' people table into Operators.

    Note the sheet has two 'Shift' columns (the person's shift, and the shift-timings
    mini-table). match_header returns the FIRST exact 'Shift' — the person's shift.
    """
    ws = find_sheet(wb, "Operator & shift Master", "Operator & Shift Master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Operator Name", "Preferred Machines")
    t = Table.from_rows(rows, h)
    c_name = t.col("Operator Name", "Operator")
    c_role = t.col("Role")
    c_mach = t.col("Preferred Machines", "Preferred Machine")
    c_shift = t.col("Shift")

    operators: list[Operator] = []
    for row in t.data_rows:
        name = str(t.get(row, c_name) or "").strip()
        role = parse_role(t.get(row, c_role))
        if not name or role is None:
            continue
        quals = frozenset(parse_machine_options(t.get(row, c_mach)))
        operators.append(Operator(name, role, quals, parse_shift(t.get(row, c_shift))))
    return tuple(operators)


def load_calendar(wb) -> ShopCalendar:
    """Read the 'Weekly off & holiday master' into a ShopCalendar.

    Weekly off is Thursday (the sheet states 'Every Thursday'); named holidays close
    the shop; a 'Leave' row removes only that operator on that date.
    """
    ws = find_sheet(wb, "Weekly off & holiday master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Category", "Day / Date", "Day/Date")
    t = Table.from_rows(rows, h)
    c_cat = t.col("Category")
    c_name = t.col("Name")
    c_date = t.col("Day / Date", "Day/Date", "Date")

    holidays: set[date] = set()
    leaves: dict[str, set[date]] = {}
    for row in t.data_rows:
        cat = str(t.get(row, c_cat) or "").strip().lower()
        d = _as_date(t.get(row, c_date))
        if cat.startswith("holiday") and d is not None:
            holidays.add(d)
        elif cat.startswith("leave") and d is not None:
            name = str(t.get(row, c_name) or "").strip()
            if name:
                leaves.setdefault(name, set()).add(d)
    return ShopCalendar(
        weekly_off_weekday=THURSDAY,
        holidays=frozenset(holidays),
        leaves={k: frozenset(v) for k, v in leaves.items()},
    )


def load_routings(wb, report: DataReport, flexible_machines: bool = False) -> dict[str, Routing]:
    """Read the "Item's process Master" into {item_code → Routing}.

    Process columns come in fixed 5-wide blocks (name, cycle, total, suggested,
    allotted). We anchor on the 'Process 1' header by NAME, then step by 5, validating
    each block's header, so a reordered/extra column elsewhere doesn't break us.

    ``flexible_machines``: how to build an in-house op's machine options.
      - False (default): the Allotted machine only (falling back to Suggested if
        Allotted is blank) — the planner's locked choice.
      - True: the UNION of Allotted + Suggested (Allotted first / preferred). Suggested
        lists every machine that CAN do the step, so this hands the scheduler the full
        feasible set to load-balance across (RULES.md: the planner may override the
        lock). This is the machine-flexibility lever — see OPTIMIZATION.md.
    """
    ws = find_sheet(wb, "Item's process Master", "Items process Master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Item code", "Item Code")
    t = Table.from_rows(rows, h)
    c_code = t.col("Item code", "Item Code")
    c_desc = t.col("Item Description", "Item Desc")

    # Anchor on "Process 1" (normalised, space-insensitive so "Process 1\n" matches).
    proc1 = None
    for i, cell in enumerate(t.header):
        norm = "".join(str(cell or "").lower().split())
        if norm == "process1":
            proc1 = i
            break
    if proc1 is None:
        raise KeyError("could not find the 'Process 1' column in the routing sheet")

    def _block_is_process(header_row, start, n) -> bool:
        if start >= len(header_row):
            return False
        norm = "".join(str(header_row[start] or "").lower().split())
        return norm == f"process{n}"

    routings: dict[str, Routing] = {}
    for row in t.data_rows:
        code = str(t.get(row, c_code) or "").strip()
        if not code:
            continue
        desc = str(t.get(row, c_desc) or "").strip()
        ops: list[Operation] = []
        for p in range(12):
            start = proc1 + p * 5
            if not _block_is_process(t.header, start, p + 1):
                break  # no more process blocks
            name = t.get(row, start)
            if name is None or str(name).strip() == "":
                break  # this item uses fewer processes
            name = str(name).strip()
            cyc = t.get(row, start + 1)
            cyc = float(cyc) if isinstance(cyc, (int, float)) else 0.0
            suggested = t.get(row, start + 3)
            allotted = t.get(row, start + 4)
            kind = classify_operation(name, suggested, allotted, cyc)
            if kind in (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION):
                allot_opts = parse_machine_options(allotted)
                sug_opts = parse_machine_options(suggested)
                if flexible_machines:
                    # Union, Allotted first (preferred), then any extra Suggested — the
                    # full set of machines the shop says can do this step.
                    options = tuple(dict.fromkeys(allot_opts + sug_opts))
                else:
                    options = allot_opts or sug_opts
                if not options:
                    # In-house step with no machine and not OS/dispatch → a real gap.
                    report.add(
                        GapKind.ROUTING_GAP,
                        code,
                        f"step '{name}' (seq {p + 1}) has no machine and isn't outsourced",
                    )
            else:
                options = ()
            ops.append(Operation(p + 1, name, kind, options, cyc))
        routings[code] = Routing(code, desc, tuple(ops))
    return routings


def register_provisional_machines(
    machines: dict[str, Machine], routings: dict[str, Routing], report: DataReport
) -> None:
    """Register any machine referenced by a routing but missing from the master.

    RULES.md's forgiveness principle: a routing may point at a machine (e.g. a new
    CNC) not yet in the Machine master. Register it provisionally so the shop model is
    complete, and record it — the user adds it to the Excel later with no code change.
    (Not needed for Test5, but future files may have it.)
    """
    referenced: set[str] = set()
    for routing in routings.values():
        for op in routing.operations:
            referenced.update(op.machine_options)
    for mid in sorted(referenced):
        if mid in machines:
            continue
        if mid.startswith("CNC") or mid.startswith("VMC"):
            kind, hrs = MachineKind.MACHINING, 19.5
        elif mid.startswith("MI") or mid == "CMM":
            kind, hrs = MachineKind.INSPECTION, 9.5
        else:
            kind, hrs = MachineKind.MANUAL, 9.5
        machines[mid] = Machine(mid, "PROVISIONAL", kind, hrs)
        report.add(GapKind.PROVISIONAL_MACHINE, mid, "referenced by a routing but not in the Machine master")
