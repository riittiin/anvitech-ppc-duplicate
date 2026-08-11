# Gantt: one process per lane, with dates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NOTE: this is a **front-end-only** change verified in a real browser (no unit tests) — inline execution by an operator who can drive the browser is the better fit here.

**Goal:** Make the Gantt render each order's processes as stacked lanes (one process per line) with each bar's start→end dates printed as text, so overlapping process bars no longer hide each other.

**Architecture:** Pure front-end change in `web/app.js` `renderGantt` and `web/style.css`. The Gantt view-model (`engine/gantt.py`) is unchanged — every bar already carries `start`, `end`, `process`, `machine`, `operator`, `qty`, `color`, `offset_days`, `duration_days`.

**Tech Stack:** Vanilla HTML/JS/CSS front-end; FastAPI backend served at `/`. Tests: `python3 -m pytest` (Python only; no front-end unit tests). Local browser verify via `uvicorn`.

## Global Constraints

- **Front-end only.** Do NOT change `engine/gantt.py`, the engine, or the schedule. Python `pytest` must stay green (golden trace untouched).
- **Layout:** each order's timeline cell becomes a `.g-lanes` container stacking **one `.g-lane` per bar** (bars already sorted by start); each lane has the `.g-bar` (positioned by `offset_days*DAYW`, width `max(duration_days*DAYW, 8)`) plus a `.g-bar-dates` label at `left = offset*DAYW + barW + 6`.
- **Dates format:** `DD-MM-YYYY → DD-MM-YYYY`, collapsed to a single `DD-MM-YYYY` when start date == end date. Exact time stays in the existing hover `title` tooltip.
- **Keep unchanged:** the order-level columns, the month/day axis header, zoom buttons, machine-colour legend, late-completion flag, and non-working-day shading (Thursdays/holidays), which must still span behind all lanes.
- **Branch:** `gantt-process-lanes`. Do NOT push/merge to `main`.
- Baseline: `python3 -m pytest -q` → **216 passed** (must stay green; this change touches no Python).

---

### Task 1: Stacked-lane Gantt rendering + CSS

**Files:**
- Modify: `web/app.js` — `renderGantt` (~lines 426-461: the `rowsHtml` bar-building + the timeline `<td>` + the closing `.g-note`)
- Modify: `web/style.css` — the `.g-track` / `.g-bar` block (~lines 267-277)

**Interfaces:**
- Consumes (from `engine/gantt.py`, unchanged): each `bar` has `offset_days:number`, `duration_days:number`, `start:"DD-MM-YYYY HH:MM"`, `end:"DD-MM-YYYY HH:MM"`, `process`, `machine`, `operator`, `qty`, `color`. Helpers already in scope: `DAYW`, `axisW`, `grid`, `offDays`, `dateOnly(s)`, `escapeHtml(s)`.

- [ ] **Step 1: Replace the bar-building + timeline cell in `renderGantt`**

In `web/app.js`, inside `const rowsHtml = g.rows.map((r) => { ... })`, replace the `const bars = r.bars.map((b) => { ... }).join("");` block with a lane-building block:

```javascript
    const lanes = r.bars.map((b) => {
      const left = b.offset_days * DAYW, w = Math.max(b.duration_days * DAYW, 8);
      const d0 = dateOnly(b.start), d1 = dateOnly(b.end);
      const dates = (d1 === d0) ? d0 : `${d0} → ${d1}`;
      const op = b.operator ? ` · ${b.operator}` : "";
      const tip = `${b.process} · ${b.machine}${op} · ${d0}${d1 !== d0 ? " → " + d1 : ""} · qty ${b.qty}`;
      return `<div class="g-lane">`
        + `<div class="g-bar" style="left:${left}px;width:${w}px;background:${b.color}" title="${escapeHtml(tip)}">${escapeHtml(b.process)}</div>`
        + `<div class="g-bar-dates" style="left:${left + w + 6}px">${escapeHtml(dates)}</div>`
        + `</div>`;
    }).join("");
```

Then, in the same `map`'s returned `<tr>`, replace the timeline cell:

```javascript
      <td class="g-timeline"><div class="g-lanes" style="width:${axisW}px;background-image:${grid}">${offDays}${lanes}</div></td>
```

(was `<div class="g-track" ...>${offDays}${bars}</div>` — change `g-track`→`g-lanes` and `${bars}`→`${lanes}`).

- [ ] **Step 2: Update the footer note to describe the new layout**

In `renderGantt`'s `root.innerHTML` template, replace the `<p class="g-note">…</p>` line with:

```javascript
    <p class="g-note">Each process sits on its own line, coloured by machine, placed on the day(s) it runs, with its start → end date shown. Hover a bar for machine · operator · time · qty. Status = Pending/Running per order.</p>`;
```

- [ ] **Step 3: Update the CSS**

In `web/style.css`, replace the `.g-track { ... }` rule (~lines 267-270) with the two lane rules, and adjust `.g-bar`'s `top`, and add `.g-bar-dates` + top-aligned body cells:

```css
.g-lanes { position: relative; }
.g-lane { position: relative; height: 26px; }
.g-offday { position: absolute; top: 0; bottom: 0; background: rgba(210, 153, 34, 0.10); }   /* non-working day, spans all lanes */
.g-bar {
  position: absolute; top: 3px; height: 20px; line-height: 20px;
  padding: 0 5px; font-size: 11px; color: #1a1a1a; border-radius: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  box-shadow: inset 0 0 0 1px rgba(0,0,0,0.18); cursor: default;
}
.g-bar-dates {
  position: absolute; top: 0; height: 26px; line-height: 26px;
  font-size: 11px; color: var(--muted); white-space: nowrap; pointer-events: none;
}
.g-table tbody td { vertical-align: top; }
```

(The existing `.g-offday` rule at ~line 271 is now redundant — remove that old line so it isn't defined twice; the version above supersedes it. The old `.g-bar` block at ~lines 272-277 is replaced by the one above.)

- [ ] **Step 4: Python suite still green (no Python touched, sanity check)**

Run: `python3 -m pytest -q`
Expected: **216 passed** (unchanged — this task edits only `web/`).

- [ ] **Step 5: Commit**

```bash
git add web/app.js web/style.css
git commit -m "Gantt: one process per lane with start→end dates (no more overlapping bars)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Browser verification (real data)

**Files:** none (verification only — no commit unless a fix is needed).

**Interfaces:** none.

- [ ] **Step 1: Start a local server on a fresh store**

```bash
cd "/Users/ritinwadekar/Desktop/Anvitech Rebuilt"
rm -rf /tmp/gantt_verify_store
STORE_DIR=/tmp/gantt_verify_store nohup python3 -m uvicorn api.main:app --port 8021 >/tmp/gantt_uv.log 2>&1 &
sleep 3
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8021/login   # expect 200
```

- [ ] **Step 2: Log in + upload Test4 + plan (cookie session)**

```bash
curl -s -c /tmp/gantt_ck.txt -X POST http://127.0.0.1:8021/login -d "username=anvitech&password=1930rail" >/dev/null
curl -s -b /tmp/gantt_ck.txt -F "file=@Test4.xlsx" http://127.0.0.1:8021/upload | python3 -c "import sys,json;d=json.load(sys.stdin);print('added',d.get('added'),'items',d['summary']['items'])"
curl -s -b /tmp/gantt_ck.txt -X POST http://127.0.0.1:8021/run -H 'Content-Type: application/json' -d '{}' | python3 -c "import sys,json;d=json.load(sys.stdin);g=d.get('gantt',{});print('gantt rows:',len(g.get('rows',[])))"   # expect >0 rows
```

- [ ] **Step 2b: Drive the browser to the Gantt tab**

Load the Chrome MCP tools (one ToolSearch call), open `http://127.0.0.1:8021/`, log in through the form if prompted, click the **Gantt** tab, and screenshot. Confirm visually:
1. For an item with several processes (e.g. one with a CNC + OUTSOURCE + VMC + WASHING), each process is on its **own line** — no bars stacked on top of each other.
2. Each process bar shows a readable **start → end date** to its right (same-day processes show one date).
3. The month/day axis, **Expected completion** column, Status pills, zoom buttons, and the shaded non-working-day columns still render correctly.
Capture a screenshot as evidence (e.g. `gantt_after.png`).

- [ ] **Step 3: Tear down the server**

```bash
pkill -f "uvicorn api.main:app --port 8021" 2>/dev/null; rm -rf /tmp/gantt_verify_store
```

- [ ] **Step 4: If a visual defect is found**

Fix it in `web/app.js` / `web/style.css`, re-run Task 2 Steps 1-2b, and commit the fix with a `Gantt fix:` message. If the layout is correct, no commit — Task 1's commit stands.

---

## Self-Review

**Spec coverage:**
- Stacked one-process-per-lane layout → Task 1 Steps 1, 3. ✅
- Start→end dates on each bar (same-day collapse) → Task 1 Step 1 (`dates` var). ✅
- Non-working shading spans all lanes → Task 1 Step 3 (`.g-offday { top:0; bottom:0 }` inside `.g-lanes`). ✅
- Order columns / axis / zoom / legend / late-flag unchanged → Task 1 only touches the bars→lanes cell + the note. ✅
- No engine change / pytest green → Task 1 Step 4. ✅
- Browser verification with Test4 → Task 2. ✅

**Placeholder scan:** none — Task 1 shows complete JS/CSS; Task 2 shows exact commands.

**Type consistency:** the `lanes` variable replaces `bars` and is interpolated as `${lanes}` in the same cell; `.g-lanes`/`.g-lane`/`.g-bar`/`.g-bar-dates` class names match between the JS (Step 1) and CSS (Step 3); `dateOnly`, `escapeHtml`, `DAYW`, `axisW`, `grid`, `offDays` are all pre-existing in `renderGantt`'s scope. ✅
