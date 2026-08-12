"""Operator↔machine staffing — this is where the STABILITY rule is enforced.

The rule that this whole rebuild exists for (RULES.md Rule 1, LESSONS.md "the
decisive one"): **one operator mans one machine for a whole shift; no hour-by-hour
hopping.** This board makes that true by construction:

  - An assignment is keyed by (machine, shift-date, shift). Once a person is put on a
    machine for a shift, that is fixed for the whole shift.
  - A person can be committed to at most ONE machine per shift (tracked in ``_busy``).
  - The decoder only ever asks this board for an operator *per shift*, never
    per-hour — so a ping-ponging schedule simply cannot be expressed.

The board is READ-ONLY during evaluation: the decoder computes a tentative placement
by reading committed assignments and accumulating the *new* assignments it would make
in a local list, committing them only for the placement it actually chooses. (Within
one operation each (machine, shift-date, shift) is touched at most once, so no local
overlay is needed while laying a single operation.)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.resources import Machine, Operator, ROLE_FOR_KIND, Shift
from ppc_engine.worktime import effective_shift


def build_machine_pools(masters: Masters) -> dict[str, tuple[Operator, ...]]:
    """Precompute, per machine, the operators who could ever run it — role-matched and
    qualified — pre-sorted scarce-first (fewest qualified machines, then name).

    This is static for a given shop, so computing it once turns the per-window operator
    search from "scan all 19 people and sort" into "scan ~6 pre-sorted candidates".
    """
    pools: dict[str, tuple[Operator, ...]] = {}
    for mid, machine in masters.machines.items():
        # Qualification is EXACTLY the machine list the admin set in Settings — role is
        # NOT a gate (2026-08-07). Role is inherited by name from the workbook's operator
        # sheet, a fossil since 2026-07-18, and is never re-derived from what the admin
        # assigned; gating on it silently discarded the assignment. One person also
        # legitimately spans kinds (manual stations AND a CNC), which a single role can
        # never express. Live bug: Sandeep Kumar was given CNC4 in Settings, was dropped
        # from CNC4's pool for being a workbook "helper", showed 0% in Analytics, and
        # CNC4 sat idle with work waiting — with nothing anywhere reporting it.
        eligible = [o for o in masters.operators if mid in o.qualified_machines]
        eligible.sort(key=lambda o: (o.flexibility, o.name))
        pools[mid] = tuple(eligible)
    return pools


class StaffingBoard:
    """Tracks which operator mans which machine, and each operator's busy TIME intervals.

    Short-job exception (2026-07-24): an operator is reserved for a machine only for the
    actual DURATION of the work, not the whole shift. So a LONG job keeps its operator
    busy all shift (one-operator-per-machine-per-shift stability is preserved by
    construction — they can't be anywhere else), while a SHORT job frees the operator to
    man another idle machine later that shift. Availability is an interval-overlap check.
    """

    def __init__(self, pools: dict[str, tuple[Operator, ...]] | None = None) -> None:
        # (machine_id, shift_date, shift) -> operator name that (last) manned it — a soft
        # preference for machine stability, not a hard lock (short jobs may share).
        self._assign: dict[tuple[str, date, Shift], str] = {}
        # operator name -> list of committed busy (start, end) intervals.
        self._intervals: dict[str, list[tuple[datetime, datetime]]] = {}
        # machine id -> pre-sorted eligible operators (scarce-first). See build_machine_pools.
        self._pools: dict[str, tuple[Operator, ...]] = pools or {}
        # operator name -> cumulative committed busy minutes (for the "balanced" pick).
        self._load: dict[str, float] = {}

    def clone(self) -> "StaffingBoard":
        """A scratch copy that can be written to without touching this board.

        Needed by parallel splitting: the parts of one split operation run on
        different machines in the SAME shift, so part B must see part A's
        operator as already taken. Without that, ``candidate_operator`` would
        hand the same person to both machines and the plan would double-book
        them. ``_pools`` is static for a shop, so it is shared, not copied.
        """
        other = StaffingBoard(self._pools)
        other._assign = dict(self._assign)
        other._intervals = {k: list(v) for k, v in self._intervals.items()}
        other._load = dict(self._load)
        return other

    def add_load(self, name: str, minutes: float) -> None:
        """Record committed work for an operator (drives the 'balanced' pick policy)."""
        self._load[name] = self._load.get(name, 0.0) + minutes

    def operator_for(self, machine_id: str, day: date, shift: Shift) -> str | None:
        """The operator that (last) manned ``machine_id`` on this shift, or None. A
        machine-stability PREFERENCE — reuse them only if still free for the new interval."""
        return self._assign.get((machine_id, day, shift))

    def free_during(self, name: str, start: datetime, end: datetime) -> bool:
        """True if ``name`` has no committed busy interval overlapping [start, end)."""
        for s, e in self._intervals.get(name, ()):
            if s < end and start < e:
                return False
        return True

    def candidate_operator(
        self,
        machine: Machine,
        day: date,
        shift: Shift,
        start: datetime,
        end: datetime,
        masters: Masters,
        config: PlanConfig,
    ) -> str | None:
        """Pick the best free, qualified operator to man ``machine`` this shift.

        An operator is eligible when ALL hold:
          - their role matches the machine kind (operator↔machining, helper↔manual,
            inspector↔inspection),
          - they are qualified for this machine (it's in their qualified set),
          - they are on THIS shift on this date (after Friday rotation), and
          - they are available (shop open + not on personal leave), and
          - they are not already manning another machine this shift.

        Among eligible people, pick the **least flexible** first (scarce-first — keeps
        flexible people free for machines only they can run; a measured win from the
        old build). Ties break by name for determinism.

        Returns the chosen operator name, or None if nobody is available (in which
        case the machine cannot run this shift and the operation must wait).
        """
        # Free, eligible operators for this machine+shift (pool is pre-sorted
        # scarce-first: ascending flexibility, then name).
        free = [
            op
            for op in self._pools.get(machine.id, ())
            if effective_shift(op, day, config) == shift
            and masters.calendar.is_operator_available(op.name, day)
            and self.free_during(op.name, start, end)
        ]
        if not free:
            return None

        pick = getattr(config, "operator_pick", "scarce")
        if pick == "flexible":
            return free[-1].name  # most flexible (pool sorted ascending flexibility)
        if pick == "balanced":
            # least cumulative load; ties → scarce (fewer machines) → name.
            return min(free, key=lambda o: (self._load.get(o.name, 0.0), o.flexibility, o.name)).name
        return free[0].name  # "scarce" (default): least flexible

    def longest_available_prefix(self, machine, day, shift, start, end,
                                 masters, config):
        """(operator, usable_end) — the longest stretch from ``start`` that some
        qualified operator on this shift is free for, or (None, start).

        ``candidate_operator`` is all-or-nothing: it needs somebody free for the
        WHOLE interval, so a single minute of conflict rejects them and the caller
        idles the entire window. This answers the weaker, more useful question:
        how long can somebody actually start now for?
        """
        best_name, best_end = None, start
        floor = timedelta(minutes=float(getattr(config, "min_slice_min", 30.0) or 0.0))
        for op in self._pools.get(machine.id, ()):
            if effective_shift(op, day, config) != shift:
                continue
            if not masters.calendar.is_operator_available(op.name, day):
                continue
            limit = end
            for s_, e_ in self._intervals.get(op.name, ()):
                if e_ <= start or s_ >= end:
                    continue
                if s_ <= start:
                    limit = start
                    break
                limit = min(limit, s_)
            if limit > best_end:
                best_name, best_end = op.name, limit
        if best_name is None or best_end - start < floor:
            return None, start
        return best_name, best_end

    def commit(self, machine_id: str, day: date, shift: Shift, name: str,
               start: datetime, end: datetime) -> None:
        """Book ``name`` onto ``machine_id`` for the interval [start, end) this shift.
        The operator is now busy for exactly that time (not the whole shift), so a short
        job leaves them free to man another machine later. ``_assign`` records them as the
        machine's shift operator (a stability preference for the next op on this machine)."""
        self._assign[(machine_id, day, shift)] = name
        self._intervals.setdefault(name, []).append((start, end))
