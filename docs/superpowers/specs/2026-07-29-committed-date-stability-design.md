# Committed-order date stability (+3-day promise cap)

- **Date:** 2026-07-29
- **Status:** Design — approved in brainstorming, pending spec review
- **Author:** owner + Claude
- **Builds on:** the freeze feature (`docs/superpowers/specs/2026-07-29-freeze-in-progress-restricted-optimize-design.md`)
- **Related code:** `engine/optimizer.py` (worst-order ceiling — the pattern to reuse),
  `ppc_engine/objective/`, `engine/models.py` (`commitment`/`promised_date`),
  `engine/orderbook.py` (`set_commitment`, lanes), `api/main.py`
  (`/orders/commit`, `/orders/urgent`, `_start_optimize`, `_incumbent_metrics`,
  `_auto_apply_result`), `web/` (Orders tab lanes).

---

## 1. Why (context / problem)

Anvitech runs a **rolling order book**. Week 1: orders arrive, get optimized, the owner
tells customers a completion date and **commits** them; the floor works the plan. Week 2:
new orders arrive (Open). When the owner re-optimizes to fit the new orders in, the
already-committed orders' expected completion dates can **drift later** — one drifted **+19
days** in the freeze test. That breaks the promise the customer was given.

The owner's rule: **a committed order's expected date may move *earlier* by any amount, but
must not slip *later* by more than +3 days** from what the customer was told. New (open)
orders fill the gaps around committed work and run in earnest after committed work clears.

A hard version of this (the July 13 two-pass: committed reserves machines, open backfills;
plus a hard promise veto) was built and **measured ~30% worse on late-days**, then removed
(the 2026-07-16 Phase-2R pivot: "lanes are status labels, no scheduling effect"). This
design brings promise protection back **soft**, reusing the proven 2026-07-24 worst-order
ceiling machinery, so it does not repeat that collapse.

**Relationship to freeze:** the freeze pins *physically in-progress* operations (machine +
operator). This feature protects the *committed order's overall completion date* against
re-optimization churn. Two complementary layers, one goal (protect the plan the floor is
executing + give customers honest, stable dates). Both are active together.

## 2. Goal / non-goals

**Goal:** committed orders' expected completion never slips more than **+3 days** past their
promised date as a result of re-optimizing (adding/optimizing open orders); open orders
absorb the slack and wait. Remove the Urgent lane. Keep the manual open→committed lifecycle.

**Non-goals:**
- No rigid two-pass / machine reservation for committed orders (that is the July collapse).
- No change to the freeze feature.
- No auto-commit — committing stays a manual owner action.
- Not a guarantee against *physical* delay (slow floor, breakdown): that is flagged, not
  prevented (see §5).

## 3. Locked decisions (from brainstorming)

| # | Decision |
|---|----------|
| 1 | **Conflict rule:** committed orders are protected; open orders wait. Open takes only the gaps that don't push any committed order past promise+3; otherwise open runs later. |
| 2 | **Physical slip:** if a committed order is already heading past +3 for physical reasons (not re-optimization), it is **flagged red ("past promise") and given priority to minimize the breach** — the cap governs the optimizer's *choices*, not physical reality. |
| 3 | **Commit trigger:** **manual** — orders stay Open until the owner clicks Commit; commit snapshots the promised date (= current expected completion) and sets the +3 anchor. Unchanged from today. |
| 4 | **Anchor:** each committed order's ceiling = **its `promised_date` + slack** (slack default **3 days**). `promised_date` is snapshotted at commit and does not float. |
| 5 | **Remove Urgent:** full removal — endpoint, lane, button, code path. Orders tab = **Committed + Open** only. Existing urgent orders **migrate to Committed**. |
| 6 | **+3 is a config knob** (`committed_promise_slack_days`, default 3). |
| 7 | **Efficiency cost is accepted and measured:** protecting committed orders costs some overall throughput (open finishes later). Measured on Test8 before shipping; no hidden regression. |

## 4. The mechanism — per-committed-order promise ceiling (soft-in-search + hard-at-apply)

Reuse the **worst-order ceiling** pattern (2026-07-24), which is a convex penalty in the
objective plus a hard no-regression backstop at apply. It is proven on the real book and did
not collapse.

### 4a. Metric (pure, in `engine/optimizer.py` `plan_metrics` + the ppc objective mirror)
For each order the plan schedules, compute its **expected completion** (already available).
For each **committed** order:
- `promise_ceiling_days` = (`promised_date` − `plan_start`) in working/calendar days **+
  `committed_promise_slack_days`** (default 3).
- `promise_slip` = max(0, `expected_completion_days` − `promise_ceiling_days`).

New metric fields (mirroring the existing `ceiling_breach` / `max_late_days`):
- `committed_promise_breach` = Σ over committed orders of `promise_slip²` (convex; the
  search-guiding penalty).
- `max_committed_slip` = max over committed orders of (`expected_completion_days` −
  (`promised_date` − plan_start) days) — the worst committed order's slip vs its promise, in
  days (the scalar the apply backstop gates on).

Open orders contribute nothing to these (no ceiling → they absorb the slack).

### 4b. Objective (soft, in-search)
`score` gains a term `COMMITTED_PROMISE_WEIGHT × committed_promise_breach`, exactly like the
existing `CEILING_WEIGHT × ceiling_breach`. Weight is **measured on Test8** (as
`ceiling_weight=100` was), high enough that the optimizer keeps committed orders within +3
when feasible and delays **open** orders instead — this is how "committed protected, open
waits" emerges without any reservation. Mirror the same term in `ppc_engine/objective` so the
new-engine search sees it too (the worst-order ceiling already does this — follow it exactly).

### 4c. Apply backstop (hard, no-regression)
Mirror the worst-order backstop in `api/main.py` `_auto_apply_result` (`worst_ok = best.max_late_days
<= inc.max_late_days`): add `promise_ok = best.max_committed_slip <= inc.max_committed_slip`.
A re-optimized plan is applied only if `promise_ok` **and** `worst_ok` **and** it scores
better. So re-optimizing can **never** increase the worst committed order's slip past its
promise — "committed dates don't change when I optimize." The manual `/optimize/apply` path
gets the same gate.

### 4d. Physical-slip handling (decision #2)
- The convex penalty naturally **prioritizes** a committed order that is slipping (its `slip²`
  dominates), pulling it earlier to minimize the breach.
- A committed order whose `expected_completion > promised_date + slack` is surfaced **red
  ("past promise, +Nd")** on the Orders tab (extend the existing Promised-vs-Current drift
  flag). This is informational; the plan is still the best achievable.

## 5. Remove Urgent (decision #5)

- **API:** delete `POST /orders/urgent`; remove the `"urgent"` branch from `set_commitment`
  and any urgent-specific handling. `commitment` becomes `open | committed` only.
- **Migration:** on load, any stored order with `commitment == "urgent"` is treated as
  `"committed"` (one-time normalization in `book_store`/`orderbook` load path; keep its
  `promised_date`). Urgent was the more-protected lane, so committed is the safe mapping.
- **UI:** remove the Urgent button and lane badge; Orders tab shows **Committed** and **Open**
  only. Remove "Mark Urgent" from the toolbar.
- **Docs:** update `CLAUDE.md`/`RULES.md` — the three-lane model becomes two lanes.

## 6. Lifecycle + the "lanes now affect scheduling" reversal

- Open = new orders (no ceiling, pure label). Committed = owner-promised (has a **promise
  ceiling** → now DOES affect scheduling). This is a **deliberate, partial reversal** of the
  Phase-2R "lanes are status labels, no scheduling effect" statement: **committed** now has a
  scheduling effect (the soft ceiling + apply backstop); **open** remains a pure label. Update
  `CLAUDE.md`/`RULES.md` to say so precisely (committed = soft-protected, open = label).
- Commit stays manual (`/orders/commit`, snapshots `promised_date`). Uncommit clears the
  ceiling. Rolling: as the owner commits week-2 orders, they too gain a ceiling from their own
  promise; a newly-committed order cannot push an earlier-committed one past +3 (each has its
  own ceiling; the search + backstop protect all committed orders).

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Committed order with no `promised_date` (legacy) | No ceiling (treated like open) until re-committed; report it so the owner can re-commit. |
| Committed order physically past promise+3 | Red flag + prioritized (decision #2); backstop still prevents making it *worse* on re-optimize. |
| All-open book (nothing committed) | `committed_promise_breach = 0`, `max_committed_slip = 0` → byte-identical to today (no behavior change). |
| Existing urgent orders | Migrated to committed (§5). |
| slack = 0 in config | Ceiling = promise exactly; valid. |
| Freeze + committed ceiling together | Both active; frozen in-progress work is pinned, committed completion protected. No conflict (freeze constrains placement, ceiling penalizes lateness). |

## 8. Config

- `Config.committed_promise_slack_days: int = 3` (validated ≥ 0). Surfaced as a Settings
  value (like other knobs). Folded into `_inputs_signature` (it changes plan shaping).
- `COMMITTED_PROMISE_WEIGHT` in `engine/optimizer.py` + the ppc mirror — a measured constant
  (locked after a Test8 sweep, like `ceiling_weight=100`), not user-facing.

## 9. Testing

- **Unit (pure):** `plan_metrics` computes `committed_promise_breach` / `max_committed_slip`
  correctly (committed within +3 → 0; +5 → slip 2, breach 4; open ignored); all-open →
  byte-identical to today; slack config respected.
- **Search:** given a book where an open and a committed order contend, the optimizer delays
  the **open** order to keep the committed within +3 (the penalty works).
- **Apply backstop:** a plan that scores better but increases `max_committed_slip` is
  **rejected** (mirror `test_worst_order_backstop`).
- **Urgent removal:** `/orders/urgent` gone (404/405); a stored urgent order loads as
  committed; Orders tab renders two lanes.
- **Byte-identical:** all-open / no-committed book plans identically to pre-feature (golden
  guard).
- **Real-data (Test8):** measure the efficiency cost — commit a set of orders, add new open
  orders, re-optimize; confirm no committed order slips > +3 (where feasible), report the
  late-days delta vs unconstrained. **Gate: if the cost is unacceptable, retune the weight or
  revisit before shipping.**

## 10. Map of code changes

| Area | File | Change |
|---|---|---|
| Metric | `engine/optimizer.py` | `plan_metrics`: `committed_promise_breach`, `max_committed_slip`; `score`: add weighted term |
| Metric mirror | `ppc_engine/objective/` | same term in the new-engine objective |
| Config | `engine/config.py` | `committed_promise_slack_days=3` + validation; fold into `_inputs_signature` |
| Apply gate | `api/main.py` | `_auto_apply_result` + `/optimize/apply`: `promise_ok` backstop; `_incumbent_metrics`/`_metrics_for_ranks` carry the new metric |
| Commit | `engine/orderbook.py`, `engine/book_store.py`, `api/main.py` | remove urgent branch; committed ceiling reads `promised_date` |
| Urgent removal | `api/main.py`, `web/` | delete `/orders/urgent`, urgent UI; migrate urgent→committed on load |
| Docs | `CLAUDE.md`, `RULES.md` | two lanes; committed = soft-protected; promise ceiling rule |

## 11. Open items for the implementation plan
1. **Weight tuning** (`COMMITTED_PROMISE_WEIGHT`) — sweep on Test8; lock the value like
   `ceiling_weight`.
2. **Days basis** for `promise_ceiling_days` — confirm working-day vs calendar-day arithmetic
   matches how `expected_completion`/`late_days` are already computed (be consistent with the
   existing lateness metric).
3. **Backstop granularity** — start with the scalar `max_committed_slip` no-regression (mirrors
   worst-order); evaluate during TDD whether a per-committed-order check is needed.
