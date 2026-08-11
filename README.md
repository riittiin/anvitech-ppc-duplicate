# Anvitech PPC Engine

A Production Planning & Control engine for Anvitech, a precision-machining job
shop. It reads customer sales orders and the shop's masters from an uploaded Excel
workbook, runs **sequenced rules** to produce a machine-by-machine schedule, then
re-plans (MRP re-run) as daily actuals come in.

The defining feature: **everything is testable and debuggable rule-by-rule.**
Each rule is a pure function and the pipeline snapshots every rule's input and
output into a trace that the per-rule frontend tabs render.

> Authority docs: [`CLAUDE.md`](CLAUDE.md) · [`RULES.md`](RULES.md) ·
> [design spec](docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md) ·
> [order-book design](docs/superpowers/specs/2026-06-22-order-book-design.md)

## Quick start

```bash
pip install -r requirements.txt

# Run the tests (engine + API)
pytest

# Start the app (engine API + frontend tabs) at http://127.0.0.1:8000
uvicorn api.main:app --reload
```

Open `http://127.0.0.1:8000`. The **Orders** tab is the home view (the order
book). **Upload your Excel** to add orders, then click **Plan** to schedule them.
Each numbered rule tab shows that rule's input/output, config, and decision notes;
a failing rule shows a red error and marks downstream tabs "not reached". The
**📊 Gantt** tab is the worker-facing schedule.

## Login & roles (whole app is gated)

The entire app — UI, API, and static assets — sits behind an **app-owned session
login** with **two roles**. A login page sets a signed (HMAC-SHA256) session
cookie; there is a Logout button and a `/me` endpoint.

- **Admin** — full control.
- **User** — read-only view of every tab, plus download the machine-allocation CSV
  and submit Capture Actuals (including marking an order complete). No config bar,
  Plan, Upload, or Delete. Enforced server-side, not just hidden in the UI.

Credentials are defined in `api/auth.py` and each can be **overridden by env vars**
without a code change:

- admin: `ADMIN_USERNAME` / `ADMIN_PASSWORD` (baked default `anvitech` / `1930rail`)
- user: `USER_USERNAME` / `USER_PASSWORD` (baked default `anvitech_user` / `anvitech12345678`)
- `SESSION_SECRET` (optional; a random secret is generated + stored if unset)

Hardening built in: username-keyed login rate limiting, CSRF Origin check, CSP +
security headers, interactive docs disabled, upload size cap. **Change the baked
credentials (edit `api/auth.py`) or set the env overrides before exposing the app.**

## Free public deployment (Render + a free database — no credit card)

Render hosts the app (free web service, public HTTPS); a free database makes the
order book + actuals durable. Persistence is **opt-in via env vars** — locally, with
none set, the app uses a local file store (`data/store/`). The live deployment runs
on **Render + MongoDB Atlas**.

1. **Push the code to GitHub** (Render deploys from Git).
2. **Pick a durable store** — the engine selects **MongoDB > Upstash > local file**:
   - **MongoDB Atlas (recommended):** free **M0 cluster (512 MB)**. Create a DB user,
     allow network access `0.0.0.0/0`, copy the `mongodb+srv://…` string → set
     `MONGODB_URI`. (`pymongo[srv]` is already in `requirements.txt`.)
   - **or Upstash Redis (256 MB free):** copy the DB's REST URL + token → set
     `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`.
3. **Create the Render service:** render.com → New → **Blueprint** → pick this repo
   (it reads `render.yaml`). You get a public `…onrender.com` URL.
4. **Set env vars** on the Render service: the store var(s) from step 2, and
   **optionally** the auth overrides (`ADMIN_*`/`USER_*`/`SESSION_SECRET`) — the
   login works with none set (baked credentials). Save → it redeploys.
5. Open the URL, log in, **upload your Excel**, click **Plan**. Orders + actuals now
   persist across sessions and devices.

Free-tier note: the Render free service **sleeps after ~15 min idle**, so the first
visit after a quiet period takes ~30–60s to wake. The data lives in your database,
so sleeping never loses it. (`GET /trace/{id}` may miss across instances — the UI
uses the `/run` response directly, so it doesn't matter.)

Want deep searches to run faster and always-on instead of on shared GitHub compute?
See `docs/ORACLE_WORKER.md` for the (optional, free) owner runbook to set up your
own dedicated Oracle Cloud worker.

## How it works

```
Upload Excel ─▶ merge into the Order Book (by (SO#, item code))
Order Book ─▶ active SO-lines (remaining qty) ─▶ R1 consolidate ─▶ R2 sort ─▶
              R3 smart priority ─▶ R6 allocate (R4 setup, R5 overlap)
              ─▶ schedule + Gantt
Rule 7 actuals reduce each order's remaining qty; "mark complete" archives it.
```

- **Forward chain:** `1 → 2 → 3 → 6`. Rules **4, 5, 7** are calc helpers consumed
  inside Rule 6. Rule 3 also reads the routing master.
- **Rule 6 is a non-delay scheduler:** it places work at the operation level and
  never leaves a machine idle while an operation is ready for it — the moment a
  machine frees up, the next batch that needs it runs. Priority (Rules 1–3) only
  breaks ties between equally-ready operations. The Rule 6 tab shows the schedule
  plus a **machine-wise view** (per-machine queue + utilization %) so you can see
  machines run continuously (the CNC bottleneck sits near 97% utilization).
- **Stateful order book.** Uploads merge into a persistent book (keyed by the
  **(SO number, item code)** pair — an SO# alone isn't unique; same SO# + different
  item = two orders);
  **Plan** schedules every active order at its *remaining* qty (ordered − good
  produced) through Rules 1–6 unchanged — so "Run" and "Rerun MRP" are one action.
  Orders flow Pending → Running (first actual) → Complete (ticked on a Rule 7 entry).
  Durable in MongoDB/Upstash — see `engine/orderbook.py` + `book_store.py`.

### Layers

| Layer | What |
|---|---|
| `engine/` | Pure Python: `config`, `models`, `loaders`, `worktime`, `pipeline`, `rules/`, the order-book layer (`orderbook`, `book_store`, `storage`), and `gantt` |
| `api/` | Thin FastAPI: `/login` `/logout` `/me`, `/upload`, `/run`(=`/rerun`), `/orders` (+ `/orders/delete`, `/orders/clear` — password-confirmed), `/actuals` (+ `/actuals/rollback`), `/items`, `/gantt`, `/report`, `/trace/{id}`; session gate + role enforcement + password re-auth on deletes + security-headers middleware (`api/auth.py`) |
| `web/` | `📋 Orders`, the per-rule tabs, and `📊 Gantt` — `app.js` renders the trace (no per-rule UI code) |
| `tests/` | Per-rule, order book, storage, pipeline, golden snapshot, and API |

## Key design facts

- The user **uploads** their masters/SO Excel (read-only); the orders merge into the
  **persistent order book** (keyed by SO#) and the masters are stored. There is no
  bundled data file — tests + the golden snapshot use a code-generated sample
  (`tests/sample_workbook.py`). The only thing the app writes is the durable store
  (`engine/storage.py`: MongoDB > Upstash > local file).
- **"Total process time" (Rule 3) = sum of per-process _cycle_ times.** This was
  confirmed against the SO Remarks oracle in the sheet — it reproduces the
  documented priorities (item `61240807-01` highest, `61247047-01` lowest); the
  sparse "Total time" column does not.
- **Machine-name normalization.** Routings write `CNC4`/`VMC1`; the master writes
  `CNC 4`/`VMC 1`. Both collapse to one canonical key, so they match with no alias
  table.
- **Fail loud, fail localized:**
  - *Loader-level* gaps are non-blocking and collected into a report:
    `PENDING_MASTER_DATA` (a routing references a machine not yet in the master,
    e.g. `CNC7` → registered as a **provisional** machine; add it to the Excel
    later with no code change) and `NO_ROUTING` (an SO item with no recipe is
    skipped). Surfaced in the UI's loader-report panel.
  - *Rule-level* contract violations raise `RuleError` — the pipeline records it
    in that rule's trace entry and stops the chain.

## Configurable parameters

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process (CNC/VMC only) | 90 min | Rule 4 |
| Operation overlap mode | sequential / 50% | Rule 5 |

All validated at run start. Edit them live in the frontend or pass a `config`
object to `POST /run`.

## Regenerating the golden snapshot

`tests/test_pipeline_golden.py` compares the forward trace to
`tests/golden_trace.json`. After an intentional logic change:

```bash
REGEN_GOLDEN=1 pytest -k golden
```
