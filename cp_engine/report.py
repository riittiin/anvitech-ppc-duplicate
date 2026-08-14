"""Does the plan on screen match the plan that was solved?

The solve runs off-box and takes minutes; the app replays its genome on every
page load. The model computes times and the decoder recomputes them, so they are
two definitions of one schedule — and **if they disagree, the plan on screen is
not the plan that was solved**, which also means the late-days the search
optimised are not the late-days the published plan realises. This repo has paid
for that class repeatedly (the Gantt saying 07-Sep while the delay report said
04-Sep; four reporting features rebuilding the shop's working hours and missing
9,470 minutes of real planned work). So it is CHECKED, not assumed.

It has already earned its keep once: a genome that could not reproduce its own
solve (2-7 days of drift on 12 books) was caught here and fixed by carrying the
solved start and the bench crew, not by widening a tolerance. **If drift is
material, tighten the model — never loosen the decoder** (spec §5.3).

WHAT IS TRUE, STATED AS WHAT WAS MEASURED — AND DRIFT IS NOT ALWAYS ZERO. Over
40 solved books / 280 orders, again with an independent generator, and again on
this module's own contended fixture under BOTH encodings, completion-DATE drift
is exactly 0 in both directions. It is nevertheless **an empirical property of
those books, not an invariant**: measured 2026-08-14 (Task 10), a shop whose
benches run the day shift only, with one helper across them and CNC batches long
enough to span the 19:00 change, drifts a **FULL DAY on every order** under the
shipping E1 default, on a book the solver calls OPTIMAL. Cause, read off both
schedules op by op rather than assumed: the solve starts a bench step while its
CNC feeder is still cutting; the decoder cannot release it until the feeder
leaves the chuck at 19:30, by which time the single-shift bench has closed and
the next window is two days out across the weekly off. That is the documented
one-op-at-a-time limitation (``decode._JobState`` tracks one op at a time, while
the model's release is a linear bound on start variables that fires
mid-operation) amplified over a day boundary — not a new defect, and its remedy
is a change to the decoder's concurrency model that this plan deliberately did
not take on. It is the owner's own shop shape, so expect real rows live.

At MINUTE resolution the residual is one-sided LATE (+83, +191 and +1,379 all
OBSERVED; **nothing establishes a ceiling**). That direction is conservative for
the floor — work arrives earlier than the sheet says. The EARLY direction is not:
it means the decoder handed itself capacity the solver withheld. The sign of
``days`` is what distinguishes them, and it is never collapsed to a distance.

REPLAY PATH — imports pyjobshop/ortools NEITHER directly nor transitively.
``genome`` and ``domain`` are both solver-free, and today's book signature is
computed from ``domain.build_jobs``, never from a ``Built`` (building one needs
pyjobshop, which Render does not have).

``roster_engine.report`` is the ONE module this package imports from its sibling.
Its four checks are an INDEPENDENT implementation of the same four shop rules,
written for a different engine — which is exactly what makes a green result mean
something. They are run unchanged; a rule that needs bending to pass would be a
model bug, not a rule exception.
"""

from __future__ import annotations

from datetime import date, datetime

from cp_engine import domain
from cp_engine.genome import _book_signature
# Spelled dotted, deliberately: ``tests/test_cp_domain.py`` allows this package
# exactly one line naming ``roster_engine.report`` and forbids every other
# import from the sibling engine. Bound as a MODULE, not as four names, so the
# checks that are run are always the ones that file currently defines.
import roster_engine.report as rules

KIND_DRIFT = "CP_PLAN_DRIFT"
KIND_STALE = "CP_GENOME_STALE"

# The three checks that are RULE BREACHES on a CP plan — each must read 0, and a
# non-zero is a defect in the model or the decoder. ``IDLE_CAPACITY`` is
# deliberately absent: see ``all_violations``.
RULE_KINDS = ("OPERATOR_SPLIT_SHIFT", "OPERATION_SEGMENTED",
              "MACHINE_DOUBLE_BOOKED")


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #

def completion_drift(entries, g) -> list:
    """Every order whose replayed completion DATE disagrees with the solve.

    ``entries`` is the published plan (``engine.models.ScheduleEntry``), read the
    way every other surface reads it: an order's completion is the LATEST end
    across every lane it touches, OS / Off-machine milestones included. Deriving
    it from real machine ops only is how the delay report came to publish a date
    the Gantt disagreed with (2026-08-07), and a drift check with that blind spot
    would invent drift on any outsourced routing.

    ``days`` is SIGNED — replayed minus solved, positive meaning the published
    plan finishes LATER than the solve promised. The sign is the finding, not
    noise: late is the search over-promising, early means the decoder found
    capacity the solver withheld (2026-08-14: neutering the guard that keeps a
    pool escape restricted to the fallback op that earned it — ``decode.py``'s
    mutation D1 — let a solved order use it too, pulling it 1 day 13 h forward
    through night shifts the solve had deliberately left dark).

    Two things this deliberately does NOT report, because a banner that cries
    wolf teaches the directors to ignore the one row that is real:

      * a batch the genome never saw — an order uploaded since the solve has no
        solved date to disagree with;
      * a batch the genome DID see that is in no plan at all today. That is not a
        date disagreement, it is "an order in no plan", which has its own named
        check (``roster_engine.report.unplanned_order_violations``, which needs
        the batches) and its own carrier (``decode.Plan.dropped``). Reporting it
        here would fire on every order legitimately completed since the solve.
    """
    solved = ((g or {}).get("cp_completion") or {})
    if not solved:
        return []

    replayed: dict = {}
    for entry in entries or ():
        end = getattr(entry, "end", None)
        if end is None:
            continue
        key = str(getattr(entry, "batch_id", ""))
        if key not in replayed or end > replayed[key]:
            replayed[key] = end

    rows = []
    for key in sorted(replayed):                    # stable: two runs can be diffed
        want = _as_date(solved.get(key))
        if want is None:
            continue
        got = replayed[key].date()
        if got == want:
            continue                                # exact, no epsilon
        days = (got - want).days
        direction = "LATER" if days > 0 else "EARLIER"
        rows.append({
            "kind": KIND_DRIFT,
            "breach": True,           # a disagreement with the solve, not a measurement
            "ref": key,
            "batch_id": key,
            "solved": want.isoformat(),
            "replayed": got.isoformat(),
            "days": days,
            "message": (
                f"batch {key} was solved to finish {want.isoformat()} but the "
                f"published plan finishes {got.isoformat()} — {abs(days)} day(s) "
                f"{direction}. The plan on screen is not the plan that was "
                f"solved, so its late-days are not the ones the search "
                f"optimised."),
        })
    return rows


def _as_date(value):
    """``cp_completion`` stores an ISO date STRING (it survives the genome's JSON
    round trip). A ``date``/``datetime`` is accepted too, so a caller holding the
    solver's own completions can use this check without converting first."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None                                  # unreadable: say nothing


# --------------------------------------------------------------------------- #
# Staleness — the genome was solved against a book, and books move
# --------------------------------------------------------------------------- #

def book_signature(batches, masters) -> str:
    """Today's book, fingerprinted the way the genome fingerprinted the solved
    one — the SAME function, over ``domain.build_jobs``'s output.

    It must never be ``engine.optimize_service.book_signature``: that one hashes
    ``engine.models.SOLine`` fields (process_qty, lane, promise) and this one
    hashes ``cp_engine.domain.Job``/``Op`` fields. They are structurally
    different representations and can NEVER byte-match, so comparing across them
    would report every plan stale.

    Built from ``build_jobs``, not from a ``Built``: constructing a ``Built``
    needs pyjobshop, and this module has to import on the production server.
    """
    jobs, _by_key, _skipped = domain.build_jobs(batches or (), masters)
    return _book_signature(jobs)


def genome_stale(batches, masters, g) -> list:
    """One row when the genome was solved against a DIFFERENT book.

    Silent when the genome carries no signature (one written before the key
    existed, or an empty genome): a check that cannot compare must say nothing
    rather than accuse — a report may never attribute a cause it did not CHECK
    (2026-08-09).
    """
    solved_sig = (g or {}).get("cp_solved_book_sig")
    if not solved_sig or not batches:
        return []
    if book_signature(batches, masters) == solved_sig:
        return []
    return [{
        "kind": KIND_STALE,
        "breach": True,            # the genome no longer describes today's book
        "ref": "cp_solved_book_sig",
        "message": (
            "the stored CP plan was solved against a DIFFERENT order book — "
            "orders, quantities, delivery dates or routings have moved since. "
            "The dates below are a replay of the old decisions on today's book; "
            "run a fresh search to plan the book you actually have."),
    }]


# --------------------------------------------------------------------------- #
# Everything, in one list
# --------------------------------------------------------------------------- #

def _tagged(rows, breach) -> list:
    """Stamp ``breach`` onto every row from an external check.

    The four ``roster_engine.report`` functions are reused UNCHANGED (that is
    the point — see the module docstring) and know nothing of this module's
    vocabulary, so the tag is applied here, once, rather than trusted to be
    remembered at every call site downstream."""
    for row in rows:
        row["breach"] = breach
    return rows


def all_violations(entries, masters, config, batches=None, genome=None,
                   absent=None) -> list:
    """The four ``roster_engine.report`` checks plus drift, in a fixed order —
    identical inputs give an identical list, so two plans can be diffed.

    Non-blocking by construction: it returns rows, it never raises on a plan it
    dislikes. A live plan must never break because a self-check is unhappy.

    EVERY ROW CARRIES A ``breach`` BOOLEAN — put on the row itself, not left to
    an out-of-band lookup against ``RULE_KINDS``, so a downstream consumer (the
    validation banner Tasks 11/12 build over this) cannot partition breaches
    from measurements wrongly by omission: ``row["breach"]`` is the one thing to
    read, always present, never optional.

    ``IDLE_CAPACITY`` IS THE ONLY ROW WITH ``breach: False``. The other kinds —
    the three ``RULE_KINDS`` plus drift and staleness — all answer "did the plan
    break a rule, or disagree with the solve?" and must read 0. Idle capacity
    answers "what did this plan leave on the table?" — a dark machine with ready
    work and a free qualified operator. Under this engine the roster's job is to
    make that small, but it is legitimately non-zero: E1 forbids an operation
    spanning an unmanned shift (the owner-authorized Rule 2 trim), and the
    decoder defers a pool-staffed machine's fallback work by one shift on
    purpose. So it is reported as a measurement and must not be asserted to
    zero. ``absent`` is passed through for the same reason the check takes it:
    without leave data a person on holiday reads as spare capacity and the row
    accuses the plan of wasting a machine nobody could have run.

    ``batches`` (today's book, Rule 1's output) turns the staleness check on. It
    matters for more than one extra row: once the book has MOVED, drift is
    EXPECTED and fully explained, so the honest report is ONE "this genome was
    solved against a different book" row rather than one alarm per order. With no
    ``batches`` the staleness question cannot be answered, so drift is reported
    as-is — loud is the safe direction when the cause is unknown.

    "An order in no plan at all" is deliberately not here: it needs the batches to
    mean anything, ``decode.Plan.dropped`` already carries it out of the decoder,
    and ``roster_engine.report.unplanned_order_violations`` is the check that
    names it. One condition, one row, one owner.
    """
    entries = list(entries or ())
    if not entries:
        return []
    rows = []
    rows.extend(_tagged(rules.operator_split_violations(entries, config, masters),
                        True))
    rows.extend(_tagged(rules.segmentation_violations(entries), True))
    rows.extend(_tagged(rules.machine_conflict_violations(entries), True))
    rows.extend(_tagged(rules.idle_capacity_violations(entries, masters, config,
                                                        absent=absent), False))
    stale = genome_stale(batches, masters, genome)
    rows.extend(stale)
    if not stale:
        rows.extend(completion_drift(entries, genome))
    return rows
