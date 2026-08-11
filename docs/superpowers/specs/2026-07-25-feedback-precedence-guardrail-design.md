# Feedback precedence guardrail — design spec (2026-07-25)

## Problem

Per-process feedback (`Capture Actuals` / `Daily Entry`) records good qty **per
process, independently** (`orderbook.completed_by_process`). Nothing enforces the
physical fact that pieces flow through the routing **in order**: a piece at *VMC first
side* must first have cleared *CNC first side*. Two consequences:

1. **Out-of-order punching is possible.** You can record *VMC first side* complete
   while *CNC first side* has nothing recorded — a physically impossible state.
2. **Downstream can exceed upstream.** If 20 pieces cleared CNC, at most 20 can be fed
   to VMC — but today you can record 40 at VMC.

Both let the order book hold a state that can't happen on the floor, and (2) also
causes the planner to **re-schedule work that was already done** (an un-recorded
upstream step looks unfinished, so it is planned again).

## The rule (one invariant covers both asks)

> For each order (`SO#`, `item`), the **quantity recorded at any process can never
> exceed the good quantity that cleared the process immediately before it** in the
> routing. The **first** process is capped at the order's **ordered qty**.

- "Quantity recorded at a process" = **cumulative `qty_produced`** at that process
  (pieces the step *consumed*, whether they end good or rejected — you cannot feed a
  step more pieces than arrived).
- "Good qty that cleared a process" = **cumulative `qty_produced − qty_rejected`**,
  clamped ≥ 0 (the existing `completed_by_process` definition).

This single invariant delivers both requirements:
- **Order enforcement:** predecessor good = 0 ⇒ successor capped at 0 ⇒ you must punch
  CNC before VMC.
- **The cap:** predecessor good = 20 ⇒ successor capped at 20.
- **Side benefit:** downstream-recorded ≤ upstream-recorded always holds, so the
  planner never re-schedules already-done upstream work.

## Behaviour

- **Hard block.** A `POST /actuals` punch that would break the invariant is **rejected
  with HTTP 400** and never recorded (owner decision: "a guard layer in order to NOT do
  that"). No "warn-and-allow".
- **Message names the blocker**, e.g.
  *"Can't record 40 at 'VMC FIRST SIDE' — only 20 pieces have cleared the previous step
  'CNC FIRST SIDE'. Record 'CNC FIRST SIDE' first."*
- **Rollback is guarded too.** `POST /actuals/rollback` is rejected if removing the
  entry would drop an upstream process below what a downstream process already recorded
  (you can't retro-create the illegal state either), e.g.
  *"Can't roll back this 'CNC FIRST SIDE' entry — 20 pieces are already recorded at the
  later step 'VMC FIRST SIDE'. Roll back 'VMC FIRST SIDE' first."*

## Edge cases & decisions

| Case | Decision |
|---|---|
| **Cap basis** | On `qty_produced` (throughput), not good — VMC can't *process* more pieces than CNC delivered good. (Owner-confirmed.) |
| **All step boundaries** | The chain covers **every** consecutive process pair, incl. OS/outsourced and manual/inspection steps — not just CNC→VMC. (Owner-confirmed.) A consequence: an **OS or inspection step that is never punched caps everything after it at 0** — i.e. those steps must be recorded too. Flagged as expected behaviour; revisit only if the floor doesn't punch OS/inspection. |
| **Pre-existing (legacy) violations** | The capture guard checks **only the punched process** against its predecessor, so a legacy over-count at some *other* process never blocks an unrelated punch. It just can't be made worse. |
| **No routing for the item** (`NO_ROUTING`) | Allow the punch (return no error) — consistent with the forgiving-loader principle; such an order isn't scheduled anyway. |
| **Punched process not in the routing** (free-text / stale name) | Allow (can't validate an unknown step). The UI dropdown is routing-driven, so this is a safety fallthrough, not the normal path. |
| **Duplicate process names in a routing** (known data quirk) | Match by normalized name; the first matching seq governs. Documented quirk, out of scope to "fix" here. |
| **Downtime-only / net-zero punches** | `qty_produced = 0` never increases any cumulative → never blocked. |
| **DISPATCH** (terminal 0-qty milestone) | Terminal, so it never gates a successor. As a *successor* it is capped at the last real step's good (shipping ≤ what's finished). |

## Pure core (no I/O — fully unit-testable)

New in **`engine/orderbook.py`** (the pure order-book layer; never touches the store):

```python
def precedence_cap_error(actuals, so_no, item_code, punched_process,
                         routing, ordered_qty) -> str | None:
    """Return an error message if the CUMULATIVE produced at `punched_process`
    (already reflected in `actuals`) exceeds the good qty that cleared the process
    immediately before it in `routing` (or `ordered_qty` for the first process).
    None = allowed. Pure; `actuals` is the full list (filtered by key internally)."""

def rollback_cap_error(actuals_after_removal, removed, routing) -> str | None:
    """Return an error message if, after removing `removed`, the good qty at the
    removed entry's process would drop below the produced qty already recorded at
    the immediately-following process. None = allowed. Pure."""
```

Both reuse the existing `completed_by_process` accounting semantics (sum of
produced / good per `(so, item, normalized-process)`), sorted by `Process.seq`.

## WIRING MAP — every connection point (the "bug-free in future" contract)

This is the authoritative list of everything the guardrail touches or depends on. A
future change to any *Depends-on* row must keep the guardrail's assumption true; a
change to any *Wires-into* row must keep calling the validator.

### A. Data the validator reads (Depends-on)
| Source | What | If it changes… |
|---|---|---|
| `engine/models.py::Actual` | `so_no, item_code, process, qty_produced, qty_rejected` | Renaming/removing any of these breaks the validator. Validator reads only these fields. |
| `engine/models.py::Process` | `seq`, `name` — the routing order | The invariant IS the seq order. If routings gain branching/parallel seqs, the "immediate predecessor" rule needs revisiting. |
| `engine/models.py::Routing.processes` | ordered step list per item | Source of the chain. |
| `engine/orderbook.py::_norm` / `completed_by_process` | process-name normalization + cumulative accounting | Validator MUST use the same normalization & good/produced accounting as planning, or capture and planning disagree. Single source of truth. |
| order's `ordered_qty` (from `Order`) | first-process cap | Passed in by the API from `book_store.load_active_orders()`. |

### B. Where the validator is called (Wires-into)
| Call site | File | Rule |
|---|---|---|
| **Capture** | `api/main.py::post_actuals` (`POST /actuals`) — after operator validation, **before** `r7.run(actual)` (which appends to the store) | Build the hypothetical actuals list (`existing + new`), call `precedence_cap_error`; on non-None → `HTTPException(400)`. The punch is never stored. |
| **Rollback** | `api/main.py::rollback_actual` (`POST /actuals/rollback`) — after locating `target`, **before** `book_store.delete_actual` | Call `rollback_cap_error(actuals_without_target, target, routing)`; on non-None → `HTTPException(400)`. Nothing is removed. |
| **Masters/routing lookup** | both call sites use `_current_masters().routings.get(item_code)` | Same masters the planner uses (operator-overlaid). |

### C. Consumers that MUST stay consistent (the invariant they now get for free)
These read per-process progress; after the guardrail they can assume
downstream-recorded ≤ upstream-recorded. None need code changes, but a future change
must not reintroduce the illegal state behind their back:
| Consumer | File | Relies on the invariant for… |
|---|---|---|
| `active_so_lines` → `process_qty` | `engine/orderbook.py:207` | correct per-step remaining; no phantom re-scheduling of done upstream steps. |
| `_plan` | `api/main.py:697` | the plan the floor sees. |
| optimize (`prepare_contest`) | `engine/optimize_service.py:210` | the search plans the same remaining. |
| commit-preview / report | `api/main.py:1045`, `2137` | expected dates. |
| Rule 7 progress tab | `api/main.py:635` via `process_progress_rows` | the on-screen per-process table. |

### D. UI (Wires-into, display only)
| Element | File | Behaviour |
|---|---|---|
| Capture form process dropdown | `web/app.js` `#a-process`, populated from `/items` routing processes (`fillItemMeta`, ~1465) | unchanged — already routing-driven, so the punched process is always a real routing step. |
| Submit error path | `web/app.js` actuals submit → shows the endpoint's 400 detail (same path today's "operator required"/"unknown operator" 400s use) | The guardrail's 400 message surfaces automatically. **No new UI code required**; a future refactor of the actuals-submit error handling must keep showing the 400 `detail`. |

### E. Cache / fingerprint (no change, documented)
- A **rejected** punch is never stored ⇒ no `book_sig` / `_plan_fingerprint` /
  actuals-digest change. Correct by construction.
- An **accepted** punch already invalidates the plan cache via the existing actuals
  channel (`_current_book_sig` + the actuals digest in `_plan_fingerprint`). The
  guardrail adds nothing here.

### F. Tests / regression wiring
| Test | File |
|---|---|
| Pure validator unit tests (order-enforce, cap, first-process cap, no-routing, net-zero, rollback) | `tests/test_orderbook.py` |
| Endpoint 400 tests (capture + rollback) | `tests/test_actuals_api.py` (or the existing actuals API test module) |
| Permanent dogfood invariant: "no order ever holds downstream-recorded > upstream-recorded" | the QA harness → promote to `tests/test_feedback_precedence.py` |

## Out of scope
- Changing planning to *infer* upstream completion from downstream (the guardrail makes
  that unnecessary — the illegal state can't exist).
- Branching/parallel routings (current routings are strictly sequential by `seq`).
- Auto-capping the UI input to the max-allowed (server 400 is the guard; a UI hint can
  come later).
