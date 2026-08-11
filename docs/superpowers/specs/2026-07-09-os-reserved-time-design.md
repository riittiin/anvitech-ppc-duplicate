# Design — OS (outsourcing) steps reserve their turnaround time

**Date:** 2026-07-09
**Status:** approved (owner), ready to plan
**Branch:** `os-reserved-time`

## Context

The shop is moving from the test file `Test4.xlsx` to a real production file
`Production1.xlsx`. `Production1.xlsx` holds two sheets — the **SO list** and the
**item process master** — laid out exactly like their Test4 equivalents (verified:
same column positions, same header names). The owner will **copy Production1's item
process master and SO list into `Test4.xlsx`'s existing named sheets** and upload
that. So the uploaded workbook stays a normal Test4-format file (proper sheet names,
all three master sheets — Machine / Operator & shift / Weekly off & holiday — present).

**Consequence:** the loader needs **no** sheet-name or master changes. The only new
thing in the data is how the item process master expresses **OS (outsourcing)**
steps. That is the entire scope of this change.

### The OS pattern in the real data (`Production1.xlsx`, 101 items, 63 OS steps)

An outsourced step is written as a process whose **Allotted M/c = `OS`** (60/63) or
whose **name ends in an `OS` word** (e.g. `CNC OS`, `BANDSAW OS`, `ROUGH MACHINING
OS`, `Polish OS`). Its **cycle-time column holds the outsource turnaround in minutes**
— values like `1440, 4320, 7200, 8640` (mostly clean multiples of 1440 = whole days).
Suggested M/c is always blank. Four OS steps currently have a **blank** cycle time
(data the owner will fill in later).

The data also contains **`DISAPTCH`** — a transposed misspelling of `DISPATCH` — in
several rows.

### Why today's behaviour is wrong for this

Rule 6 currently treats an off-machine step as "**no machine AND no cycle time**" and
schedules it as a **zero-duration** milestone. Two problems for OS:

1. An OS step *has* a cycle time and Allotted = `OS`, so today it would be read as a
   real machine named `OS` and scheduled as `cycle × qty + setup` on a phantom
   station — wrong on every count.
2. Outsourcing genuinely consumes calendar time (the parts are away at a vendor for
   days). That time must be **reserved**, not zeroed.

## Decisions (confirmed with the owner)

| Question | Decision |
|---|---|
| OS block size | **Flat per batch** = the cycle-time value, regardless of qty. Not ×qty; no 90-min setup. |
| OS time basis | **Continuous 24×7 wall-clock** — ignores Anvitech's Thursday-off and shift hours (the vendor runs on its own clock). 7200 min = exactly 5 calendar days. |
| OS capacity | **Unlimited / parallel** — OS is not a constraining resource; any number of orders can be at OS at once. Not added to `machine_free`. |
| Operator | **None** (blank). |
| Successor timing | Next process **waits for the full OS block to finish** (sequential; no Rule 5 overlap — the step is off-site). |
| Blank-cycle OS | **Zero-duration pass-through** now; the moment a number is entered it reserves that block — no code change. |
| Early arrival | Handled by the existing feedback loop: the owner punches the OS step done in Capture Actuals → its per-process remaining hits 0 → the next Plan skips it. No new code. |
| Dispatch misspelling | A **robust matcher** treats `DISPATCH` / `Dispatch` / `DISAPTCH` as the dispatch gate. |

## Design

All changes are **additive**: with no OS/`DISAPTCH` steps present (the generated test
sample, the golden trace), behaviour is byte-identical to today.

### 1. OS detection (`engine/rules/rule6_allocate.py`)

New helper `_is_os(proc)`:

```
"OS" is a candidate of the Allotted or Suggested cell
  OR ( the step has NO real machine assigned  AND  "OS" is a whitespace token of the name )
```

Keyed on the **machine cell** (`Allotted`/`Suggested` = `OS`), with the name only
counting when no real machine is assigned. This is deliberate: the existing test
sample (`tests/sample_workbook.py`) has a step literally named `"CNC OS"` that is a
*normal in-house* step with machine `CNC1/CNC2` — a name-only rule would wrongly flip
it to an OS block and break the golden trace. In the real data every OS step that has
a cycle time carries `Allotted = OS`; the name-only cases are all blank-cycle (they
fall through to a zero-duration OS milestone anyway). Checked **before** machine
resolution, so the sentinel `OS` never flows into `_resolve_candidates`. Reuses
`parse_resource_candidates` (machine cell) and `normalize_process_name` (name tokens).

### 2. Scheduling an OS step (`rule6_allocate.py`, in the allocation loop)

When advancing a batch, before the "needs a machine" path:

- **`_is_os(proc)` and cycle-time > 0** → emit **one** `ScheduleEntry`:
  - `machine` = the `"OS / Outsourced"` lane (existing `_offmachine_lane` already
    returns this for an OS name; extend it so an Allotted-`OS` step with a non-OS name
    also lands there),
  - `start = s["ready"]`, `end = s["ready"] + timedelta(minutes=cycle_time)` — **raw
    wall-clock**, *not* via `WorkClock` (continuous 24×7),
  - `qty = _qty_for(batch, proc)`, `occupancy_min = cycle_time` (flat; no ×qty, no setup),
  - `operator = ""`.
  - It is **not** written to `machine_free` (unlimited parallel).
  - The batch's `ready` for the next process = the OS `end` (sequential, no overlap).
- **`_is_os(proc)` and no cycle-time** → today's **zero-duration** milestone on the OS
  lane (start = end = `ready`), reported as "OS step missing its cycle time".
- **DISPATCH / other no-machine-no-time steps** (not OS) → unchanged zero-duration
  milestone.
- **per-process remaining 0** → skipped (unchanged "continue from reality").

`_is_offmachine` stays for the DISPATCH/no-time case; the OS-with-time case is handled
by the new branch above it.

### 3. `OS` is never a machine (`engine/loaders.py`)

In `_validate`, skip registering a provisional machine when the candidate normalizes
to `OS` (it is a sentinel, not a resource) — no bogus `PENDING_MASTER_DATA` for `OS`.

### 4. Robust dispatch matcher (`engine/orderbook.py` + used by `rule6`)

`finished_gate` currently matches `_norm(name) == "DISPATCH"`. Replace the exact match
with `is_dispatch(name)`: alphanumeric-only, uppercase, in `{"DISPATCH", "DISAPTCH"}`.
Same helper used wherever Rule 6 needs to know a step is dispatch. Additive: `DISPATCH`
behaviour is unchanged; `DISAPTCH` now also resolves as the gate (fixing a latent bug
where such an item's gate silently fell through to "last step").

### 5. Machine-utilization view (`rule6_allocate.build_machine_view`)

Exclude the `"OS / Outsourced"` and `"Off-machine"` lanes from the machine timeline /
utilization summary — they are not machines, and OS bars now overlap in parallel,
which would break the one-op-at-a-time idle math. They remain on the Gantt.

## Data flow (unchanged except the OS branch)

```
Order Book → active SO-lines → R1 → R2 → R3 → R6 allocate
                                              │
                                    per process: _is_os? ──yes, cycle>0──▶ reserve flat
                                              │                            continuous block
                                              │                            (OS lane, no op,
                                              │                            unlimited); next
                                              │                            step waits for end
                                              └── no ──▶ existing machine / off-machine paths
```

## Testing

- **Rule 6 OS (`tests/test_rule6.py` or a new `tests/test_rule6_os.py`):**
  - A 500-piece OS step reserves **exactly** its cycle-time minutes (not ×qty, no setup).
  - The block is **continuous** — a 1440-min OS step spanning a Thursday still ends
    exactly 24 h later (does not stretch across the day off).
  - **No operator** on the OS entry; the successor starts at the OS block's end.
  - **Two OS orders overlap** in wall-clock (unlimited parallel; `OS` not in `machine_free`).
  - **Blank-cycle OS** → zero-duration milestone (no crash), reported.
  - `OS` never appears as a machine / provisional machine.
- **Dispatch matcher (`tests/test_orderbook.py`):** `DISAPTCH` resolves as the
  finished-goods gate; `DISPATCH` unchanged.
- **Loader (`tests/test_loaders.py`):** an Allotted-`OS` routing loads with
  `allotted_machine == "OS"` and **no** `PENDING_MASTER_DATA` for `OS`.
- **Sample workbook:** extend `tests/sample_workbook.py` (or a focused fixture) with an
  OS-bearing routing so the above run against a full Test4-format workbook (masters present).
- **Golden trace:** the sample has no OS/`DISAPTCH` steps → `tests/golden_trace.json`
  **unchanged**. Full `pytest` stays green (currently 198).

## Out of scope (deferred, per the owner)

- Content-based sheet detection / uploading `Production1.xlsx` directly — not needed;
  the owner pastes the two sheets into Test4's named sheets before upload.
- Auto-keeping masters when an upload omits those sheets.
- A real OS vendor lead-time model beyond the single reserved block.
- Red-marking as an in-app feature — delivered as a **one-off** `Production1_flagged.xlsx`
  (routing sheet only) via `scratchpad/flag_inconsistencies.py`.

## Docs to update alongside the code

- `RULES.md` — Rule 6 "Off-machine steps" section: split into DISPATCH/zero-time
  milestones vs **OS reserved-time blocks** (continuous, unlimited, no operator).
- `CLAUDE.md` — the `rule6_allocate.py` and `loaders.py`/`orderbook.py` bullets.
```
