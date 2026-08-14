"""Can the owner's book be solved at all — and if so, with which encoding?

READ-ONLY. Plans nothing, writes nothing to any store. Throwaway diagnostic
code; its OUTPUT is the deliverable
(``docs/superpowers/plans/2026-08-14-cp-tractability-findings.md``).

It exists because the spec (§5.1, §5.4) deliberately leaves two encodings open
and says measurement decides:

  * Rule 2 permits an operation to be HELD across an unmanned shift. Encoding
    that exactly (E2) costs per-(task, machine, shift) processing variables.
    Forbidding it (E1) costs one optional interval per (machine, shift) and
    nothing else -- but forbidding it is a restriction beyond the shop's four
    rules, so shipping E1 is the owner's call and must come with a number.
  * Rule 4's setup credit ("credit") is Rule 4 as written; charging 90 minutes
    unconditionally ("always") is conservative but is not Rule 4.

And because Task 6 measured, incidentally, that 12 batches solve OPTIMAL in
~10 s, 30 in ~19 s, and 50 return UNKNOWN at ~46,000 variables -- while the
owner's real book is ~58 batches. So the first question is no longer which
encoding is cheaper. It is whether a full solve is reachable AT ALL inside a
worker's realistic time budget.

FIVE MODES
    sizes    build each variant and report model size only (1-second solves)
    variants the four E1/E2 x credit/always variants at full scale
    scaling  the chosen variant at increasing batch counts -- where does it stop
             returning a usable answer?
    head     price the provably-dead head release bound (with vs without)
    span     how many operations in an E2 solution actually do the thing E1
             forbids -- i.e. what E1's restriction really costs

NOTE ON THE BOOK. Without ``MONGODB_URI`` this runs on the repo's generated
workbook at the owner's SCALE (``--items 58 --orders 68`` -> 61 batches, 68 SO
lines). That is a SAME-SHAPE PROXY, not his data: same order count, same
routing depth, same two-shift calendar, but a 14-machine / 19-operator fleet
where the real shop has ~26 machines. Size conclusions transfer; the exact
late-day numbers do not.

Run with the venv that has pyjobshop:
    ./.venv/bin/python scripts/cp_tractability_spike.py --mode variants
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VARIANTS = [
    ("E1 + credit", dict(hold=False, setup="credit")),
    ("E2 + credit", dict(hold=True, setup="credit")),
    ("E1 + always", dict(hold=False, setup="always")),
    ("E2 + always", dict(hold=True, setup="always")),
]


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #

def _book(args):
    from scripts.tardiness_bound import load_book, load_demo_book

    book = (load_book() if args.real
            else load_demo_book(n_items=args.items, n_orders=args.orders,
                                seed=args.seed))
    book = _squeeze(book, args.due_shift)
    if args.limit:
        # Truncating ONE book keeps the fleet, calendar, config and plan clock
        # byte-identical and moves only the number of batches, so a size
        # comparison measures size and nothing else.
        book = book._replace(batches=book.batches[:args.limit])
    return book


def _squeeze(book, days: int):
    """Pull every delivery date ``days`` earlier, in memory only.

    Not cosmetic. The generated workbook's due dates are loose enough that the
    whole book finishes ON TIME, and a book with zero total late-days is the
    EASY case for this engine twice over: phase 1's objective bottoms out at its
    own lower bound the moment any feasible schedule is found, and phase 2 is
    skipped outright (``solve.py``: nothing to even out). The owner's book is
    nothing like that — Test9 carries ~1,062 late-days, most orders overdue —
    so measuring tractability on a zero-tardiness instance would measure
    FEASIBILITY and report it as optimisation.

    Read-only with respect to the store: the batches are rebuilt in memory and
    the loaded workbook is never written.
    """
    import dataclasses
    from datetime import timedelta

    if not days:
        return book
    batches = [
        dataclasses.replace(
            b, so_delivery_date=b.so_delivery_date - timedelta(days=days))
        if getattr(b, "so_delivery_date", None) else b
        for b in book.batches
    ]
    return book._replace(batches=batches)


def _solve(book, batches, *, hold, setup, time_limit, horizon_days, workers,
           seed=42):
    """One solve, timed. Returns ``(Solved, wall seconds)``.

    A failed solve still carries ``stats``, which is where the model sizes live,
    so a 1-second run is a legitimate way to measure size without paying for a
    search.
    """
    from cp_engine import solve as cp_solve

    started = time.perf_counter()
    res = cp_solve.solve_book(
        batches, book.masters, book.config, book.plan_start,
        time_limit=time_limit, horizon_days=horizon_days, num_workers=workers,
        hold_across_unmanned_shift=hold, setup_mode=setup, seed=seed)
    return res, time.perf_counter() - started


def _row(label, res, secs, extra=""):
    stats = res.stats
    late = res.total_late_days
    bound = res.lower_bound_days
    return (f"{label:<16} {stats.get('tasks', 0):>6d} "
            f"{stats.get('variables', 0):>9d} {stats.get('booleans', 0):>9d} "
            f"{stats.get('constraints', 0):>9d} "
            f"{str(res.status):>10} "
            f"{('-' if late is None else f'{late:.0f}'):>10} "
            f"{('-' if bound is None else f'{bound:.0f}'):>8} "
            f"{secs:>8.1f} {extra}")


_HEADER = (f"{'variant':<16} {'tasks':>6} {'vars':>9} {'bools':>9} "
           f"{'constr':>9} {'status':>10} {'late-days':>10} {'bound':>8} "
           f"{'secs':>8}")


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def mode_sizes(book, args, out):
    """Model size per variant. One-second solves -- size is decided at build
    time, so this costs seconds and answers the cheaper half of the question."""
    print("\n== MODEL SIZE (1-second solves; sizes are phase-1, pre-phase-2) ==")
    print(_HEADER)
    for label, opts in VARIANTS:
        res, secs = _solve(book, book.batches, hold=opts["hold"],
                           setup=opts["setup"], time_limit=1.0,
                           horizon_days=args.horizon_days, workers=args.workers)
        print(_row(label, res, secs))
        out.append({"mode": "sizes", "variant": label, "secs": secs,
                    "status": res.status, "stats": res.stats})


def mode_variants(book, args, out):
    """The four variants at full scale, each given the same budget."""
    print(f"\n== VARIANTS, {len(book.batches)} batches, "
          f"{args.time_limit:.0f}s each, {args.workers} workers ==")
    print(_HEADER)
    for label, opts in VARIANTS:
        res, secs = _solve(book, book.batches, hold=opts["hold"],
                           setup=opts["setup"], time_limit=args.time_limit,
                           horizon_days=args.horizon_days, workers=args.workers)
        print(_row(label, res, secs), flush=True)
        out.append({"mode": "variants", "variant": label, "secs": secs,
                    "status": res.status, "status_ok": res.status_ok,
                    "late_days": res.total_late_days,
                    "bound": res.lower_bound_days,
                    "spread": res.spread, "stats": res.stats})


def mode_scaling(book, args, out):
    """The chosen variant at increasing batch counts.

    Truncates ONE book rather than loading several: the fleet, calendar, config
    and plan clock stay byte-identical and only the number of batches moves, so
    the curve measures size and nothing else.
    """
    counts = [int(n) for n in args.scale.split(",")]
    hold = not args.e1
    setup = args.setup
    print(f"\n== SCALING, {'E2' if hold else 'E1'} + {setup}, "
          f"{args.time_limit:.0f}s each, {args.workers} workers ==")
    print(_HEADER)
    for n in counts:
        batches = book.batches[:n]
        if len(batches) < n:
            print(f"  (only {len(batches)} batches available; stopping)")
            break
        res, secs = _solve(book, batches, hold=hold, setup=setup,
                           time_limit=args.time_limit,
                           horizon_days=args.horizon_days, workers=args.workers)
        print(_row(f"{n} batches", res, secs,
                   extra=f"p1={res.stats.get('phase_one_status', '-')}"),
              flush=True)
        out.append({"mode": "scaling", "batches": n, "hold": hold,
                    "setup": setup, "secs": secs, "status": res.status,
                    "status_ok": res.status_ok,
                    "late_days": res.total_late_days,
                    "bound": res.lower_bound_days, "stats": res.stats})


def mode_head(book, args, out):
    """Price the head release bound.

    ``rules.add_release`` posts TWO bounds per overlapping step pair. The tail
    bound provably dominates the head bound (rules.py's own docstring proves it
    from ``a.end - a.start >= processing``), so the head can never bind -- yet it
    costs one linear constraint per pair, plus ``_setup_charged``'s per-pair
    linear expression over every candidate machining assignment, in a model whose
    whole premise is that the previous encoding died of size.

    Measured by monkeypatching ``rules.add_release`` with a tail-only twin. The
    twin re-uses ``rules._overlapping_pairs`` so only the two-constraint core is
    duplicated; if ``add_release`` changes, this must be re-derived.
    """
    from cp_engine import rules

    def _tail_only(cp_model, variables, built, config):
        out_k: dict = {}
        for job in built.jobs:
            pairs = rules._overlapping_pairs(built, job)
            if not pairs:
                continue
            k = cp_model.new_int_var(1, min(q for _a, _b, q, _c in pairs),
                                     f"k_{job.key}")
            out_k[job.key] = k
            for a_idx, b_idx, qty, cutting in pairs:
                a = variables.task_vars[a_idx]
                b = variables.task_vars[b_idx]
                cp_model.add(qty * b.start >= qty * a.end - (qty - k) * cutting)
        return out_k

    hold = not args.e1
    print(f"\n== HEAD RELEASE BOUND, {'E2' if hold else 'E1'} + {args.setup}, "
          f"{len(book.batches)} batches, {args.time_limit:.0f}s each ==")
    print(_HEADER)
    real = rules.add_release
    try:
        for label, fn in (("with head", real), ("tail only", _tail_only)):
            rules.add_release = fn
            res, secs = _solve(book, book.batches, hold=hold, setup=args.setup,
                               time_limit=args.time_limit,
                               horizon_days=args.horizon_days,
                               workers=args.workers)
            print(_row(label, res, secs), flush=True)
            out.append({"mode": "head", "variant": label, "secs": secs,
                        "status": res.status, "late_days": res.total_late_days,
                        "bound": res.lower_bound_days, "stats": res.stats})
    finally:
        rules.add_release = real


def mode_span(book, args, out):
    """What E1's restriction actually costs, in operations.

    E1 forbids an operation from OVERLAPPING a shift in which its machine is
    rostered but unstaffed. So: solve under E2, then count the operations that
    do exactly that. If the count is zero or near zero, E1 forbids nothing the
    solver wanted to do and the restriction is free.

    Two counts are reported because they answer different questions:
      * ``overlaps``  -- E1 forbids this outright (its dark interval no-overlaps
        the work interval), so this is the count that prices E1.
      * ``contains``  -- the whole unstaffed shift sits inside the operation:
        a part genuinely held in the chuck overnight, Rule 2's clause in the
        flesh.
    A third count, ``breaks``, is context: operations spanning a CALENDAR break
    (an off day, or a single-shift station's night). BOTH encodings permit that
    -- it is a pyjobshop break, not a roster decision -- so it is not E1's cost.
    """
    from cp_engine import domain
    from cp_engine.rules import _machine_runs

    res, secs = _solve(book, book.batches, hold=True, setup=args.setup,
                       time_limit=args.time_limit,
                       horizon_days=args.horizon_days, workers=args.workers)
    print(f"\n== E2 SPAN ANALYSIS, {len(book.batches)} batches, "
          f"{secs:.0f}s, status {res.status} ==")
    if not res.status_ok:
        print("  no solution to analyse")
        out.append({"mode": "span", "status": res.status, "secs": secs})
        return

    shop = domain.build_shop(book.masters, {})
    rostered = shop.machining_ids
    staffed = set(res.genome.get("cp_roster", {}))

    overlaps = contains = breaks = total = 0
    detail = []
    for key, (start, end) in res.windows.items():
        mid = res.machine_of.get(key)
        if mid is None:
            continue
        total += 1
        machine = shop.machines.get(mid)
        for shift in res.shifts:
            if shift.start >= end or shift.end <= start:
                continue                       # no overlap at all
            if machine is not None and not _machine_runs(machine, shift):
                breaks += 1
                continue                       # a calendar break: legal in BOTH
            if mid not in rostered:
                continue                       # not a rostered machine
            if (mid, shift.index) in staffed:
                continue                       # somebody was on it
            overlaps += 1
            if start <= shift.start and shift.end <= end:
                contains += 1
                detail.append((key, mid, shift.index))

    print(f"  operations placed on a machine        : {total}")
    print(f"  ... overlapping an UNSTAFFED rostered shift (E1 forbids): "
          f"{overlaps}")
    print(f"  ... fully CONTAINING one (held in the chuck)            : "
          f"{contains}")
    print(f"  ... overlapping a calendar break (legal under BOTH)     : "
          f"{breaks}")
    for key, mid, shift_idx in detail[:20]:
        print(f"      held: {key} on {mid} across shift {shift_idx}")
    out.append({"mode": "span", "status": res.status, "secs": secs,
                "late_days": res.total_late_days, "ops": total,
                "e1_forbidden_overlaps": overlaps, "held_across": contains,
                "calendar_break_spans": breaks,
                "detail": [[list(k), m, s] for k, m, s in detail[:50]]})


_MODES = {"sizes": mode_sizes, "variants": mode_variants,
          "scaling": mode_scaling, "head": mode_head, "span": mode_span}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="sizes", choices=sorted(_MODES))
    ap.add_argument("--time-limit", type=float, default=300.0,
                    help="TOTAL seconds per solve, both phases together")
    ap.add_argument("--horizon-days", type=int, default=120)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--real", action="store_true",
                    help="the owner's book from the store (needs MONGODB_URI)")
    ap.add_argument("--items", type=int, default=58)
    ap.add_argument("--orders", type=int, default=68)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=0,
                    help="use only the first N batches of the book")
    ap.add_argument("--due-shift", type=int, default=0,
                    help="pull every delivery date this many days earlier, so "
                         "the book is genuinely tardy like the owner's")
    ap.add_argument("--scale", default="10,20,30,40,50,61",
                    help="batch counts for --mode scaling")
    ap.add_argument("--e1", action="store_true",
                    help="scaling/head/span: use E1 instead of E2")
    ap.add_argument("--setup", default="credit", choices=("credit", "always"))
    ap.add_argument("--json", default=None, help="append results here")
    args = ap.parse_args()

    book = _book(args)
    dues = sorted(b.so_delivery_date for b in book.batches
                  if getattr(b, "so_delivery_date", None))
    print(f"{len(book.so_lines)} SO lines, {len(book.batches)} batches, "
          f"{len(book.masters.machines)} machines, "
          f"{len(book.masters.operators)} operators, "
          f"plan start {book.plan_start}")
    if dues:
        print(f"delivery dates {dues[0]} .. {dues[-1]} "
              f"(due-shift {args.due_shift} days), horizon "
              f"{args.horizon_days} days")
    print("NOTE: model sizes below are PHASE-1 sizes. Phase 2 adds roughly 60 "
          "constraints per late order on top (stats.phase_two_constraints is "
          "the post-phase-2 total where a phase 2 ran).")

    out: list = []
    _MODES[args.mode](book, args, out)
    if args.json:
        path = Path(args.json)
        prior = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps(prior + out, indent=1, default=str))
        print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
