# Anvitech PPC Engine — Persistent Order Book Design

**Date:** 2026-06-22
**Status:** Approved (brainstorming)
**Related:** [`RULES.md`](../../../RULES.md), [original engine spec](2026-06-19-anvitech-ppc-engine-design.md)

---

## 1. Problem

The engine is currently **stateless**: each Excel upload is parsed, scheduled, and
forgotten (tracked only by a per-tab `dataset_id`). Real shop use is **cumulative**
— orders arrive over time, get produced over days, and the system must remember
them. Without that, uploading a second file (new orders) or re-uploading the same
file breaks the picture: there is no notion of which orders are new vs in-progress,
and re-uploads double-count.

This design adds a **persistent, shared Order Book** that uploads feed into, with a
clear per-order lifecycle and duplicate detection — without changing the tested
Rules 1–7.

## 2. Architecture (Approach A — order book as a layer above the rules)

```
Upload Excel ─▶ MERGE into Order Book ─┐
                                        ├─▶ Order Book (durable, shared)  ──▶ "Plan" ──▶ Rules 1–7 (unchanged) ──▶ Schedule + Gantt
Rule 8 entry ─▶ recorded vs SO#  ───────┘        (orders · masters · actuals)
   (incl. "complete" flag)
```

- The **Order Book** owns all state; Rules 1–7 stay pure and untouched.
- On **Plan**, the book emits active SO-lines (each with its remaining qty) and
  feeds the existing rules.
- The per-tab `dataset_id` model is **removed**. There is **one shared book** that
  persists, so "log in tomorrow, everything's as I left it" works for all users.

## 3. Data model

**Order** — one per **(SO number, item code)** pair, which is the unique key. An SO
number is **not** unique on its own: in production one SO number can carry several
item lines, and each line is tracked as its own order. Every keyed structure
(orders hash, good-by-order, orders-with-actuals, per-process progress) uses the
pair, never the SO number alone.

| field | meaning |
|---|---|
| `so_no` + `item_code` | **the unique key (the pair)** — exposed as `Order.key` |
| `item_name` | item |
| `ordered_qty` | quantity ordered |
| `delivery_date` | SO delivery date |
| `completed` | bool — set ONLY when the user denotes complete via Rule 8 |
| `first_seen` | when/which upload added it |

**Status is derived** (never hand-managed, to avoid state-sync bugs):
- `completed == true` → **COMPLETE**
- else has ≥1 actual for this **(SO#, item)** → **RUNNING**
- else → **PENDING**
- `produced_good` = Σ good qty (produced − rejected) from actuals for the (SO#, item)
- `remaining` = `ordered_qty − produced_good`

**Persistence (durable storage layer, already built — Upstash in prod / local in dev):**
- active orders (keyed by the (SO#, item) pair — encoded as a single composite hash
  field), `completed` archive, actuals (exists), latest masters workbook.

## 4. Upload → merge flow

1. Parse the workbook (existing loader). **Parse fails → HTTP 400, book unchanged**
   (atomic — never half-merge).
2. **Masters:** if the workbook has non-empty masters sheets → replace (latest-wins);
   if masters are missing/empty → **keep existing** (an orders-only file never wipes
   the shop setup). Masters rarely change in practice.
3. **Each SO row, by SO number:**
   - **unseen** → add as **Pending**.
   - **already active** → **flag as repeat** (identical or changed); the original is
     **not modified** (revisions are deferred — see §10).
   - **in the completed archive** → **flag "already completed"**; do not re-add
     (prevents re-doing finished work).
4. Return a **merge summary**: *N added (pending)* and *M flagged* (each flag lists
   SO# + reason: identical / changed / already-completed).

Result: re-uploading the same file flags everything and adds nothing — no
double-counting.

## 5. Lifecycle

```
PENDING ──(first actual recorded)──▶ RUNNING ──(user marks complete in Rule 8)──▶ COMPLETE → archive
```

- **PENDING → RUNNING:** automatic, when the first Rule 8 actual is saved for the SO#.
- **RUNNING → COMPLETE:** **only** when the user denotes it complete on the Rule 8
  daily-production entry (a "Mark order complete" flag). **The engine NEVER assumes
  completion** — not even when `remaining ≤ 0`. When `remaining ≤ 0`, the Orders view
  shows a non-binding **"ready to complete"** hint.
- COMPLETE orders move to the archive and leave active planning (kept for history/KPIs;
  not hard-deleted).

## 6. Scheduling ("Plan" unifies the old Run + Rerun-MRP)

1. The book emits **active** orders (not completed), each as an SO-line with
   `qty = ordered − produced_good`. (Pending → full qty falls out of the same formula
   since produced_good = 0.)
2. Orders with `remaining ≤ 0` are **excluded from machine scheduling** (nothing left
   to make) but remain listed as Running.
3. The active SO-lines feed **Rules 1–7 unchanged** → schedule + Gantt.
4. Schedule/Gantt rows carry a **Pending/Running** label derived from their source SO#s.

"Run plan" and "Rerun MRP" become a single **Plan** action — both just plan the current
book by each order's remaining qty.

## 7. Persistence & concurrency

- Book, masters, and actuals all live in the durable store → survive logout, browser
  close, and host restart/sleep. On login + Plan, yesterday's state loads exactly.
- **Shared** across all users (one book), replacing the per-tab dataset model.
- **Concurrency (append-safe):** orders are keyed by the (SO#, item) pair (different
  orders never clash — including two item lines that share an SO number);
  actuals are appended as individual entries rather than rewriting one blob — so two
  workers saving at once don't overwrite each other. (Single-process local dev is
  unaffected.)

## 8. UI changes

- **Orders tab (new):** read-only dashboard — each SO# with derived status
  (Pending/Running/Complete), ordered / produced / remaining, delivery date, and the
  "ready to complete" hint. (No mark-complete button here — completion is via Rule 8.)
- **Upload** → "Upload orders (Excel)" → shows the merge summary (added / flagged).
- **Rule 8 daily entry** gains a **"Mark order complete"** checkbox; saving it with the
  entry sets that SO#'s `completed` flag.
- **One "Plan" button** (Run + Rerun merged); the per-rule debug tabs and Gantt remain.

## 9. Edge-case catalog

| Case | Behavior |
|---|---|
| Re-upload same file | all flagged, 0 added |
| New-orders file | new (SO#, item) lines → pending; overlaps flagged |
| Same SO#, different item | two distinct orders (SO# alone is not unique) |
| Completed (SO#, item) reappears | flagged "already completed", not re-added |
| Orders-only file (no masters) | existing masters kept |
| New item in upload | uses uploaded routing; none → NO_ROUTING flag (existing) |
| Fully produced, not marked complete | stays Running, excluded from scheduling, "ready to complete" hint |
| Rejected qty | stays in `remaining` → re-planned (existing) |
| User marks complete with qty remaining | allowed (user's call) with an "X of Y produced — sure?" confirm |
| Corrupt/unparseable upload | HTTP 400, book untouched |
| Empty book (fresh) | "No orders — upload to begin" |
| Two users act at once | orders keyed by (SO#, item); actuals append-safe |

## 10. Scope

**v1 (this build):** persistent shared order book; upload-merge with repeat /
already-completed flags; Pending/Running derivation; completion via the Rule 8 flag →
archive; unified Plan; Orders tab; append-safe persistence; Pending/Running labels on
the schedule & Gantt.

**Deferred (explicitly, per the user):**
- Applying **revisions** to existing orders (changed qty/date) — for now they are
  **flagged only**, original kept.
- Explicit **cancel** action.
- **Hard-delete** of completed orders (archive is retained).

## 11. Testing

- **Merge:** unseen → added pending; known → flag repeat; completed → flag; masters
  latest-wins; masters kept when omitted; re-upload same file → 0 added.
- **Lifecycle:** pending → running on first actual; mark-complete via Rule 8 → archived
  & excluded from planning; engine never auto-completes at `remaining ≤ 0`.
- **Plan:** active planned by remaining qty; completed excluded; pending = full qty;
  Pending/Running labels correct.
- **Persistence:** book + actuals survive a store round-trip.
- **Concurrency:** two appended actuals both survive (no overwrite).
