# Design — Gantt: one process per lane, with start→end dates

**Date:** 2026-07-09
**Status:** approved (owner), ready to plan
**Branch:** `gantt-process-lanes`

## Problem

On the Gantt tab, each order is one table row whose timeline cell is a single
`.g-track` (height 28px). Every process is a `.g-bar` with `position:absolute; top:4px`
— so **all of an order's process bars sit on the same horizontal line and overlap**,
hiding each other. That overlap is worsened by the Rule 5 overlap scheduling (processes
run at overlapping times). The exact start/end datetimes exist only in the bar's hover
`title` tooltip, never as visible text. Result: the owner cannot tell, at a glance, when
each process for an item starts and ends.

## Goal

For any item, **immediately** see when each process starts and ends — no hovering, no
overlapping bars.

## Design (front-end only)

Change how the Gantt draws each order's timeline cell in `web/app.js` `renderGantt`
(+ supporting CSS in `web/style.css`). **No engine/`gantt.py` change** — every bar
already carries `start`, `end` ("DD-MM-YYYY HH:MM"), `process`, `machine`, `operator`,
`qty`, `color`, `offset_days`, `duration_days`.

### Layout: stacked lanes, one per process

Replace the single `.g-track` (all bars overlapping) with a `.g-lanes` container that
stacks **one `.g-lane` per bar** (in start order — the entries are already sorted by
start). Structure:

```
<td class="g-timeline">
  <div class="g-lanes" style="width:{axisW}px; background:{day-gridlines}">
    {offDays}                              <!-- absolute, top:0 bottom:0 → shaded non-
                                                working-day bands spanning ALL lanes -->
    for each bar:
      <div class="g-lane">                 <!-- block; stacks vertically -->
        <div class="g-bar" style="left:{offset*DAYW}px; width:{max(dur*DAYW,8)}px;
             background:{color}" title="{full tooltip, as today}">{process}</div>
        <div class="g-bar-dates" style="left:{offset*DAYW + barW + 6}px">{start→end}</div>
      </div>
  </div>
</td>
```

- Each process is on its **own line**, positioned on the same day/month date-axis as
  today (still a real timeline you can scan), so **no bars overlap**.
- The **start→end dates** print as text immediately to the right of each bar:
  `DD-MM-YYYY → DD-MM-YYYY`, collapsing to a single `DD-MM-YYYY` when the process starts
  and ends on the same day. Exact **time** stays in the existing hover tooltip.
- Non-working days (Thursdays/holidays) remain shaded as vertical bands behind all lanes
  (the `offDays` divs become `top:0; bottom:0` inside `.g-lanes`).
- The order-level columns (Item name, Item Code, SO No, SO Qty, SO Del date, **Expected
  completion**, Status), the month/day axis header, the zoom buttons, the machine-colour
  legend, and the late-completion flag are all **unchanged**.

### CSS (new / adjusted in `web/style.css`)

- `.g-lanes { position: relative; }` (grid-gridline background applied inline as today's
  `.g-track` did).
- `.g-lane { position: relative; height: 26px; }` (block → lanes stack; bar sits inside).
- `.g-bar` keeps its look but is positioned relative to its lane (`top: 3px`).
- `.g-bar-dates { position: absolute; top: 0; line-height: 26px; font-size: 11px;
  color: var(--muted); white-space: nowrap; }`.
- `.g-offday` stays absolute; inside `.g-lanes` it spans all lanes.

An order with N processes becomes N lanes tall (accepted trade-off: more vertical
scrolling for many-process items; the owner chose always-visible over click-to-expand).

## Out of scope
- No change to `gantt.py`, the engine, or the schedule itself — purely presentation.
- Time-on-bar (only dates are shown on the bar; time stays on hover) — can be added later
  if the owner wants it.
- Parallel-split halves (two entries, same process seq) simply render as two lanes, each
  labelled with its own machine via the tooltip — no special grouping.

## Testing / verification

Front-end change with no automated UI tests, so verify in a **real browser** (the
HANDOFF local-verify flow): start a local `uvicorn` on a spare port with a fresh
`STORE_DIR`, log in, upload `Test4.xlsx`, POST `/run`, open the Gantt tab, and confirm:
1. each process of an item sits on its **own line** (no overlapping bars),
2. each shows a readable **start→end date**,
3. the axis, expected-completion, status, zoom, and non-working shading still work.
Capture a before/after screenshot. `python3 -m pytest` stays green (Python untouched).
