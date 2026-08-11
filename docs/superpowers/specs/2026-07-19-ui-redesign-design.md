# UI/UX redesign — clean, simple, idiot-proof

**Date:** 2026-07-19 · **Status:** APPROVED by the owner (Team card removed per his direction) ·
**Hard constraint:** the backend does not change — same endpoints, same request/
response shapes, same role gating, same features. Frontend files only
(`web/index.html`, `web/app.js`, `web/style.css`).

## The problem (owner's words + confirmed in code)

The page is one long vertical dump: toolbar → optimize panel → settings →
report → absences list → the full operator master → and only THEN the actual
work tabs. First-time users land on master data instead of their work. No
visual hierarchy, minimal styling (394 lines of CSS), banners scattered.

## The redesign in one sentence

A top navigation with six clear destinations, a one-line status strip that
always says what the system is doing, and everything administrative tucked
into Settings — so a worker sees exactly one thing: their work.

## Layout (wireframe)

```
┌────────────────────────────────────────────────────────────────┐
│  ANVITECH PPC          Orders · Schedule · Gantt · Daily Entry │
│                        · Analytics · Settings          [Logout]│
├────────────────────────────────────────────────────────────────┤
│  ● 54 orders · 42 late · Plan follows today (19-07-2026)       │
│  Next optimization: Mon 11:00 · Next shift rotation: Fri 24-07 │
│  ▸ last note: "Checked 10:56 — current plan still best."       │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│                    [ the ONE selected view ]                   │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

## FULL placement map — every existing feature's new home

**Optimize moves to the Schedule view** (it changes the schedule — that's
where its effect is seen), not Settings. **The Operator & shift master is a
first-class card inside Settings.** Detail per view below.

## The six destinations

1. **Orders** (landing view, both roles) — the order book table as today, plus
   the Upload control at the top (admin; it belongs where orders arrive).
   Lane badges, promised/expected columns unchanged. Empty state: "Upload
   your Excel to begin."
2. **Schedule** (both) — [admin] the OPTIMIZE card at top (explainer, Start
   "Deep search", Stop & keep best, progress, before/after result with
   Apply/Discard, applied/staleness banner + Remove); then the Rule 6
   allocation table + both CSV downloads (full + shift-wise).
3. **Gantt** (both) — unchanged Gantt, full width.
4. **Daily Entry** (both) — the floor's page, two stacked cards:
   the entry form (SO → Item → Process pickers, Operator dropdown, Shift
   dropdown, date, produced/rejected qty, downtime fields, remarks,
   mark-complete, one big "Save entry" button) and "Today's saved
   entries" with per-entry rollback. NO Team card (owner decision
   2026-07-19): operators/shifts/absences are admin-only knowledge; the
   admin conveys shift timings to the floor personally. The user role
   sees operator NAMES only inside the entry form's dropdown (required
   for punching), never the master table, shifts, pins, or absences.
5. **Analytics** (both) — the utilization/bottleneck tab as today.
6. **Settings** (admin only in nav; absent for users) — one scrollable page,
   four cards in order:
   a. **Plan settings** — Start-from-today checkbox + date, consolidation
      window, setup time, overlap %, priority metric/window, the four
      option ticks (operator logic, split parallel, expedite, balance
      load), and the "Save & re-plan" button.
   b. **Operators & shifts** (the master) — the full table (name, machines,
      shift dropdown, "Stays" pin, remove), the add-operator row, and the
      "Shifts rotate every Friday — next rotation: DD-MM" line.
   c. **Absences** — operator + from/to date pickers, "Mark absent", and
      the current-absence list with one-click remove.
   d. **Efficiency report** — month picker (defaults to last month),
      on-screen preview, "Download CSV".
   Delete order / clear book stay ON the Orders view (bottom, visually
   separated danger strip, password modal unchanged).
   The user role has NO operator/shift/absence surface anywhere (owner
   decision — admin conveys shifts verbally).

## The status strip (always visible, both roles)

One calm line replacing today's scattered banners: order count · late count ·
plan basis ("follows today", resolved date) · next scheduled optimization ·
next rotation · the latest auto-note (collapsible). Warning states (staleness
banner, unstaffed hours) appear here as a colored chip that expands on click.
All fed by existing response fields — zero new endpoints.

## Visual system (self-contained; no frameworks, no external assets — CSP-safe)

- System font stack; 3 sizes (14/16/20) + one display size for headings.
- CSS variables: ink #1a1d21, paper #f6f7f9, card #ffffff, accent #1f6feb,
  ok #1a7f37, warn #b35900, danger #b42318, line #e4e7eb.
- Cards with 8px radius + subtle border (no shadows-everywhere), 8px spacing
  grid, tables with sticky headers + zebra rows + right-aligned numbers.
- Buttons: one primary (accent, verbs: "Upload & merge", "Start
  optimization", "Save entry"), ghost secondary, red only for destructive.
- Badges for lanes/status (Pending/Running/Complete, Open/Committed/Urgent).
- Every section: a title + ONE plain-language explainer sentence.
- Responsive down to tablet width (the Gantt/tables scroll inside their
  cards, never the page sideways).

## Idiot-proofing rules

- One view at a time; the nav highlights where you are.
- Every empty state names the next action.
- Dates DD-MM-YYYY everywhere (existing convention).
- Danger (delete orders, clear book) stays behind the existing password
  modal, grouped at the bottom of Settings, visually separated.
- The user role never sees empty admin shells — admin-only cards are absent
  from their DOM (existing gating pattern, applied consistently).

## Implementation approach (risk containment)

- **Keep `app.js` logic and every element ID intact.** The redesign
  restructures `index.html` (nav + view wrappers around the existing
  sections), adds a ~40-line hash router (show/hide views, restore last
  view), and rewrites `style.css` as the design system. The existing tab
  machinery for trace tabs is absorbed into the new nav (same render
  functions, new mount points).
- No backend edits of any kind; no new endpoints; no changed payloads.
- Golden/pytest untouched by construction; the API test suite must stay
  green (it will — no api changes).
- REQUIRED verification: full browser walkthrough of both roles on the
  sample book (every view, every action incl. upload, punch, absence,
  operator edit, optimize preview, efficiency download), plus a visual
  check on a narrow window.

## Out of scope (explicitly)

Dark mode; mobile-phone layouts; charts beyond existing Analytics; any
change to wording of stored data; framework adoption.
