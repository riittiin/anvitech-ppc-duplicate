"""Pure normalization helpers — the messy-real-world → clean-domain translations.

Everything here is a pure function with no Excel/openpyxl dependency, so it is trivial
to unit-test. These encode the data quirks documented in RULES.md / LESSONS.md:
machine-name spacing, OS/dispatch detection (including "OS" in a step *name*), and
tolerant header matching (never by fixed column index).
"""

from __future__ import annotations

import re

from ppc_engine.domain.resources import MachineKind
from ppc_engine.domain.routing import OperationKind


def canon_machine(raw) -> str:
    """Canonical machine id: uppercase, spaces removed.

    So ``"CNC 5"`` → ``"CNC5"`` and ``"M LATHE"`` → ``"MLATHE"``, which is how the
    routings write them — collapsing the master and routing spellings to one id with
    no alias table (RULES.md).
    """
    return re.sub(r"\s+", "", str(raw or "")).upper()


def parse_machine_options(cell) -> tuple[str, ...]:
    """Split a Suggested/Allotted cell into canonical machine ids.

    Alternatives are separated by ``/`` or ``,`` (e.g. ``"CNC3/CNC6"``,
    ``"MW1/MW2/MW3"``). Blanks and the ``OS`` sentinel are dropped (OS is not a
    machine — it means outsourced).
    """
    if cell is None:
        return ()
    parts = re.split(r"[/,]", str(cell))
    out = []
    for p in parts:
        cid = canon_machine(p)
        if cid and cid != "OS":
            out.append(cid)
    return tuple(out)


def _name_tokens(name: str) -> set[str]:
    """Uppercase word tokens of a step name (for whole-word matching like "OS")."""
    return set(t for t in re.split(r"[^A-Za-z0-9]+", name.upper()) if t)


def classify_operation(name, suggested, allotted, cycle_min) -> OperationKind:
    """Decide what kind of operation a routing step is (RULES.md Part 1 §4).

    Rules, in order:
      - DISPATCH  : the step name is/contains DISPATCH (tolerating the ``DISAPTCH``
                    misspelling seen in the data).
      - OUTSOURCED : Allotted or Suggested is ``OS``; OR the step *name* contains the
                    word ``OS`` with no real in-house machine; OR the cycle is a large
                    "thousands" value with no real machine. (An op with a real machine
                    is never OS, even if its cycle is large.)
      - MACHINING  : the (first) resource is a CNC/VMC.
      - INSPECTION : the (first) resource is an MI station or the CMM.
      - MANUAL     : any other real station.
    A step with none of the above and no machine falls through to MANUAL with empty
    options — the loader flags that item as a routing gap.
    """
    name = str(name or "")
    tokens = _name_tokens(name)
    if name.upper().startswith("DISPATCH") or "DISPATCH" in tokens or "DISAPTCH" in tokens:
        return OperationKind.DISPATCH

    options = parse_machine_options(allotted) or parse_machine_options(suggested)
    allot_c = canon_machine(allotted)
    sug_c = canon_machine(suggested)
    has_real_machine = bool(options)
    is_os_marker = allot_c == "OS" or sug_c == "OS"
    is_os_name = ("OS" in tokens) and not has_real_machine
    is_os_thousands = isinstance(cycle_min, (int, float)) and cycle_min >= 1000 and not has_real_machine
    if is_os_marker or is_os_name or is_os_thousands:
        return OperationKind.OUTSOURCED

    if options:
        first = options[0]
        if first.startswith("CNC") or first.startswith("VMC"):
            return OperationKind.MACHINING
        if first.startswith("MI") or first == "CMM":
            return OperationKind.INSPECTION
        return OperationKind.MANUAL

    # No machine, not OS, not dispatch → an incomplete routing step (data gap).
    return OperationKind.MANUAL


def machine_kind_from_type(type_text) -> MachineKind:
    """Map a Machine-master 'Machine Type' string to a MachineKind.

    CNC/VMC → MACHINING; inspection or measuring (CMM) → INSPECTION; everything else
    (deburring, packing, washing, band saw, lathe, drilling, …) → MANUAL.
    """
    t = str(type_text or "").lower()
    if "cnc" in t or "vertical machining" in t:
        return MachineKind.MACHINING
    if "inspection" in t or "measuring" in t:
        return MachineKind.INSPECTION
    return MachineKind.MANUAL


def _norm_header(text) -> str:
    """Normalize a header cell for matching: lowercase, collapse whitespace/newlines."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def match_header(header_cells, *candidates) -> int | None:
    """Find a column index by header NAME (never by fixed position — LESSONS.md).

    ``candidates`` are tried in priority order. For each, an EXACT normalized match
    wins first (across all columns); only if no exact match exists anywhere do we fall
    back to a substring match. Returns the column index, or None if nothing matches.

    Exact-first matters: it stops ``"SO Qty"`` being shadowed by ``"Pend SO Qty"``
    (a real bug in the old build).
    """
    norm = [_norm_header(c) for c in header_cells]
    wanted = [_norm_header(c) for c in candidates]
    for w in wanted:
        for i, h in enumerate(norm):
            if h == w:
                return i
    for w in wanted:
        for i, h in enumerate(norm):
            if w and w in h:
                return i
    return None


def parse_shift(text):
    """Parse a shift cell → Shift. 'Second shift' → SECOND, anything else → FIRST."""
    from ppc_engine.domain.resources import Shift

    return Shift.SECOND if "second" in str(text or "").lower() else Shift.FIRST


def parse_role(text):
    """Parse a role cell → Role (operator / helper / inspector), or None if unknown."""
    from ppc_engine.domain.resources import Role

    t = str(text or "").strip().lower()
    return {"operator": Role.OPERATOR, "helper": Role.HELPER, "inspector": Role.INSPECTOR}.get(t)
