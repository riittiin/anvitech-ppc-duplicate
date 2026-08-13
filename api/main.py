"""FastAPI layer — order-book engine + per-rule trace, behind one login.

Endpoints:
  POST /upload        merge an uploaded workbook into the order book (+ masters)
  POST /run           plan the active order book (unifies old Run + Rerun MRP)
  POST /rerun         alias of /run (kept for compatibility)
  GET  /orders        the order-book dashboard (status / remaining per SO#)
  GET  /gantt         Gantt for the current plan
  POST /actuals       save a daily actual; mark-complete archives the order
  GET  /items         item metadata for the Rule 8 form
  GET  /report        loader validation report
  GET  /trace/{id}    a past run's trace
The web/ frontend is served at /.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.loaders import load_all
from engine import loaders
from engine.models import PlanRun, Actual, Masters, fmt_date
from engine.pipeline import run_forward, to_table, KEY_SEP
from engine import optimizer
from engine import optimize_service
from engine.gantt import build_gantt
from engine import book_store, orderbook
from engine import operator_coverage
from engine import storage
from engine import efficiency
from engine import operator_master
from engine import freeze
from engine.rules import (
    rule3_tiebreak_process_time as r3,
    rule4_setup_time as r4,
    rule5_overlap_mode as r5,
    rule6_allocate as r6,
    rule7_capture_actuals as r7,
)
from api import auth

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB cap on uploaded workbooks
MAX_LOGIN_BYTES = 8 * 1024            # tiny cap on the login form body

@asynccontextmanager
async def _lifespan(app):
    # Resolve the signing secret once at startup (avoids a first-request race /
    # latency blip). Lazy resolution still covers any path that skips startup.
    try:
        auth.get_secret()
    except Exception:
        pass
    yield


# Interactive docs disabled in the deployed app to shrink the attack surface
# (they were behind auth anyway — this is defense-in-depth).
app = FastAPI(title="Anvitech PPC Engine", lifespan=_lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# --------------------------------------------------------------------------- #
# Login + session gate. The whole app (UI + API + static) sits behind a signed
# session cookie, with two roles (admin / user). See engine-free api/auth.py.
# --------------------------------------------------------------------------- #
# Exact (method, path) allowlist of pages reachable WITHOUT a session. Matched
# exactly — never by prefix or extension — so no static file leaks past the gate.
_PUBLIC = {("GET", "/login"), ("POST", "/login"), ("POST", "/logout")}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    method, path = request.method, request.url.path

    # CSRF: reject a state-changing request only when an Origin/Referer is present
    # AND its host doesn't match ours. Absent (curl / server-to-server) → allowed;
    # those carry no ambient cookie, so they're not a CSRF vector.
    if method not in _SAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            o = urlsplit(origin).netloc
            if o and o != request.headers.get("host", ""):
                return Response("cross-origin request rejected", status_code=403)

    if (method, path) in _PUBLIC:
        return await call_next(request)

    # Cloud-optimize worker endpoints: authenticated by a shared secret header
    # (server-to-server from the GitHub Actions runner — no session cookie).
    # Constant-time compare; with no OPTIMIZE_WORKER_SECRET configured these
    # paths simply fall through to the normal session gate (→ 401).
    if ((method == "GET" and path.startswith("/optimize/job/"))
            or (method == "GET" and path == "/optimize/pending")
            or (method == "POST" and path in ("/optimize/progress",
                                              "/optimize/result",
                                              "/optimize/shard-result"))):
        if _worker_secret_ok(request):
            request.state.user = "cloud-worker"
            request.state.role = "worker"
            return await call_next(request)

    payload = auth.verify_token(request.cookies.get(auth.COOKIE_NAME))
    if payload is None:
        # Browser navigation → send to the login page; API/XHR → 401 (no
        # WWW-Authenticate header, so no browser Basic-Auth popup).
        if method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=302)
        return Response("authentication required", status_code=401)
    request.state.user = payload["u"]
    request.state.role = payload["role"]
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if _is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.middleware("http")
async def request_store_cache(request: Request, call_next):
    """Give each request a per-request store read cache: every durable-store KEY is
    read from the backend at most once per request (a write to a key drops its
    cached reads, so read-after-write stays correct). This removes the redundant
    round-trips a single request makes — the operator table alone was read 4× per
    plan — which is the dominant latency on the MongoDB/Upstash backends. Purely a
    performance layer: the data returned is identical. The cache lives only for the
    request; outside a request the raw backend is used (see engine.storage)."""
    token = storage.begin_request_cache()
    try:
        return await call_next(request)
    finally:
        storage.end_request_cache(token)


def require_admin(request: Request):
    """Raise 403 unless the verified session role is admin. Role comes ONLY from
    the signed session — never from a request body/header/query."""
    if getattr(request.state, "role", None) != auth.ADMIN:
        raise HTTPException(status_code=403, detail="admin only")


def require_password(request: Request, password: str):
    """Re-authenticate the signed-in admin by their password (a deliberate guard on
    destructive actions). Raises 403 if it doesn't match. Constant-time via auth."""
    user = getattr(request.state, "user", "")
    if auth.authenticate(user, password) != auth.ADMIN:
        raise HTTPException(status_code=403, detail="password confirmation failed")


# --- login / logout / identity ------------------------------------------- #
def _render_login(error: str = "") -> str:
    """Login page HTML with a server-controlled (constant, safe) error message."""
    html = (WEB_DIR / "login.html").read_text(encoding="utf-8")
    block = f'<div class="err">{error}</div>' if error else ""
    return html.replace("<!--ERROR-->", block)


def _set_session(response: Response, token: str, request: Request):
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=_is_https(request), path="/")


@app.get("/login")
def login_page():
    return HTMLResponse(_render_login())


@app.post("/login")
async def login_submit(request: Request):
    if int(request.headers.get("content-length") or 0) > MAX_LOGIN_BYTES:
        return HTMLResponse(_render_login("Request too large."), status_code=413)
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if auth.is_rate_limited(username):
        return HTMLResponse(
            _render_login("Too many attempts. Please wait a few minutes and try again."),
            status_code=429)
    role = auth.authenticate(username, password)
    if role is None:
        auth.record_failed_login(username)
        if auth.FAILED_LOGIN_DELAY:
            await asyncio.sleep(auth.FAILED_LOGIN_DELAY)
        return HTMLResponse(_render_login("Incorrect username or password."),
                            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    _set_session(resp, auth.make_token(username, role), request)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Commit/uncommit feature gate (2026-08-04,
# docs/superpowers/specs/2026-08-04-hide-commitment-feature-design.md)
#
# Anvitech's directors asked for the Committed/Open lanes to be HIDDEN, not
# removed — they may want them back. Flip this to True and the whole feature
# returns: the Commit/Uncommit buttons, the Lane + Promised columns, and both
# endpoints. Any order committed before is still exactly as it was; nothing is
# migrated or rebuilt.
#
# The engine is deliberately untouched — `Order.commitment`, the optimizer's
# promise penalty and every promise test stay live. With no order committed that
# machinery is dormant, so hiding the feature does not move the schedule at all.
# The endpoints close WITH the buttons on purpose: buttons gone + endpoint open
# would let an order be committed through the API, where it would steer the
# optimizer (engine/optimizer.py:94) with nothing on screen to reveal or undo it.
COMMITMENT_FEATURE_ENABLED = False


# --------------------------------------------------------------------------- #
# PLAN START: SHIFT START, NOT THE NEXT HOUR (owner decision, 2026-08-11 —
# docs/superpowers/specs/2026-08-11-plan-start-at-shift-start-design.md)
#
# False (live) = the plan always begins at 08:00 of the plan date. Press Optimize
# at 09:30 and the schedule starts 08:00 that morning, because the book needs the
# whole shift. This is the pre-2026-08-03 behaviour, restored on the owner's call
# after he ran the next-hour version on the floor for a week.
#
# True = the 2026-08-03/08-07 behaviour: a stored plan clock at the next full hour
# (`_stamp_plan_clock`), advancing when an optimization finishes or on the first
# plan of a new day. The whole mechanism is kept live and tested behind this one
# flag — `_ceil_next_hour`, `_stamp_plan_clock`, `book_store.save/load_plan_start_
# floor`, `Config.plan_start_floor` and `new_engine._plan_config`'s
# max(08:00, floor) — so bringing it back is this line and nothing else.
#
# Known and accepted: with it off, a run after the shifts end plans from 08:00
# THAT morning, so the schedule's first hours have already passed. The next day's
# punches and the freeze pass correct it.
#
# The 2026-08-07 "one plan, one set of dates" guarantee is not weakened by this —
# 08:00-of-today is constant for the whole IST day, whereas the clock it replaces
# advanced every time a contest landed.
PLAN_START_NEXT_HOUR = False


def _require_commitment_feature():
    """404 while the lanes are hidden — the endpoint simply does not exist."""
    if not COMMITMENT_FEATURE_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/me")
def me(request: Request):
    return {"username": getattr(request.state, "user", None),
            "role": getattr(request.state, "role", None),
            # The UI hides its commitment controls/columns from this — one source
            # of truth, so the screen and the server can never disagree.
            "commitment_enabled": COMMITMENT_FEATURE_ENABLED}


# --------------------------------------------------------------------------- #
# Masters: from the latest uploaded workbook, else the bundled test file.
# Cached in-process, keyed by the workbook's content hash.
# --------------------------------------------------------------------------- #
# Recent plan traces, keyed by run_id, for the /trace/{id} endpoint. Bounded so it
# can't grow without limit (every save auto-re-plans) and leak memory on the worker.
_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_RUNS_MAX = 40
_MASTERS_CACHE: dict = {"key": None, "masters": None}

# Plan-result cache (in-process). `_plan` is CPU-heavy (the operator-stable engine
# schedules the whole book) and re-runs on every login/refresh even when nothing
# changed — the dominant latency on Render's weak free CPU. We cache the last plan
# result keyed by a fingerprint of EVERY input that determines it (see
# _plan_fingerprint), so a login with no changes is served instantly. The fingerprint
# self-invalidates on any real change (orders, actuals, absences, masters, operators,
# config, applied ranks, or a new day), so a stale plan can never be served.
# CAREFUL — the response carries more than the plan. It also carries the Orders tab
# table, which spans the WHOLE book (active + archived) while the plan spans only the
# lines with work remaining. The fingerprint must cover what is DISPLAYED, not just
# what is SCHEDULED (`book_rows`), and `_plan` rebuilds that one table on every hit.
# Both, deliberately: 2026-08-08 shipped this bug because only the plan was covered.
_PLAN_CACHE: dict = {"key": None, "result": None}


def _store_run(run_id: str, trace: dict) -> None:
    _RUNS[run_id] = trace
    while len(_RUNS) > _RUNS_MAX:
        _RUNS.popitem(last=False)   # evict the oldest


def _store_env_key():
    """Identity of the active store config — so the masters cache resets when a
    test swaps STORE_DIR/backend, but stays warm in production."""
    return (os.environ.get("MONGODB_URI"),
            os.environ.get("UPSTASH_REDIS_REST_URL"),
            os.environ.get("STORE_DIR"))


def _current_masters():
    """Masters from the latest uploaded workbook, else empty masters.

    The PARSED WORKBOOK is cached in-process (keyed by content hash / store
    config); the app-owned operator table is overlaid on EVERY call so display
    always reflects the latest Settings edits and a freshly-emptied store
    re-seeds. The cache never holds operators — they belong to the store, not
    the workbook."""
    key = _store_env_key()
    if _MASTERS_CACHE["masters"] is not None and _MASTERS_CACHE["key"] == key:
        base = _MASTERS_CACHE["masters"]
    else:
        raw = book_store.load_masters_bytes()
        if raw is None:
            # No workbook uploaded yet → empty masters; the UI prompts to upload.
            # (There is no bundled demo file anymore — production runs on uploads.)
            base = Masters()
        else:
            _, base = load_all(io.BytesIO(raw))
        _MASTERS_CACHE.update(key=key, masters=base,
                              sha=hashlib.sha256(raw).hexdigest() if raw else "none")
    return _with_operator_overlay(base)


def _with_operator_overlay(base):
    """Overlay the app-owned operator table (seed-once) onto a COPY of the cached
    workbook masters — the cache is never mutated. Seeds the store the first time
    masters carry operators and no table exists yet. Automatic Friday rotation
    was removed 2026-08-05 (`rotate_table` is now a no-op, kept only as the shared
    call every wiring site still goes through) — an operator's shift is whatever
    is on file in Settings, every week, until an admin changes it. Empty store +
    no workbook operators → the base is returned unchanged (nothing to overlay)."""
    today = _ist_today()
    table = book_store.load_operator_table()
    if table is None:
        if not base.operators:
            return base                       # nothing to seed from yet
        table = {"week_anchor": operator_master.last_friday(today).isoformat(),
                 "operators": operator_master.seed_rows_from_masters(base)}
        book_store.save_operator_table(table)
    rotated, flips = operator_master.rotate_table(table, today)
    if flips > 0:
        book_store.save_operator_table(rotated)   # lazy catch-up, idempotent
    return replace(base, operators=operator_master.to_operators(
        rotated.get("operators", [])))


def _masters_sha() -> str:
    """Content hash of the stored masters workbook (cached with the parsed masters)."""
    _current_masters()   # warms the cache (and refreshes sha after an upload)
    return _MASTERS_CACHE.get("sha") or "none"


def _inputs_signature(config: Config) -> str:
    """Fingerprint of everything an applied optimization's numbers depend on: the
    masters workbook + the schedule-shaping settings. When either changes after
    Apply, replaying the saved ranks legitimately yields different numbers — the
    UI must SAY so instead of looking non-deterministic (live 2026-07-15 finding:
    the owner re-uploaded an edited workbook after Apply and saw "the same run"
    produce different results). Schedule-neutral knobs are excluded:
    balance_operator_load never moves timing, and expedite is forced off whenever
    ranks replay, so neither can alter an optimized plan."""
    d = config.to_dict()
    d.pop("worst_ceiling_days", None)   # transient per-run ceiling, not a saved input
    d.pop("plan_start_floor", None)     # transient per-run 'next hour' start floor
    d.pop("balance_operator_load", None)
    d["expedite_window_min"] = 0
    d["consolidation_window_days"] = 1   # engine-decided now; a stale saved value is ignored
    # The scheduler's own semantics version: saved ranks were scored under a
    # specific allocation policy, so a deploy that changes it (e.g. the
    # scarce-first operator pick, 2026-07-19) must flag the applied plan stale
    # and let the scheduled contest re-run — otherwise the ranks replay under
    # new semantics behind a green banner (review-caught deploy hole).
    d["scheduler_fingerprint"] = r6.SCHEDULER_FINGERPRINT
    from engine import flow_scheduler as _flow
    d["flow_fingerprint"] = _flow.FLOW_FINGERPRINT
    from engine import new_engine as _new
    d["new_engine_fingerprint"] = _new.SCHEDULER_FINGERPRINT
    # The roster engine's own semantics version, folded in ONLY when roster is the
    # selected scheduler. Adding it unconditionally (as the three above are) would
    # move every classic/flow/new signature the moment this line shipped, and
    # instantly flag the owner's applied optimization stale with a "re-run the deep
    # search" banner on a site nobody has switched engines on.
    if getattr(config, "scheduler", "classic") == "roster":
        from roster_engine import SCHEDULER_FINGERPRINT as _roster_fp
        d["roster_engine_fingerprint"] = _roster_fp
    # The crew genome is a real plan input when it is set (replay the same ranks
    # against a different roster and the plan moves), but it must be INVISIBLE when
    # it is not: leaving `None` in the blob would change the signature for every
    # engine simply because the field now exists.
    if not d.get("crew_rank"):
        d.pop("crew_rank", None)
    # Operators live in the store now, so the masters sha no longer covers them.
    # Fold ONLY the table's sorted row CONTENT (name/machines/shift/pin) into
    # the blob so a rotation or an operator edit correctly flags the applied
    # optimization stale (spec 2026-07-18). ids are excluded (churn only) — and
    # so is week_anchor: a net-no-op double-Friday catch-up advances the anchor
    # while leaving every shift identical, and hashing it would make the
    # signature differ from the applied meta until the next Apply, firing a full
    # (non-applying) scheduled contest on every tick. A genuine single rotation
    # still changes the persisted shift content, so it is still caught.
    table = book_store.load_operator_table()
    op_blob = None
    if table:
        op_blob = sorted([[r.get("name", ""), r.get("machines_raw", ""),
                           r.get("shift", ""), bool(r.get("pinned"))]
                          for r in table.get("operators", [])])
    blob = json.dumps([_masters_sha(), d, op_blob], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _report_after_upload(masters):
    """The validation report for the FILE just uploaded (upload endpoint only).

    Unlike ``_report_for_book`` (book-scoped, used by /run|/gantt|/report), this
    is deliberately loader-scoped: it returns ``masters.report`` AS-IS, so it
    keeps the loader's NO_ROUTING rows for every SO item the file's routings
    can't cover — those items are dropped before they ever reach the order book,
    so the book-scoped report never sees them and the admin would otherwise have
    no way to learn which item codes need a routing added. Also appends the
    absence-orphan rows (ABSENT_OPERATOR_UNKNOWN) for parity with the plan
    report. Does not touch or replay `_report_for_book`; /run stays book-scoped."""
    rows = list(masters.report)
    # Absence orphans are judged against the APP-OWNED operator table (operators are
    # app-owned; this file's Operator sheet is a fossil). Overlaying keeps the loader
    # rows from `masters.report` while checking absences against the real roster — so an
    # app-added operator isn't mislabelled an orphan, nor a workbook name kept as valid.
    for name in _absence_orphans(_with_operator_overlay(masters)):
        rows.append({"kind": "ABSENT_OPERATOR_UNKNOWN", "ref": name,
                     "message": f"absence entry for an operator not in the "
                                f"current masters: ignored"})
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in rows
    ])


def _absence_orphans(masters, absences=None) -> list:
    """Names on file in operator absences that are no longer in the current
    masters' Operator & shift Master (e.g. removed/renamed on a re-upload).
    Sorted for stable output. ``absences`` (optional) is the already-loaded
    list — pass it when the caller has one, to avoid a redundant store read;
    ``None`` (default) loads it here."""
    names = {o.name for o in masters.operators}
    if absences is None:
        absences = book_store.load_absences()
    return sorted({a["operator"] for a in absences
                  if a["operator"] not in names})


def _report_for_book(masters, so_lines, absences=None, config=None, schedule=None,
                     batches=None):
    """The validation report scoped to the CURRENT order book. Loader-level rows
    about the masters (pending machines, time coercions, …) pass through, but
    NO_ROUTING is re-derived from the live book: the stored workbook's own SO
    sheet can list orders that were never merged or were later deleted (live
    2026-07-15 bug: a "5 orders without routing" banner for ghost orders while
    every real order planned fine). Also appends a non-blocking row per absence
    entry whose operator is no longer in the masters (ABSENT_OPERATOR_UNKNOWN) —
    the entry is ignored by planning, not an error. ``absences`` (optional) is
    the already-loaded list — pass it when the caller has one (e.g. `/run`),
    to avoid a redundant store read; ``None`` (default) loads it here."""
    rows = [r for r in masters.report if r["kind"] != "NO_ROUTING"]
    seen = set()
    for l in so_lines:
        if l.item_code not in masters.routings and l.item_code not in seen:
            seen.add(l.item_code)
            rows.append({"kind": "NO_ROUTING", "ref": l.item_code,
                         "message": f"SO item '{l.item_code}' has no routing in "
                                    f"Item's process Master; order skipped "
                                    f"(cannot schedule without a recipe)"})
    for name in _absence_orphans(masters, absences=absences):
        rows.append({"kind": "ABSENT_OPERATOR_UNKNOWN", "ref": name,
                     "message": f"absence entry for an operator not in the "
                                f"current masters: ignored"})
    # Cross-check the two sources of truth: the workbook's Machine master says which
    # machines exist, the Settings operator table says who can run them (`masters` has
    # already had the Settings table overlaid by `_current_masters`). A machine nobody
    # is assigned to can never be scheduled — flag it rather than let the plan quietly
    # route around it (owner, 2026-08-07). Non-blocking, like every row here.
    # `config` is passed in by callers that already resolved one; resolving here would
    # make a REPORT able to stamp the plan clock as a side effect.
    rows.extend(operator_coverage.staffing_gaps(
        masters, config if config is not None else _load_plan_config()))
    # Hard invariant, checked rather than assumed: the plan must never put an operator
    # on a machine they are not assigned to in Settings. Surfaced, not raised — a live
    # plan must not break — but tests assert it is empty. See
    # new_engine.qualification_violations for why this exists.
    if schedule:
        try:
            from engine import new_engine as _ne
            _nm = _ne._apply_app_operators(
                _ne._new_masters(bool(getattr(config, "flexible_machines", False))),
                masters)
            rows.extend(_ne.qualification_violations(schedule, _nm))
        except Exception:  # noqa: BLE001 — a self-check must never break the report
            pass
        # Same deal for the routing: the plan must never run a step before the step
        # that feeds it. On a clean book the scheduler gets this right; with work
        # IN PROGRESS it shipped 63 inversions over 21 of 68 real orders
        # (live 2026-08-09). Checked every plan now. See
        # new_engine.routing_order_violations.
        try:
            from engine import new_engine as _ne2
            rows.extend(_ne2.routing_order_violations(schedule, masters))
        except Exception:  # noqa: BLE001 — a self-check must never break the report
            pass
        # And that the plan actually MAKES what the book still owes. A step given
        # fewer pieces than its batch owes is invisible everywhere else — every other
        # step of the same order still shows the full quantity (live 2026-08-11: a
        # frozen op ran only the punched SO line's 88 pieces and dropped the 281 of the
        # untouched SO clubbed with it). See new_engine.batch_quantity_violations.
        if batches:
            try:
                from engine import new_engine as _ne3
                rows.extend(_ne3.batch_quantity_violations(schedule, batches))
            except Exception:  # noqa: BLE001 — a self-check must never break the report
                pass
        # The roster engine's four guarantees, checked on every plan it builds:
        # one operator per machine per shift, an operation never interrupted,
        # no machine dark while a qualified operator is unassigned, and no
        # successor released on a fraction of a piece — plus the whole-plan
        # backstop under them, that no machine is ever occupied by two
        # operations at once (MACHINE_DOUBLE_BOOKED).
        #
        # GATED TO THE ROSTER ENGINE ON PURPOSE. The live engine genuinely
        # breaks the first two on every plan — hundreds of rows — and this
        # banner is the one the directors read on a site that has not switched
        # engines yet. Flooding it would break the rebuild's own rule that
        # nothing changes until DEFAULT_SCHEDULER=roster. The cross-engine
        # comparison is measured in the A/B harness, which calls
        # roster_engine.report directly on BOTH engines' output — that is where
        # the numbers belong, and where they are reported from.
        if str(getattr(config if config is not None else _load_plan_config(),
                       "scheduler", "") or "") == "roster":
            try:
                from roster_engine import report as _rr
                rows.extend(_rr.all_violations(
                    schedule, masters,
                    config if config is not None else _load_plan_config(),
                    absent=optimize_service.absence_reservations(
                        absences if absences is not None
                        else book_store.load_absences())))
            except Exception:  # noqa: BLE001 — a self-check must never break the report
                pass
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in rows
    ])


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    config: Optional[dict] = None
    persist: bool = False   # admin Plan-click persists; auto-load sends False


class DeleteRequest(BaseModel):
    # Each order is identified by its (SO number, item code) pair — an SO number is
    # not unique. Sent as [so_no, item_code] pairs.
    orders: List[List[str]] = []
    password: str = ""    # admin re-enters their password to confirm a delete


class ClearRequest(BaseModel):
    password: str = ""


class CommitRequest(BaseModel):
    # Each order is identified by its (SO number, item code) pair, sent as
    # [so_no, item_code] pairs — mirrors DeleteRequest.
    orders: List[List[str]] = []


class CompleteRequest(BaseModel):
    so_no: str
    item_code: str


class AbsenceRequest(BaseModel):
    operator: str
    from_date: str
    to_date: str


class OperatorRequest(BaseModel):
    name: str
    machines_raw: str = ""
    shift: str = ""


class OperatorPatchRequest(BaseModel):
    machines_raw: Optional[str] = None
    shift: Optional[str] = None
    pinned: Optional[bool] = None


class OptimizeRequest(BaseModel):
    budget: str = "deep"    # one option (owner decision): ~1,000 plans total.
                            # Legacy "quick" (cached clients) maps to the same.


class ActualRequest(BaseModel):
    so_no: str
    item_code: str
    entry_date: str
    shift: str = ""
    item_name: str = ""
    process: str = ""
    operator: str = ""
    # Quantities/times can never be negative — a negative would corrupt the
    # produced/rejected math and the plan. Rejected server-side (422), not just the UI.
    qty_produced: float = Field(default=0.0, ge=0)
    qty_rejected: float = Field(default=0.0, ge=0)
    actual_setup_min: float = Field(default=0.0, ge=0)
    no_power_min: float = Field(default=0.0, ge=0)
    no_operator_min: float = Field(default=0.0, ge=0)
    tool_problem_min: float = Field(default=0.0, ge=0)
    machine_breakdown_min: float = Field(default=0.0, ge=0)
    no_load_min: float = Field(default=0.0, ge=0)
    other_work_min: float = Field(default=0.0, ge=0)
    remarks: str = ""
    mark_complete: bool = False


# --------------------------------------------------------------------------- #
# Helper-tab augmentation (Rules 3/4/5/7 + Rule 6 machine view)
# --------------------------------------------------------------------------- #
def _augment_helpers(trace, plan_run, config, masters, actuals=None):
    if "rule3" in trace and trace["rule3"].get("reached", True) and plan_run.batches_prioritized:
        breakdown = r3.build_priority_breakdown(plan_run.batches_prioritized, config, masters)
        trace["rule3"]["tables"] = [
            {"title": "Priority breakdown: slack/critical-ratio per batch (lower slack = more urgent)",
             "table": to_table(breakdown)},
        ]

    if "rule6" in trace and trace["rule6"].get("reached", True):
        timeline, summary = r6.build_machine_view(
            plan_run.schedule, masters, config, plan_run.batches_prioritized)
        trace["rule6"]["tables"] = [
            {"title": "Machine timeline: per-machine queue (Idle before = working minutes the machine waited)",
             "table": to_table(timeline)},
            {"title": "Machine utilization", "table": to_table(summary)},
        ]
        # Analytics tab: utilization & bottlenecks derived from this plan.
        # Operator absences reduce each person's Available (hrs) so utilization
        # stays honest (Task 13 already keeps busy work out of absent days).
        from engine import analytics as _an
        trace["analytics"] = _an.build_analytics(
            plan_run.schedule, masters, config, plan_run.batches_prioritized,
            absences=book_store.load_absences())
        # Shift-wise timeline (download-only view): every op split into per-shift segments
        # with the real operator each shift. Not rendered as a panel (can be ~900 rows) —
        # attached under its own key for the "Download shift-wise schedule" button. Only
        # meaningful when operator/shift logic is on.
        if config.apply_operator_logic:
            trace["rule6"]["shiftwise"] = to_table(r6.build_shiftwise_timeline(
                plan_run.schedule, masters, config, plan_run.batches_prioritized))
        # Operator/shift coverage: when each machine can run, and unmatched specialties.
        if config.apply_operator_logic:
            from engine.operator_coverage import machine_windows
            windows, cov = machine_windows(masters, config)
            first = (config.first_shift_start_hour * 60, config.first_shift_end_hour * 60)
            second = (config.first_shift_end_hour * 60, (24 + config.second_shift_end_hour) * 60)
            manual = (config.manual_start_hour * 60, config.manual_end_hour * 60)

            def _cov_label(mid):
                iv = windows.get(mid)
                if iv is None:
                    return "-"
                if not iv:
                    return "⚠ needs operator"
                parts = []
                if first in iv:
                    parts.append("1st shift")
                if second in iv:
                    parts.append("2nd shift")
                if manual in iv:
                    parts.append(f"manual {config.manual_start_hour:02d}:00–{config.manual_end_hour:02d}:00")
                return " + ".join(parts) or "-"

            cov_rows = [{"Machine": m.display_name,
                         "Available Hrs/Day": m.available_hrs_per_day,
                         "Runs": _cov_label(mid) + (" (provisional)" if m.provisional else "")}
                        for mid, m in sorted(masters.machines.items())]
            trace["rule6"]["tables"].append({
                "title": "Operator coverage: when each machine can run (from Available "
                         "Hrs/Day + which shifts have a qualified operator)",
                "table": to_table(cov_rows)})
            if cov.get("unmatched_specialties"):
                trace["rule6"]["tables"].append({
                    "title": "Operator specialties that match no machine: check the "
                             "spelling/name in Excel",
                    "table": to_table([{"Operator": u["operator"], "Specialty": u["specialty"]}
                                       for u in cov["unmatched_specialties"]])})

    if plan_run.schedule:
        e = plan_run.schedule[0]
        routing = masters.routings.get(e.item_code)
        proc = routing.processes[0] if routing else None
        cycle = proc.cycle_time if proc else None
        notes4 = [f"occupancy = cycle({cycle}) x qty({e.qty:g}) + setup({config.setup_time_min}) = {e.occupancy_min:g} min"]
    else:
        notes4 = ["no scheduled processes to illustrate"]
    trace["rule4"] = {
        "input": to_table([{"Cycle Time": "per process", "Qty": "batch qty", "Setup": config.setup_time_min}]),
        "output": to_table([
            {"Batch": e.batch_id, "Process": e.process_name, "Occupancy (min)": round(e.occupancy_min, 2)}
            for e in plan_run.schedule
        ]),
        "config": config.to_dict(), "notes": notes4, "error": None, "reached": True,
    }

    # Rule 5 applied to THIS plan: every operation handoff, what it waited under
    # sequential vs this run, and how much overlap pulled the next op earlier.
    pct = config.overlap_percent
    overlap_rows = r5.build_overlap_view(plan_run.schedule, config)
    total_pulled = sum(r["Pulled earlier (min)"] for r in overlap_rows)
    n_overlapped = sum(1 for r in overlap_rows if r["Overlap applied"].startswith("yes"))
    rule5_notes = [
        f"Active mode this run: {config.overlap_mode}"
        + (f" ({pct}% of cutting time)" if config.overlap_mode == OVERLAP_PERCENT else ""),
        f"{len(overlap_rows)} operation handoff(s) in this plan; {n_overlapped} overlapped, "
        f"pulling later operations {total_pulled:g} working-minutes earlier in total vs sequential.",
        "Overlap % applies to the cutting time only: the 90-min setup is excluded "
        "(the next machine is set up in parallel). A step with no cutting time "
        "(deburring, inspection, washing, packing) does not overlap; its successor "
        "waits for it to fully complete.",
    ]
    if not overlap_rows:
        rule5_notes.append("No scheduled operations yet. Upload orders and click Plan.")
    trace["rule5"] = {
        "input": to_table([{
            "Overlap mode": config.overlap_mode,
            "Overlap %": pct,
            "Setup excluded from overlap (min)": config.setup_time_min,
            "Operation handoffs in plan": len(overlap_rows),
        }]),
        "output": to_table(overlap_rows),
        "config": config.to_dict(),
        "notes": rule5_notes,
        "error": None, "reached": True,
    }

    if actuals is None:
        actuals = book_store.load_actuals()
    total_down = sum(a.total_downtime_min() for a in actuals)
    # The 'Saved entries' list shows only the latest punched date (kept small +
    # rollback-able); the rollup + progress still cover ALL recorded actuals.
    visible = orderbook.actuals_on_latest_date(actuals)
    latest = orderbook.latest_actual_date(actuals)
    progress = orderbook.process_progress_rows(book_store.load_active_orders(), actuals, masters)
    tables = [{"title": "Totals by item: what was produced, and downtime added up from every entry",
               "table": to_table(r7.aggregate_by_item(actuals))}]
    if progress:
        tables.insert(0, {
            "title": "How many pieces are finished at each step so far",
            "table": to_table(progress)})
    list_note = (f"Showing the {fmt_date(latest)} entries. This is the only day you can undo. "
                 f"Earlier days are locked, but their totals are still counted below."
                 if latest else "No entries yet.")
    trace["rule7"] = {
        "input": to_table([{"Note": "Your entries are saved as soon as you click Save"}]),
        "output": to_table(visible), "actuals_ids": [a.id for a in visible], "config": None,
        "notes": [
            f"{len(actuals)} entries saved so far. {total_down:g} min of downtime logged.",
            list_note,
            "The last step (Dispatch) is what finishes the order. Good pieces at earlier "
            "steps are remembered too, so the next plan does not re-schedule work that "
            "is already done. Ticking Mark complete on an entry closes the order.",
        ],
        "tables": tables,
        "error": None, "reached": True,
    }
    return trace


# --------------------------------------------------------------------------- #
# Core: plan the active order book
# --------------------------------------------------------------------------- #
def _orders_table():
    """The Orders tab table, read live from the store.

    ONE definition, shared by GET /orders, every plan response and the rollback
    endpoint — the dashboard must never have a second way to read the book. Note it
    spans the WHOLE book (active **plus** archived), which is a WIDER set than the
    lines the planner schedules: `active_so_lines` drops anything with nothing left
    to make. That gap is what made the plan cache serve a stale order status
    (see `_plan` and `_plan_fingerprint`)."""
    return to_table(orderbook.order_rows(book_store.load_active_orders(),
                                         book_store.load_completed_orders(),
                                         book_store.load_actuals(),
                                         _current_masters()))


def _plan(config: Config):
    # Serve the cached plan when EVERY input is unchanged (the common login/refresh
    # case) — the fingerprint is complete, so a hit is byte-identical to recomputing.
    _fp = _plan_fingerprint(config)
    _cached = _PLAN_CACHE.get("result")
    if _cached is not None and _PLAN_CACHE.get("key") == _fp:
        # …with ONE exception, rebuilt live: the Orders tab table. It is the only
        # part of this response derived from the whole book rather than from the
        # plan, so it can go stale in ways the fingerprint is not built to see.
        # Belt-and-braces on top of the fingerprint fix (live 2026-08-08: a director
        # marked three lines complete and the owner's 20 refreshes still showed them
        # running). Cost is three store reads, deduped by the per-request cache.
        # Same reasoning for `auto_note`: it reports whether a search is running
        # RIGHT NOW, which has nothing to do with the plan being served.
        return {**_cached, "orders": _orders_table(),
                "auto_note": _auto_note_for_display()}

    masters = _current_masters()
    # Fingerprint of the plan-shaping inputs as REQUESTED (base config, before the
    # actuals-driven start advance) — compared against the applied optimization's.
    # Computed on the SAVED (unresolved) config: an auto plan_start_date is None
    # here, so a moving 'today' can never look like a settings change.
    current_inputs_sig = _inputs_signature(config)
    # The response echoes the SAVED (unresolved) config — auto mode (null) must
    # survive the round-trip, or the Settings UI would reflect today back as a
    # FIXED date and the next save would silently lose auto (live 2026-07-18 bug).
    saved_config_dict = config.to_dict()
    # Resolve auto (None) plan start -> today (IST) so the engine never sees None.
    config = _resolve_config(config)
    # What the auto/fixed start resolved to — a separate, display-only response
    # key (the date the plan clock starts from, before any actuals advance).
    resolved_plan_start = config.plan_start_date.isoformat()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    actuals = book_store.load_actuals()
    # Operator absences: physical unavailability, not a promise reservation —
    # reserved in the single plan pass. Lanes never reserve time. Loaded once
    # and reused below for the validation report (ABSENT_OPERATOR_UNKNOWN).
    absences_raw = book_store.load_absences()
    ab = optimize_service.absence_reservations(absences_raw)

    so_lines = orderbook.active_so_lines(active, actuals, masters)   # remaining = ordered − finished good

    # Advance the plan clock past days already worked: once a day's production is
    # punched, the re-plan starts from the NEXT working day's first shift, not the
    # original date (a config COPY so the persisted config keeps its base date).
    eff_start = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                                    masters.calendar)
    if eff_start != config.plan_start_date:
        config = replace(config, plan_start_date=eff_start)

    # Operators from the app-owned table, re-overlaid onto this (already fresh)
    # masters copy so the schedule, Gantt, and analytics all agree. `eff_start`
    # is passed through `operators_as_of` for its (now inert, since rotation was
    # removed 2026-08-05) `as_of` parameter — the shifts returned are simply
    # whatever is on file in Settings, the same for any date. Pure view —
    # nothing persisted.
    op_table = book_store.load_operator_table()
    if op_table:
        masters = replace(masters, operators=operator_master.operators_as_of(
            op_table, eff_start))

    # An applied Optimize run (the saved rank map) is replayed as ONE pass over
    # every active line. Lanes (open/committed) are pure status labels —
    # they have no scheduling effect (owner pivot, 2026-07-16). No saved
    # optimization -> plans are byte-identical to the pre-optimize plan.
    prio = book_store.load_plan_priority()
    ranks = prio["ranks"] if prio else None
    frozen = book_store.load_frozen_ops()

    # The optimized ranks ARE the prioritization. The Expedite window dynamically
    # re-sorts ops by slack at schedule time, which would OVERRIDE the ranks and cancel
    # the whole optimization out (the sequence stops mattering). So the ranked orders
    # are planned in the pure non-delay model (expedite off) — the optimizer found the
    # best order globally, which supersedes expedite's local tie-break. No ranks ->
    # config unchanged (expedite behaves exactly as the Settings tick sets it).
    ranked_config = replace(config, expedite_window_min=0) if ranks else config

    # ONE pass over all active lines. Operator absences (physical unavailability)
    # reserve time; lanes never do. The feedback loop is quantity-only: recorded
    # times (downtime, actual setup) are stored for the record, never scheduled.
    plan_run = PlanRun(so_lines=so_lines)
    trace = run_forward(plan_run, ranked_config, masters, reserved=ab or None,
                        priority_rank=ranks, frozen=frozen or None)

    _augment_helpers(trace, plan_run, config, masters, actuals=actuals)

    # Rule 8 tab: the active order book by remaining qty. List EVERY active order
    # so the count matches the Orders tab; flag the ones with nothing left to make
    # (fully produced but not yet marked complete) — they aren't scheduled, which
    # is why they don't appear in the schedule/Gantt.
    finished = orderbook.finished_good_by_order(actuals, masters)
    started = orderbook.orders_with_actuals(actuals)
    # Status keyed by each order's (SO#, item) pair — an SO# alone isn't unique.
    status_by_order = {o.key: orderbook.derive_status(o, started) for o in active.values()}

    # Keys that ACTUALLY landed on the schedule (so the "In this plan" column tells the
    # truth): an order with work remaining that is absent from the schedule was held out
    # — no routing, or an in-house step with no machine that has a qualified operator —
    # so it must NOT be labelled "scheduled".
    scheduled_keys = {(r, e.item_code) for e in plan_run.schedule
                      for r in (e.so_refs or [])}

    def _r8_row(o):
        remaining = max(o.ordered_qty - finished.get(o.key, 0.0), 0.0)
        if remaining <= 0:
            in_plan = "no, fully produced, mark complete"
        elif o.key in scheduled_keys:
            in_plan = "scheduled"
        else:
            in_plan = ("not scheduled — a step has no routing or no machine with a "
                       "qualified operator; it will schedule once the master is completed")
        return {"SO No": o.so_no, "Item Code": o.item_code, "Remaining Qty": remaining,
                "SO Delivery Date": fmt_date(o.delivery_date),
                "Status": status_by_order[o.key],
                "In this plan": in_plan}

    # Sort by the real date (not the DD-MM-YYYY display string).
    r8_rows = [_r8_row(o) for o in sorted(active.values(),
                                          key=lambda o: (o.delivery_date, o.so_no, o.item_code))]
    scheduled = sum(1 for r in r8_rows if r["In this plan"] == "scheduled")
    trace["rule8"] = {
        "input": to_table([{"Active orders": len(active),
                            "Scheduled (work remaining)": scheduled,
                            "Actuals applied": len(actuals)}]),
        "output": to_table(r8_rows),
        "config": config.to_dict(),
        "notes": [
            "Unified Plan: every active order is listed at its remaining qty "
            "(ordered − finished good at the DISPATCH/last-step gate). Production "
            "still mid-routing (WIP) does not reduce it. Completed orders are excluded.",
            "An order fully produced but not yet marked complete shows Remaining 0 "
            "and 'In this plan = no': it isn't scheduled until you tick 'mark "
            "complete' on a Rule 7 entry to archive it.",
            "An order with work remaining but 'In this plan = not scheduled' was held "
            "out because a process step has no routing or no machine with a qualified "
            "operator. Complete the master (add the routing / assign an operator) and it "
            "schedules automatically on the next plan — it is never lost or completed.",
        ],
        "error": None, "reached": True,
    }

    run_id = uuid.uuid4().hex[:8]
    _store_run(run_id, trace)
    gantt = build_gantt(plan_run.schedule, plan_run.batches_prioritized, masters,
                        status_by_order=status_by_order)
    orders = _orders_table()

    # Expected completion per order (SO#, item), keyed "SO\x1fitem" — from the ONE
    # shared definition (optimizer.expected_completion) the Gantt, the delay report
    # and plan_metrics also use, so no two surfaces can publish different dates.
    exp_end = {f"{so}{KEY_SEP}{item}": d.isoformat()
               for (so, item), d in optimizer.expected_completion(plan_run.schedule).items()}

    # Staleness info for the Optimize banner: how many of the currently planned
    # orders the applied optimization covers (uncovered = uploaded after it ran).
    optimize_meta = {"active": False}
    if prio:
        keys = {f"{l.so_no}{KEY_SEP}{l.item_code}" for l in so_lines}
        covered = sum(1 for k in keys if k in prio["ranks"])
        saved_sig = (prio.get("meta") or {}).get("inputs_sig")
        # Delivery-date staleness. Compare only keys present in BOTH the applied
        # snapshot and the current book: an order that has since completed or been
        # newly uploaded is normal traffic, not a reason to cry stale.
        saved_dates = (prio.get("meta") or {}).get("dates") or {}
        current_dates = {f"{l.so_no}{KEY_SEP}{l.item_code}":
                         l.delivery_date.isoformat() if l.delivery_date else None
                         for l in so_lines}
        dates_moved = sum(1 for k, v in saved_dates.items()
                          if k in current_dates and current_dates[k] != v)
        optimize_meta = {"active": True,
                         "saved_at": (prio.get("meta") or {}).get("saved_at"),
                         "covered": covered, "uncovered": len(keys) - covered,
                         # True when the masters/settings differ from what the
                         # optimization was computed on — its numbers will differ.
                         "inputs_changed": bool(saved_sig and saved_sig != current_inputs_sig),
                         "dates_changed": dates_moved > 0,
                         "dates_changed_count": dates_moved}
        # Reconcile what the applied optimization TARGETED against what THIS plan
        # actually achieves. They match under normal operation (ranks replay to the
        # same plan), but a change the inputs signature can't see — a code deploy
        # between the contest and now, a cloud/local parity gap, an untracked input —
        # can leave the live plan worse than the number the Optimize panel showed.
        # Surface both (+ a `diverged` flag) so that gap is never silent again.
        _target = (prio.get("meta") or {}).get("best") or {}
        _ach = optimizer.plan_metrics(
            plan_run.schedule, so_lines, config.plan_start_date,
            promise_slack_days=getattr(config, "committed_promise_slack_days", 3))

        def _num(d, k):
            v = d.get(k)
            return round(float(v), 1) if isinstance(v, (int, float)) else None
        _t_ms, _a_ms = _num(_target, "makespan_days"), _num(_ach, "makespan_days")
        _t_late, _a_late = _num(_target, "total_late_days"), _num(_ach, "total_late_days")
        # Flag only when the live plan is meaningfully WORSE than the target (a
        # better-than-target plan is no problem). Tolerances swallow a 1-day
        # effective-start drift; the real gaps (this one was 4 days) trip it.
        diverged = bool(_target) and (
            (_t_ms is not None and _a_ms is not None and _a_ms - _t_ms >= 2.0)
            or (_t_late is not None and _a_late is not None and _a_late - _t_late >= 5.0))
        optimize_meta.update(
            target={"makespan_days": _t_ms, "total_late_days": _t_late},
            achieved={"makespan_days": _a_ms, "total_late_days": _a_late},
            diverged=diverged)

    result = {"run_id": run_id, "trace": trace,
              "report": _report_for_book(masters, so_lines, absences=absences_raw,
                                         config=config, schedule=plan_run.schedule,
                                         batches=plan_run.batches_prioritized),
              "gantt": gantt, "orders": orders,
              # SAVED (unresolved) config — null plan_start_date = auto survives
              # the round-trip; the resolved start is a separate display key.
              "config": saved_config_dict,
              "resolved_plan_start": resolved_plan_start,
              "expected_end": exp_end, "optimize_meta": optimize_meta,
              "auto_note": _auto_note_for_display()}
    # The raw artifacts of THIS run, cached alongside the response under the same
    # fingerprint. Downloads that need the schedule itself (the delay justification
    # report) read these instead of planning again — a second plan is a second set of
    # dates (live 2026-08-07: Gantt 07-Sep vs delay report 04-Sep for one order).
    _PLAN_CACHE.update(key=_fp, result=result,
                       artifacts={"plan_run": plan_run, "so_lines": so_lines,
                                  "masters": masters, "config": config})
    return result


def _commit_orders(pairs):
    """Snapshot each order's current expected completion (from a fresh plan) as
    its promised date, and lock it into the committed lane. Unknown (so, item)
    pairs are silently skipped — set_commitment() returns False for them."""
    from datetime import date as _date
    result = _plan(_load_plan_config())
    exp = result.get("expected_end", {})
    now = _ist_now().isoformat(timespec="seconds")
    for so, item in pairs:
        iso = exp.get(f"{so}\x1f{item}")
        promised = _date.fromisoformat(iso) if iso else None
        book_store.set_commitment(so, item, "committed", promised, now)


def _load_plan_config() -> Config:
    """The admin's last-saved plan config, or defaults. Never raises: a missing,
    unparseable, or invalid stored value falls back to ``Config()`` so a read
    endpoint can't be 500'd by a bad stored config.

    Engine selection: the code default is ``classic`` (the kept engine the test suite
    validates). Production runs the new operator-stable engine by setting the env var
    ``DEFAULT_SCHEDULER=new`` — applied only when the admin hasn't explicitly saved a
    scheduler choice, so an explicit Settings choice always wins and tests (no env) stay
    on classic."""
    raw = book_store.load_plan_config()
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
    try:
        cfg = Config.from_dict(saved)
        cfg.validate()
    except Exception:
        cfg = Config()
    # DEFAULT_SCHEDULER is the DEPLOY-level engine selector and is AUTHORITATIVE: it
    # overrides any scheduler stored in the saved config, so switching the deployed engine
    # never requires clearing an old MongoDB config (which pins the retired flow/classic).
    # Normalised for stray whitespace/case. Unset (e.g. in tests) -> the saved/Config value.
    env_sched = (os.environ.get("DEFAULT_SCHEDULER") or "").strip().lower()
    # "roster" belongs here as much as the others: a value missing from this
    # whitelist is not an error, it is silently IGNORED — the deploy keeps running
    # the saved/default engine while the env var says otherwise.
    if env_sched in ("new", "classic", "flow", "roster"):
        cfg.scheduler = env_sched
    return cfg


# --------------------------------------------------------------------------- #
# Optimize: search the current book for a better batch sequence (admin action).
# One job at a time, in a background thread (Render free tier = 1 worker); the
# job snapshots its inputs at start so later book edits can't race it. Only the
# APPLIED result is durable (book_store.save_plan_priority) — a process restart
# mid-run just returns the job to idle, the applied ranks are unaffected.
# --------------------------------------------------------------------------- #
# Evaluation counts — deterministic. One option (owner cap, 2026-07-15): 1,000
# plans TOTAL per click (~2.3 s/plan on the free 0.1-CPU Render tier ≈ 40 min),
# split equally across the overlap contenders by sweep_optimize. Legacy "quick"
# (a cached client's request) runs the same single budget. The user can Stop any
# run and keep the best plan found so far.
_OPT_BUDGETS = {"deep": 1000, "quick": 1000}
_OPT_SEED = 42

# How long a Stop waits for live cloud workers to hear about it and post their
# best-so-far before we finalize without them. Workers heartbeat every
# PROGRESS_EVERY_S = 5.0s (scripts/cloud_optimize_worker.py) and learn about a
# cancel from that response, so ending the job on the first 2-second poll — as an
# earlier cut of this fix did — kills it before any worker can answer: their next
# heartbeat 404s, they never stop, and their results are dropped. Measured: that
# turned a Stop that yielded a real plan into one that yielded nothing.
# 90s is ~18 heartbeat cycles, and still bounded far below the 40-minute deadline.
_CANCEL_GRACE_S = 90.0

_OPTIMIZE = {"state": "idle", "label": None, "budget_evals": 0, "evals": 0,
             "searched_plan_start": None,
             "baseline": None, "best": None, "error": None, "elapsed_s": 0.0,
             "started_mono": None, "result": None, "cancel": False,
             "mode": "local", "job_id": None, "cloud_payload": None,
             "cloud_failed": False, "base_config": None, "auto": False,
             "claimed": False, "shards": {}, "shard_total": None,
             "shards_finalizing": False, "shard_evals": {}}
_OPTIMIZE_LOCK = threading.Lock()

# Identity of THIS server process. A contest lives only in `_OPTIMIZE` above, so a
# restart or a Render free-tier spin-down kills it with nothing written down. Notes
# about a running search carry this token; when it no longer matches, the search is
# known to have died and the Orders tab says so (see `_auto_note_for_display`).
_PROCESS_TOKEN = uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Cloud compute (GitHub Actions) — the full 2,400-plan contest runs on a free
# GitHub runner (2 vCPU ≈ 40× the Render instance) in ~8-10 min. Enabled by two
# env vars on Render; with either missing, Optimize computes locally exactly as
# before. The worker authenticates with OPTIMIZE_WORKER_SECRET; if the cloud
# never answers, a watchdog falls back to local compute so the button always
# works. Spec: docs/superpowers/specs/2026-07-15-optimize-settings-sweep-design.md.
# --------------------------------------------------------------------------- #
# The repo this deployment mirrors. Dispatching a contest into it would burn the
# live site's Actions minutes and — because that repo's secrets point APP_URL at
# the live app — post results back to production. That is the same clause-2
# violation as a database write, reached by a different road, so it is refused
# outright rather than merely defaulted away from.
UPSTREAM_GITHUB_REPO = "riittiin/anvitech-ppc-engine"
OWN_GITHUB_REPO = "riittiin/anvitech-ppc-duplicate"


def _cloud_config():
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
    secret = os.environ.get("OPTIMIZE_WORKER_SECRET", "").strip()
    if not token or not secret:
        return None
    repo = os.environ.get("GITHUB_REPO", OWN_GITHUB_REPO).strip() or OWN_GITHUB_REPO
    if repo == UPSTREAM_GITHUB_REPO:
        # Misconfiguration, not a request to honour. Returning None disables the
        # cloud tier, so Optimize still works — it just computes locally.
        return None
    return {"token": token, "secret": secret,
            "repo": repo,
            "workflow": os.environ.get("OPTIMIZE_WORKFLOW", "optimize.yml"),
            # 40, not 20: flow-scheduler evals are ~5x slower than classic, so the
            # GitHub Actions runner needs ~25 min for a full flow contest. At 20 the
            # cloud (fast, 2 vCPU) got cut off ~79% done and fell back to Render's
            # 0.1-CPU local compute (live 2026-07-19). 40 lets GitHub always finish
            # first, so the slow local path is never hit. GitHub cost stays $0 (free
            # minutes, spending cap 0); a run is ~25 min, far under the 2000/month.
            "timeout_min": float(os.environ.get("OPTIMIZE_CLOUD_TIMEOUT_MIN", "40"))}


def _dispatch_workflow(cloud, job_id) -> bool:
    """Trigger the optimize workflow on GitHub (workflow_dispatch). Stdlib only.
    Token "manual" skips the GitHub call — the operator starts the worker
    themselves (local E2E testing, or any non-GitHub compute box)."""
    if cloud["token"] == "manual":
        return True
    import urllib.request
    url = (f"https://api.github.com/repos/{cloud['repo']}"
           f"/actions/workflows/{cloud['workflow']}/dispatches")
    body = json.dumps({"ref": "main", "inputs": {"job_id": job_id}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {cloud['token']}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "anvitech-ppc",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 204
    except Exception:
        return False


def _worker_secret_ok(request: Request) -> bool:
    secret = os.environ.get("OPTIMIZE_WORKER_SECRET", "")
    given = request.headers.get("x-worker-secret", "")
    try:
        return bool(secret) and hmac.compare_digest(secret.encode("utf-8"), given.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Feedback trigger (spec 2026-07-22, replaces the Mon/Fri GitHub-cron design
# of 2026-07-18-scheduled-optimize): the job order re-optimizes when POST
# /optimize/done fires — the "Done entering — update plan" button, reachable
# by either role. Admin mutations (uploads, commit/uncommit, deletes,
# Settings saves) still never start a contest on their own; absences are
# covered in tests/test_absences_api.py. AUTO_OPTIMIZE=0 is internal test
# isolation only — never user-facing.
# --------------------------------------------------------------------------- #
def _auto_enabled() -> bool:
    return os.environ.get("AUTO_OPTIMIZE", "1") != "0"


def _ist_now():
    """Server runs UTC; the auto notes reference a named local (IST) clock."""
    from datetime import datetime as _dt, timedelta as _td
    return _dt.utcnow() + _td(hours=5, minutes=30)


def _ist_today():
    """Today's date on the named local (IST) clock — the LIVE plan start.
    Every date the app reasons from (plan clock, rotation, first-seen) follows
    this, not a frozen test-era date (go-live 2026-07-19)."""
    return _ist_now().date()


def _ceil_next_hour(dt):
    """Round ``dt`` UP to the next full hour (minutes/seconds zeroed). 23:30 -> next day
    00:00. Used to floor an auto plan start so a run never schedules in the past."""
    from datetime import timedelta as _td
    return dt.replace(minute=0, second=0, microsecond=0) + _td(hours=1)


def _stamp_plan_clock() -> str:
    """Start a new plan clock at the next full hour (IST) and persist it; returns the
    ISO floor. Called when an optimization finishes (`_finalize_optimize`) and on the
    first plan of a day that has not had one. Between stamps every planning entry reads
    the stored value, so all features plan from the same instant — see `_resolve_config`.

    Never raises: if the store write fails the floor is still returned and used for this
    run, which is no worse than the pre-2026-08-07 per-call behaviour."""
    now = _ist_now()
    floor = _ceil_next_hour(now).isoformat(timespec="minutes")
    try:
        book_store.save_plan_start_floor(now.date().isoformat(), floor)
    except Exception:  # noqa: BLE001 — a pin failure must not break planning
        pass
    return floor


def _resolve_config(config: Config) -> Config:
    """Resolve an 'auto' plan start (plan_start_date is None) to today (IST) so
    the pure engine NEVER sees None. Called at every planning entry; a config
    that already carries an explicit date is returned unchanged. The SAVED config
    keeps None — this resolution is per-run only, never persisted."""
    # Consolidation is engine-decided (research 2026-07-24: a 1-day window beats the old
    # 10-day default by ~6% on the Judge — batching orders up to 10 days apart delays the
    # earlier-due one). The owner no longer sets it; force it every run so a stale saved
    # value can't reintroduce the regression.
    if config.consolidation_window_days != 1:
        config = replace(config, consolidation_window_days=1)
    if config.plan_start_date is None:
        # Auto: resolve to today. The plan then starts at 08:00 of that date — the shift
        # start — whatever time of day this runs (owner decision 2026-08-11; see
        # PLAN_START_NEXT_HOUR at the top of this file). No floor means the engine's
        # max(08:00-of-date, floor) degenerates to 08:00, and the start cannot drift
        # within the day at all.
        if not PLAN_START_NEXT_HOUR:
            return replace(config, plan_start_date=_ist_today(), plan_start_floor=None)
        # --- PLAN_START_NEXT_HOUR only, below ---
        # Floor the plan at the next full hour (IST), so a run late in the day never
        # schedules from 08:00 that (past) morning. The engine starts at
        # max(08:00-of-date, floor). See engine/new_engine._plan_config.
        #
        # The floor is a STORED PLAN CLOCK, not a per-call `now()` (2026-08-07). It shifts
        # the engine's plan start, and the scheduler is a greedy dispatcher, so it does not
        # merely offset the plan — it re-decides it: on the real book a 6-hour difference
        # re-sequenced 54 of 68 orders and moved completion dates by up to 24 days.
        # Recomputing it per call meant the Gantt and a report pulled an hour later were
        # DIFFERENT PLANS from identical inputs (the live 07-Sep vs 04-Sep bug).
        #
        # It advances at exactly two events, both discrete and visible:
        #   * an optimization FINISHES  -> `_stamp_plan_clock`, next full hour (owner's
        #     rule: a contest landing 09:01 Mon makes the plan start 10:00 Mon)
        #   * the first plan of a NEW day, when no contest has run yet that day
        # Between those it holds, so every feature reports the same dates.
        today = _ist_today()
        pinned = book_store.load_plan_start_floor()
        if pinned and pinned.get("date") == today.isoformat() and pinned.get("floor"):
            floor = pinned["floor"]
        else:
            floor = _stamp_plan_clock()
        return replace(config, plan_start_date=today, plan_start_floor=floor)
    # Fixed date (testing/reproducibility): start at 08:00 of that date; clear any stale floor.
    if config.plan_start_floor is not None:
        config = replace(config, plan_start_floor=None)
    return config


def _current_book_sig() -> str:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      actuals, masters)
    absences = book_store.load_absences()
    return optimize_service.book_signature(lines, absences=absences)


def _current_plan_start_sig() -> str:
    """The date the plan clock actually starts from, as an ISO string.

    The THIRD fingerprint of "has anything changed since the last search", next to
    the book (quantities) and the inputs (settings). Neither of those moves when
    the calendar does: `_current_book_sig` is quantity-derived, and
    `_inputs_signature` reads the SAVED config, where an auto start is None on
    purpose so a moving 'today' never looks like a settings change.

    Without this, any day the start advances with no new quantities — a Monday
    after the weekend, a holiday, pressing "Done entering" before punching — was
    answered "nothing new to re-plan", and a sequence optimized for an older start
    date kept being replayed. Measured on the live book: ranks computed 2026-08-11
    gave 397 late-days against the 08-12 book, where re-running the same optimizer
    on 08-12 gave 365.
    """
    config = _load_plan_config()
    start = config.plan_start_date or _ist_now().date()
    eff = orderbook.effective_plan_start_date(
        book_store.load_actuals(), start, _current_masters().calendar)
    return eff.isoformat()


def _matches_last_search(record, book_sig, inputs_sig, plan_start) -> bool:
    """Does ``record`` (applied meta, or the last completed search) describe a
    contest run against exactly today's book, settings AND plan start?

    A missing ``inputs_sig`` is tolerated — records written before that field
    existed matched on the book alone, and this must not change that path. A
    missing ``plan_start`` is NOT tolerated: it means the record predates this
    fingerprint, so we cannot know which date it was built for. Re-planning once
    is the safe answer, and the next search writes the field.
    """
    if not record:
        return False
    if record.get("book_sig") != book_sig:
        return False
    rec_inputs = record.get("inputs_sig")
    if rec_inputs is not None and rec_inputs != inputs_sig:
        return False
    return record.get("plan_start") == plan_start


def _delivery_dates() -> dict:
    """Every active order's delivery date, keyed like the optimizer's ranks.

    Stored alongside an applied optimization so a later plan can tell whether the
    delivery dates have moved since it was computed (a director re-importing the
    Excel with a revised date). A plain map rather than a hash: it costs ~3 KB for
    a 70-order book and lets the banner say HOW MANY orders moved."""
    return {f"{o.so_no}{KEY_SEP}{o.item_code}": o.delivery_date.isoformat()
            for o in book_store.load_active_orders().values()
            if not o.completed and o.delivery_date}


def _plan_fingerprint(config: Config) -> str:
    """A hash of EVERY input that determines the plan output, so the plan cache serves
    a stored result only when it would be byte-identical to a fresh compute. Covers:
    the book (orders + actuals + absences, via `_current_book_sig`), the masters
    workbook (`_masters_sha`), the app-owned operator table, the FULL resolved config
    (all scheduling knobs + the resolved plan-start date, which moves with 'today'),
    and any applied optimization ranks. Any change flips the fingerprint → recompute;
    nothing changed → instant cache hit. All the store reads here are deduped by the
    per-request cache, so this is cheap relative to the plan compute it can skip."""
    parts = {
        "book": _current_book_sig(),
        # The order book AS DISPLAYED — active PLUS archived, every field. `book`
        # above is built from `active_so_lines`, which SKIPS any order with nothing
        # left to make, so an order that was already fully produced was invisible
        # here: marking it complete (or editing it) changed no input the cache could
        # see, and the stale response kept publishing the old Orders and Rule 8 tabs.
        # Live 2026-08-08: a director marked three (SO#, item) lines complete in the
        # office and the owner at home refreshed ~20 times still seeing them running.
        # The PLAN was right either way (a finished order contributes no work) — only
        # the display was stale, and the display spans a wider book than the planner.
        "book_rows": hashlib.sha256(json.dumps(
            [o.to_json() for o in sorted(
                list(book_store.load_active_orders().values())
                + list(book_store.load_completed_orders().values()),
                key=lambda o: o.key)],
            sort_keys=True, default=str).encode("utf-8")).hexdigest(),
        "masters": _masters_sha(),
        "operators": book_store.load_operator_table(),
        "config": _resolve_config(config).to_dict(),
        "ranks": (book_store.load_plan_priority() or {}).get("ranks"),
        # The frozen (in-progress) set pins certain ops to their already-started
        # machine/operator — a freeze change must bust the cache, or a stale plan
        # (computed before the freeze) would keep being served.
        "frozen": book_store.load_frozen_ops(),
        # NOT the Orders-tab note. It is DISPLAY, not a plan input, so it is rebuilt
        # on every cache hit by `_auto_note_for_display()` instead of being keyed on
        # here. Keying on it threw away a perfectly good plan every time a status
        # message changed — and those messages land while a contest is running, i.e.
        # exactly when Render's free CPU has none to spare (2026-08-09).
        # FULL actuals content, plan-cache only (NOT book_signature — a net-zero-good
        # punch must not fire the Thursday optimize trigger). `_current_book_sig` is
        # quantity-derived, so a downtime-only / all-reject / rolled-back entry leaves
        # it unchanged even though it advances the effective plan start (→ the schedule)
        # and the Rule 7 downtime tab. Digesting every actual closes that stale window.
        "actuals": hashlib.sha256(json.dumps(
            [a.to_json() for a in book_store.load_actuals()],
            sort_keys=True, default=str).encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _hhmm() -> str:
    return _ist_now().strftime("%H:%M")


def _who(by: str) -> str:
    """'ravi ' / '' — so the owner can see WHO on the floor pressed the button."""
    return f"{by} " if by else ""


def _auto_note_write(text: str, *, running: bool = False):
    """Write the one-line status the Orders tab shows to BOTH roles.

    `running=True` marks a search that is still in flight, and stamps the process
    that owns it. A contest lives in `_OPTIMIZE`, which is process memory only, so
    a Render restart (every deploy) or a free-tier spin-down erases it with no
    trace — the stamp is what lets `_auto_note_for_display` notice afterwards and
    say so instead of leaving 'searching…' on screen forever."""
    note = {"text": text, "at": _ist_now().isoformat(timespec="seconds")}
    if running:
        note.update(running=True, process=_PROCESS_TOKEN)
    book_store.save_auto_note(note)


def _auto_note_for_display():
    """The stored note, corrected for what this process can actually still see.

    A note that claims a search is running, written by a process that is gone (or
    by this one before the contest vanished from `_OPTIMIZE`), is a lie — say it
    was interrupted and tell the operator what to do. Display-only: deliberately
    NOT in `_plan_fingerprint`, and rebuilt on every cache hit."""
    note = book_store.load_auto_note()
    if not note or not note.get("running"):
        return note
    if note.get("process") == _PROCESS_TOKEN:
        # Same process: either it really is still searching, or it has just finished
        # and the result note is milliseconds away. Both are fine — say nothing. A
        # thread that dies instead writes its own failure note.
        return note
    return {**note, "running": False,
            "text": (note.get("text", "") + "  ⚠ This update was INTERRUPTED (the "
                     "server restarted or went to sleep) and did not finish. The "
                     "plan is unchanged — press \"Done entering: update plan\" "
                     "again to restart it.")}


def _applied_plan_meta():
    """The meta dict recorded against the last applied optimization, read
    directly (not via book_store.load_plan_priority(), which treats an empty
    ranks dict as "nothing applied" for planning purposes — a different concern
    from "does the last contest still match today's book/inputs"). ``None`` when
    nothing has ever been applied."""
    raw = book_store.get_store().kv_get(book_store.PLAN_PRIORITY_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("meta") or {}


def _compute_and_store_frozen() -> list:
    """Derive the frozen (in-progress) set from the last-applied plan + the punches and
    persist it (anvitech:frozen_ops). Machine/operator from the applied plan; remaining
    qty from the punches. Empty when nothing is in progress or no plan is on file yet."""
    from collections import defaultdict
    masters = _current_masters()
    actuals = book_store.load_actuals()
    active = book_store.load_active_orders()
    so_lines = orderbook.active_so_lines(active, actuals, masters)
    applied = book_store.load_last_applied_schedule()
    good_by_step = defaultdict(float)
    for a in actuals:
        good_by_step[(a.so_no, a.item_code, loaders.normalize_process_name(a.process))] += a.good_qty()
    rows = freeze.compute_frozen_set(applied, so_lines, dict(good_by_step), masters)
    book_store.save_frozen_ops(rows)
    return rows


def _try_start_auto(by: str = "") -> bool:
    """Start an auto-applying re-optimization if it makes sense. Invoked by
    POST /optimize/done (the 'Done entering — update plan' button). Returns True
    iff a contest was started. Returns False — starting nothing — when auto is
    disabled, a contest is already running, or NOTHING material changed since the
    last applied plan (book + inputs fingerprint match; a friendly note is written
    in that case). Unlike the removed Mon/Fri cron this is NOT cloud-only:
    _start_optimize falls back to local compute, so the button always does
    something even with no cloud configured."""
    if not _auto_enabled():
        return False
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            return False                         # one contest at a time
    # Skip only when NOTHING material changed since the last applied plan —
    # "material" includes the inputs fingerprint (masters + settings + operator
    # rotation/edits), not just the book. A legacy applied meta without an
    # inputs_sig doesn't force a run on the missing field alone. Also skip when
    # a contest already SEARCHED this exact book+inputs and found nothing worth
    # applying (or nothing has ever been applied) — otherwise a redundant Done
    # click re-runs the full contest every time.
    try:
        cur_book = _current_book_sig()
        cur_inputs = _inputs_signature(_load_plan_config())
        # The plan START is the third thing a contest is run against. Leaving it out
        # meant a new day looked like "nothing changed" (see _current_plan_start_sig).
        cur_start = _current_plan_start_sig()
        applied_match = _matches_last_search(_applied_plan_meta(), cur_book,
                                             cur_inputs, cur_start)
        searched_match = _matches_last_search(book_store.load_last_searched(),
                                              cur_book, cur_inputs, cur_start)
        if applied_match or searched_match:
            _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()}: nothing "
                             "new to re-plan since the last update, so the plan is "
                             "unchanged. Enter today's production first, then press it.")
            return False                         # nothing material changed
    except Exception as e:  # noqa: BLE001
        # NEVER silent. This used to be a bare `return False`: the floor pressed the
        # button, nothing happened, and the owner 10 km away had no way to find out.
        _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()} but the plan "
                         f"update could NOT start: {e}. The plan is unchanged — please "
                         "try again, and tell your admin if it keeps happening.")
        return False
    try:
        _start_optimize(_OPT_BUDGETS["deep"], "auto", background=True, auto=True)
    except HTTPException as e:                   # e.g. nothing to optimize
        _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()} but the plan "
                         f"update could NOT start: {e.detail}. The plan is unchanged.")
        return False
    _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()}: searching for a "
                     "better schedule now. This takes 15 to 30 minutes; the new plan "
                     "appears here by itself when it lands.", running=True)
    return True


def _start_optimize(budget_evals: int, label: str, background: bool = True,
                    auto: bool = False):
    """Snapshot the book + config and run the settings-sweep contest. ONE pool
    over every active line — lanes (Open/Committed) are status labels
    with no scheduling effect. Operator absences are reserved as blocked
    machine/operator intervals, same as in a normal Plan. Cloud-configured →
    dispatch the full contest to GitHub Actions; otherwise (or on any cloud
    failure) compute locally."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            raise HTTPException(status_code=409,
                                detail="an optimization is already running")

        # Recompute the frozen (in-progress) set from the latest punches +
        # last-applied plan right before optimizing — both the Done/auto path
        # and the admin's manual "Start deep search" now honour it (owner
        # decision, 2026-07-29: deep-search is pressed in week 2 when week-1
        # work is already running). Doing this inside the lock (rather than
        # in the caller) also closes a race between the read and the write.
        _compute_and_store_frozen()

        config = _load_plan_config()
        masters = _current_masters()
        base_config = config      # as saved (auto -> None) — the inputs fingerprint basis
        # Flow evals are ~15s each on the free instance — a full local search would
        # run for hours. Cap the LOCAL-path budget hard for flow (the cloud path
        # sizes itself via cloud_candidates); classic keeps its full budget.
        if getattr(base_config, "scheduler", "classic") == "flow":
            budget_evals = min(budget_evals, optimizer.FLOW_LOCAL_BUDGET)
        # The new engine's local fallback is budget-capped so it finishes on the free tier
        # (the Stop button also applies); the cloud path runs via the worker when available.
        new_engine_run = getattr(base_config, "scheduler", "classic") == "new"
        if new_engine_run:
            budget_evals = min(budget_evals, 200)
        config = _resolve_config(config)   # None -> today (IST) for the engine/contest
        # Worst-order ceiling: the current plan's worst lateness. The search barrier +
        # apply backstop use it so a re-optimization never pushes any order past today's
        # worst-case. base_config (the fingerprint basis) is intentionally left without it.
        try:
            _ceiling = _incumbent_metrics().get("max_late_days")
        except Exception:  # noqa: BLE001 - a ceiling failure must never block optimizing
            _ceiling = None
        if _ceiling is not None:
            config = replace(config, worst_ceiling_days=float(_ceiling))
        actuals = book_store.load_actuals()
        orders = book_store.load_active_orders()
        absences = book_store.load_absences()
        operator_table = book_store.load_operator_table()
        # The frozen (in-progress) set now restricts BOTH paths — the Done/auto
        # button and the admin's manual "Start deep search" (owner decision,
        # 2026-07-29). It was just recomputed above from the latest punches +
        # last-applied plan, so it's naturally empty on a fresh import with
        # nothing in progress yet — manual deep-search stays fully unrestricted
        # then, which is correct.
        frozen = book_store.load_frozen_ops()
        try:
            setup = optimize_service.prepare_contest(orders, actuals, masters, config,
                                                     absences=absences,
                                                     operator_table=operator_table,
                                                     frozen=frozen)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # The new engine now runs its contest on GitHub Actions too (the worker seeds its
        # masters from the payload workbook). If the cloud run fails/times out, the watchdog
        # falls back to the in-process sweep (budget-capped above) — so the button always works.
        cloud = _cloud_config()
        job_id = uuid.uuid4().hex
        if cloud:
            # The contest lineup must match the scheduler MODE: chunk counts under
            # flow, overlap % under classic. Passing the wrong list would make the
            # worker replace flow_chunks with 60..95 (invalid — validate caps at 50)
            # and the whole cloud contest would error out, never running in flow mode.
            _cands = optimize_service.cloud_candidates(setup.search_config)
            _bpc = optimize_service.cloud_budget(setup.search_config)
            # Extra RNG seeds to multi-start the search from. Empty (the default)
            # = one seed = the contest exactly as it ran before. The search is an
            # iterated local search, so its answer depends on the random stream:
            # at a fixed overlap on the live book, three seeds spread 24 late-days.
            _seeds = optimize_service.cloud_seeds()
            payload = optimize_service.build_payload(
                orders, actuals, book_store.load_masters_bytes(), config,
                seed=_OPT_SEED, candidates=_cands, budget_per_candidate=_bpc,
                absences=absences, operator_table=operator_table, frozen=frozen,
                seeds=_seeds)
            # Use contest_jobs (not sweep_contenders) for the true candidate
            # count: under the new engine the contest also sweeps the
            # machine-set dimension (Allotted-only vs Allotted+Suggested), so
            # the sharded workers report 2x the (overlap-only) contender
            # count — sweep_contenders alone made the bar overshoot to ~200%.
            denom = _bpc * len(optimize_service.contest_jobs(payload))
        else:
            payload = None
            _knob, _kcands = optimizer.knob_for(setup.search_config)
            _mult = 2 if getattr(setup.search_config, "scheduler", "classic") == "new" else 1
            denom = _mult * optimizer.sweep_total_evals(
                budget_evals, getattr(setup.search_config, _knob), _kcands)

        # Snapshot the book/inputs fingerprints AT START — a punch that lands
        # mid-run must still force a re-run on the next Done click, even though
        # this contest is already in flight against the pre-punch book.
        searched_book_sig = _current_book_sig()
        searched_inputs_sig = _inputs_signature(base_config)
        searched_plan_start = _current_plan_start_sig()
        _OPTIMIZE.update(state="running", label=label, budget_evals=denom,
                         evals=0, baseline=None, best=None, error=None, result=None,
                         elapsed_s=0.0, started_mono=time.monotonic(), cancel=False,
                         mode=("cloud" if cloud else "local"), job_id=job_id,
                         cloud_payload=payload, cloud_failed=False, claimed=False,
                         base_config=base_config, auto=bool(auto),
                         searched_book_sig=searched_book_sig,
                         searched_inputs_sig=searched_inputs_sig,
                         searched_plan_start=searched_plan_start,
                         shards={}, shard_total=None, shards_finalizing=False,
                         shard_evals={})

    def local_job():
        try:
            def on_progress(evals, best):
                _OPTIMIZE["evals"] = evals
                _OPTIMIZE["best"] = best

            # Honest baseline = the admin's CURRENT plan — the applied optimized
            # sequence replayed, NOT the book with no sequence at all (2026-08-07: that
            # showed a plan the owner did not have, and disagreed with the auto-note's
            # "was N" for the same instant). `_incumbent_metrics` is the one definition.
            real_baseline = _OPTIMIZE.get("baseline")
            if real_baseline is None:
                try:
                    real_baseline = _incumbent_metrics(with_distribution=True)
                except Exception:  # noqa: BLE001 — fall back to the unranked book
                    base_sched, base_lines = _all_lines_schedule(setup, setup.masters, None)
                    real_baseline = optimizer.plan_metrics(
                        base_sched, base_lines, setup.config.plan_start_date,
                        with_distribution=True,
                        promise_slack_days=getattr(setup.config, "committed_promise_slack_days", 3))

            # One pool: search ALL active lines together (lanes are status labels
            # with no scheduling effect). Only operator absences reserve time.
            sw = optimizer.sweep_optimize(setup.target, setup.search_config,
                                          setup.masters, budget_evals=budget_evals,
                                          seed=_OPT_SEED, on_progress=on_progress,
                                          should_cancel=lambda: _OPTIMIZE.get("cancel"),
                                          base_reserved=setup.absence_reserved,
                                          frozen=setup.frozen)
            res = sw.result
            _finalize_optimize(job_id, base_config, real_baseline, label,
                               winner_overlap=sw.overlap_percent,
                               winner_flexible=sw.flexible_machines, ranks=res.ranks,
                               best=res.best, evals=sw.evals, table=sw.table,
                               cancelled=sw.cancelled,
                               crew_rank=getattr(sw, "crew_rank", None))
        except Exception as e:  # noqa: BLE001 — a failed search must report, not hang
            with _OPTIMIZE_LOCK:
                _OPTIMIZE.update(state="failed", error=str(e), cancel=False)
            # A crashed search left the 'searching…' note standing forever, so the
            # floor and the owner both kept waiting for a plan that was never coming.
            if auto:
                _auto_note_write(f"The plan update started earlier FAILED at {_hhmm()}: "
                                 f"{e}. The plan is unchanged — press \"Done entering: "
                                 "update plan\" to try again.")

    def cloud_job():
        try:
            # Baseline is computed HERE (one plan, seconds) so the before/after
            # is ready whatever the worker does.
            base_sched, base_lines = _all_lines_schedule(setup, setup.masters, None)
            real_baseline = optimizer.plan_metrics(
                base_sched, base_lines, setup.config.plan_start_date,
                with_distribution=True,
                promise_slack_days=getattr(setup.config, "committed_promise_slack_days", 3))
            with _OPTIMIZE_LOCK:
                _OPTIMIZE["baseline"] = real_baseline

            # Oracle tier (2026-08-01): give the always-on poller a window to claim
            # the job before falling back to the GitHub dispatch. 0/negative = no
            # window (today's immediate-GitHub behavior).
            try:
                _claim_min = float(os.environ.get("ORACLE_CLAIM_TIMEOUT_MIN", "3"))
            except ValueError:
                _claim_min = 3.0
            claim_deadline = time.monotonic() + max(0.0, _claim_min) * 60
            claimed = False
            cancelled = False
            while time.monotonic() < claim_deadline:
                with _OPTIMIZE_LOCK:
                    if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
                        return                    # superseded / already finished
                    claimed = bool(_OPTIMIZE.get("claimed"))
                    cancelled = bool(_OPTIMIZE.get("cancel"))
                if claimed or cancelled:
                    break
                time.sleep(2)

            # Re-check right before acting: a claim OR a Stop landing during the
            # loop's final 2s sleep must still be honoured, not overridden by a
            # stale local flag from the last locked read above (the last-sleep
            # race — a claim or cancel here would otherwise be missed and
            # GitHub dispatched anyway).
            with _OPTIMIZE_LOCK:
                if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
                    return
                claimed = bool(_OPTIMIZE.get("claimed"))
                cancelled = bool(_OPTIMIZE.get("cancel"))

            if not claimed and cancelled:
                # Stopped before anyone claimed the job and before GitHub was
                # ever dispatched. Pre-Oracle this race was unreachable
                # (dispatch was immediate); the claim window makes it common,
                # so it must be handled here rather than left to the 40-min
                # watchdog. Same terminal state and message as the wait loop's
                # own cancel-with-nothing-arrived path (via _cancel_cloud_job)
                # so a Stop pressed during the window ends the run promptly
                # with the same UX — no dispatch, no local fallback.
                with _OPTIMIZE_LOCK:
                    if _OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id:
                        _OPTIMIZE.update(state="failed", cancel=False,
                                         error="stopped: no usable result had "
                                               "come back yet, so the current plan "
                                               "is unchanged")
                return

            if not claimed:
                if not _dispatch_workflow(cloud, job_id):
                    with _OPTIMIZE_LOCK:
                        still_mine = (_OPTIMIZE["state"] == "running"
                                      and _OPTIMIZE["job_id"] == job_id)
                        if still_mine:
                            _OPTIMIZE["mode"] = "local"
                            _k, _kc = optimizer.knob_for(setup.search_config)
                            _mult = 2 if getattr(setup.search_config, "scheduler",
                                                  "classic") == "new" else 1
                            _OPTIMIZE["budget_evals"] = _mult * optimizer.sweep_total_evals(
                                budget_evals, getattr(setup.search_config, _k), _kc)
                    if still_mine:
                        local_job()
                    return

            deadline = time.monotonic() + cloud["timeout_min"] * 60
            cancel_deadline = None      # set on first sight of a cancel
            while True:
                time.sleep(2)
                with _OPTIMIZE_LOCK:
                    if (_OPTIMIZE["state"] != "running"
                            or _OPTIMIZE["job_id"] != job_id):
                        return           # the worker delivered (or the job was cleared)
                    # A worker-reported failure short-circuits the deadline.
                    timed_out = (time.monotonic() > deadline
                                 or _OPTIMIZE.get("cloud_failed"))
                    was_cancelled = _OPTIMIZE["cancel"]
                    # Cancel is its OWN trigger, checked on every 2-second poll — not
                    # only when the deadline passes (2026-08-06 spec) — but it does
                    # NOT act immediately: a ~_CANCEL_GRACE_S grace window (owner
                    # decision, 2026-08-06) gives live cloud workers a chance to hear
                    # about the Stop on their next heartbeat and post their
                    # best-so-far before we finalize without them. The salvage lives
                    # in _cancel_cloud_job, called below (once the grace expires and
                    # the lock is released); the clean-stop for a never-resolving
                    # in-flight finalize lives in this loop's own deadline check.
                    if was_cancelled and cancel_deadline is None:
                        cancel_deadline = min(time.monotonic() + _CANCEL_GRACE_S,
                                              deadline)
                    cancel_expired = (cancel_deadline is not None
                                      and time.monotonic() > cancel_deadline)
                    if not was_cancelled and timed_out:
                        # Prefer salvaging arrived shards over a full local
                        # recompute. `claim_shard_finalize` is an ATOMIC claim
                        # of `shards_finalizing` — mutually exclusive with the
                        # /optimize/shard-result collector's own claim of the
                        # same flag, so the two can never both finalize.
                        have_shards = bool(_OPTIMIZE.get("shards"))
                        claim_shard_finalize = (have_shards
                                                 and not _OPTIMIZE.get("shards_finalizing"))
                        if claim_shard_finalize:
                            _OPTIMIZE["shards_finalizing"] = True
                        go_local = not have_shards
                        if go_local:
                            _OPTIMIZE["mode"] = "local"
                            _k, _kc = optimizer.knob_for(setup.search_config)
                            _mult = 2 if getattr(setup.search_config, "scheduler",
                                                  "classic") == "new" else 1
                            _OPTIMIZE["budget_evals"] = _mult * optimizer.sweep_total_evals(
                                budget_evals, getattr(setup.search_config, _k), _kc)
                            _OPTIMIZE["evals"] = 0
                        # else: shards present but another finalize already
                        # claimed (the collector, e.g. the very last shard
                        # landing at the same moment) — do nothing, let it
                        # finish; falls through to the plain `return` below.
                if cancel_expired:
                    _cancel_cloud_job(job_id)
                    with _OPTIMIZE_LOCK:
                        still_mine = (_OPTIMIZE["state"] == "running"
                                      and _OPTIMIZE["job_id"] == job_id)
                        # `continue` below skips the timeout branch for the rest of
                        # this job's life, so the deadline must be enforced HERE or a
                        # never-resolving in-flight finalize would spin forever. The
                        # known way to reach that state is a raise inside
                        # _finalize_from_shards leaving shards_finalizing latched with
                        # shards still populated; /optimize/shard-result now catches
                        # and terminalizes, so this is the backstop for anything that
                        # still slips past.
                        if still_mine and time.monotonic() > deadline:
                            _OPTIMIZE.update(
                                state="failed", cancel=False, cloud_failed=False,
                                error="stopped: no usable result had come back yet, "
                                      "so the current plan is unchanged")
                            still_mine = False
                    if not still_mine:
                        return
                    # Reached only when _cancel_cloud_job left the job alive, which
                    # it does on exactly one path: another party owns an in-flight
                    # finalize. Every other path writes a terminal state, so still_mine
                    # is False and we returned above. Keep polling rather than return,
                    # or nothing ever finishes the job and every future optimize 409s.
                    continue
                if was_cancelled:
                    # Inside the grace window: keep polling so the workers can hear
                    # the Stop on their next heartbeat and deliver their best-so-far.
                    # The normal collector then finalizes and the top-of-loop state
                    # check ends this thread.
                    continue
                if timed_out:
                    if claim_shard_finalize:
                        _finalize_from_shards(job_id)   # salvage what arrived
                        with _OPTIMIZE_LOCK:
                            still_running = (_OPTIMIZE["state"] == "running"
                                              and _OPTIMIZE["job_id"] == job_id)
                            needs_local = still_running and bool(_OPTIMIZE.get("cloud_failed"))
                            if needs_local:
                                _OPTIMIZE["mode"] = "local"
                                _k, _kc = optimizer.knob_for(setup.search_config)
                                _mult = 2 if getattr(setup.search_config, "scheduler",
                                                      "classic") == "new" else 1
                                _OPTIMIZE["budget_evals"] = _mult * optimizer.sweep_total_evals(
                                    budget_evals, getattr(setup.search_config, _k), _kc)
                                _OPTIMIZE["evals"] = 0
                        if needs_local:
                            local_job()   # salvaged shards had no eligible winner
                        return
                    if go_local:
                        local_job()          # cloud never answered → compute here
                    return
        except Exception as e:  # noqa: BLE001
            with _OPTIMIZE_LOCK:
                _OPTIMIZE.update(state="failed", error=str(e), cancel=False)
            # A crashed search left the 'searching…' note standing forever, so the
            # floor and the owner both kept waiting for a plan that was never coming.
            if auto:
                _auto_note_write(f"The plan update started earlier FAILED at {_hhmm()}: "
                                 f"{e}. The plan is unchanged — press \"Done entering: "
                                 "update plan\" to try again.")

    job = cloud_job if cloud else local_job
    if background:
        threading.Thread(target=job, daemon=True).start()
    else:
        job()
    return _optimize_status()


def _finalize_optimize(job_id, base_config, real_baseline, label, *,
                       winner_overlap, winner_flexible=False, ranks, best, evals,
                       table, cancelled, crew_rank=None):
    """Store a finished contest (local sweep OR cloud worker result) as the
    Optimize outcome — one place computes improved/inputs_sig for both paths."""
    # THE PLAN CLOCK STARTS HERE — only while PLAN_START_NEXT_HOUR is on (2026-08-07
    # rule): when the optimization finishes, the plan begins at the NEXT FULL HOUR. A
    # contest landing 09:01 Monday makes every plan start 10:00 Monday, and that clock
    # then HOLDS — for the Gantt, the Orders tab, the reports, the next incumbent
    # measurement — until the next contest finishes. Stamped BEFORE the metric recomputes
    # below so the numbers this panel shows are measured on the same clock the applied
    # plan will run on.
    # With the flag off (live since 2026-08-11) every plan starts at 08:00 of its date,
    # so there is no clock to advance and nothing may be written: a pinned floor left in
    # the store would come back the moment the flag is flipped on again.
    if PLAN_START_NEXT_HOUR:
        _stamp_plan_clock()
    # RECOMPUTE the winner's metrics locally, by replaying its ranks through the same
    # path _plan uses. The contest's own `best` (especially a cloud worker's) can be
    # measured a hair differently from how the app actually replays, so trusting it made
    # the Optimize panel promise a plan the applied plan didn't reproduce (the 2026-07-25
    # 52.5-vs-55.6 gap). Recomputing here makes "shown" == "applied" by construction.
    # Both `best` and `real_baseline` (already local) are then on the same footing, so
    # `improved` is judged honestly. Keep the contest's number only if the replay fails.
    crew_rank = dict(crew_rank or {})
    if ranks:
        _local_best = _metrics_for_ranks(ranks, winner_overlap, winner_flexible,
                                         crew_rank=crew_rank)
        if _local_best is not None:
            best = _local_best
    # `real_baseline` was measured when the contest STARTED, on the previous plan clock.
    # Re-measure it through `_incumbent_metrics` — the ONE "plan you have now" — on the
    # clock just stamped, so the panel's before/after compares two plans on one clock and
    # reports the same "before" the auto-note does. Falls back to the contest-start
    # number if the replay fails.
    if real_baseline is not None:
        try:
            real_baseline = _incumbent_metrics(with_distribution=True)
        except Exception:  # noqa: BLE001 — a re-measure must never fail a finished contest
            pass
    improved = (best is not None and real_baseline is not None
                and optimizer.score(best) < optimizer.score(real_baseline))
    # Fingerprint against the WINNING settings: Apply persists the winning
    # overlap into the saved plan config, so the staleness check must
    # compare future plans against exactly that config.
    _knob, _ = optimizer.knob_for(base_config)
    _won_config = replace(base_config, flexible_machines=bool(winner_flexible),
                          **{_knob: winner_overlap})
    if crew_rank:
        # Apply persists the crew genome into the saved config too, so it belongs in
        # the fingerprint for the same reason the overlap does.
        _won_config = replace(_won_config, crew_rank=dict(crew_rank))
    inputs_sig = _inputs_signature(_won_config)
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["job_id"] != job_id:
            return False
        _OPTIMIZE.update(
            state="done", evals=evals, baseline=real_baseline, best=best,
            cancel=False, cloud_payload=None, error=None,
            elapsed_s=round(time.monotonic() - _OPTIMIZE["started_mono"], 1),
            result={"ranks": ranks, "improved": improved,
                    "cancelled": cancelled,
                    "baseline": real_baseline, "best": best,
                    "budget": label, "seed": _OPT_SEED,
                    "inputs_sig": inputs_sig,
                    "best_overlap": winner_overlap,
                    "current_overlap": getattr(base_config, _knob),
                    "knob": _knob,
                    "flexible_machines": bool(winner_flexible),
                    "current_flexible": bool(getattr(base_config, "flexible_machines", False)),
                    # The roster engine's winning crew genome ({} for every other
                    # engine). Persisted by _optimize_apply beside the ranks.
                    "crew_rank": crew_rank,
                    "sweep_table": table})
        # Record a "last searched" marker for EVERY completed contest — not
        # just an applied one — so a redundant Done click (no improvement
        # found, or nothing was ever applied) can still be skipped without
        # re-running the search.
        book_store.save_last_searched({"book_sig": _OPTIMIZE.get("searched_book_sig"),
                                       "inputs_sig": _OPTIMIZE.get("searched_inputs_sig"),
                                       "plan_start": _OPTIMIZE.get("searched_plan_start")})
    if _OPTIMIZE.get("auto"):
        try:
            _auto_apply_result()
        except Exception:   # noqa: BLE001 — an auto note must never crash a result
            pass
    return True


def _optimize_status():
    with _OPTIMIZE_LOCK:
        elapsed = _OPTIMIZE["elapsed_s"]
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE["started_mono"] is not None:
            elapsed = round(time.monotonic() - _OPTIMIZE["started_mono"], 1)
        res = _OPTIMIZE.get("result") or {}
        return {"state": _OPTIMIZE["state"], "budget": _OPTIMIZE["label"],
                "budget_evals": _OPTIMIZE["budget_evals"], "evals": _OPTIMIZE["evals"],
                "baseline": _OPTIMIZE["baseline"], "best": _OPTIMIZE["best"],
                "error": _OPTIMIZE["error"], "elapsed_s": elapsed,
                "improved": res.get("improved"), "cancelled": res.get("cancelled", False),
                "best_overlap": res.get("best_overlap"),
                "current_overlap": res.get("current_overlap"),
                "flexible_machines": res.get("flexible_machines"),
                "current_flexible": res.get("current_flexible"),
                # which config field the value tunes: overlap_percent (classic)
                # or flow_chunks (flow) — the UI labels the result with this
                "knob": res.get("knob"),
                "mode": _OPTIMIZE.get("mode", "local"),
                # Whether the current/last run was auto-triggered (self-tuning) rather
                # than a manual admin Start click — the UI uses this to suppress a
                # stale Apply/Discard panel after an auto contest has already applied
                # (or discarded) itself.
                "auto": bool(_OPTIMIZE.get("auto")),
                # The job id is only useful together with the worker secret
                # (manual-dispatch mode / debugging) — not sensitive by itself.
                "job_id": _OPTIMIZE.get("job_id") if _OPTIMIZE["state"] == "running" else None,
                "stopping": bool(_OPTIMIZE.get("cancel")) and _OPTIMIZE["state"] == "running"}


def _optimize_cancel():
    """Ask a running search to stop after its current plan; it keeps the best plan
    found so far (state becomes 'done'). No-op if nothing is running."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            _OPTIMIZE["cancel"] = True
    return _optimize_status()


def _cancel_cloud_job(job_id):
    """Honour a Stop during a CLOUD run: salvage whatever shards arrived, then end.

    Stop means stop (2026-08-06 spec), but not on the very first poll: the caller
    (the wait loop in `_start_optimize`'s `cloud_job()`) waits out a
    ~`_CANCEL_GRACE_S` grace window first, so a live worker's next heartbeat has a
    chance to hear about the Stop and post its best-so-far — only calling this once
    that grace has expired (or the overall deadline has, whichever is sooner). Before
    the grace window existed, the wait loop read `cancel` every two seconds and acted
    immediately, which ended the job before any worker still computing could ever
    answer: their next heartbeat 404s, `state["cancel"]` is never set worker-side, and
    their eventual results are dropped — measured turning a Stop that would have
    yielded a real plan into one that yielded nothing. Before THAT, cancel was only
    read inside `if timed_out:`, so a Stop did nothing for up to
    OPTIMIZE_CLOUD_TIMEOUT_MIN (40) minutes. Live 2026-08-06: GitHub allocated 17 of
    20 requested runners, so the collector's all-arrived condition could never be
    met, and 17 delivered shard results were about to be discarded.

    NEVER falls back to local. `_finalize_from_shards` sets `cloud_failed` when the
    merged shards yield no eligible winner, and the watchdog reads that flag to start a
    local search — under cancel that would turn Stop into "start a fresh computation",
    which the owner explicitly rejected. So this clears it.

    `shards_finalizing` is claimed under the lock in the same block that reads it.
    But that flag alone is ambiguous: `_finalize_from_shards`'s no-winner branch leaves
    it set while clearing `shards`, expecting a later watchdog poll to self-heal. Only
    `finalizing AND shards` means genuinely in flight — and only then does this return
    without writing a terminal state, because doing so would make their in-flight
    finalize a silent no-op. In every other case this ENDS the job. The caller does
    NOT simply stop polling after that in-flight case, though — it keeps polling
    (bounded by the overall deadline, enforced in the caller itself) until either
    this function's own terminal write lands on a later call, or the deadline forces
    one; the caller enforces that backstop, not this function.
    """
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
            return                       # already finished, or a superseded job
        have_shards = bool(_OPTIMIZE.get("shards"))
        finalizing = bool(_OPTIMIZE.get("shards_finalizing"))
        # finalizing AND shards  -> someone is genuinely mid-flight
        # finalizing AND !shards -> already ran, found no winner, latched
        #                           (see _finalize_from_shards's no-winner branch)
        in_flight = finalizing and have_shards
        claim = have_shards and not finalizing
        if claim:
            _OPTIMIZE["shards_finalizing"] = True
    if in_flight:
        # Someone else owns the salvage and is still working. Writing a terminal
        # state here would make THEIR _finalize_from_shards a silent no-op — it
        # returns early unless the state is "running" — discarding every shard
        # they were about to merge. Leave them alone; their finalize ends the job.
        return
    if claim:
        _finalize_from_shards(job_id)    # merges whatever subset arrived
        with _OPTIMIZE_LOCK:
            res = _OPTIMIZE.get("result")
            if res is not None and _OPTIMIZE.get("job_id") == job_id:
                # Shards that finished BEFORE the Stop carry cancelled=False, so
                # merge_shard_rows' any() derives False for a partial salvage. Mark
                # it here or the UI presents a part-searched plan as a finished one.
                res["cancelled"] = True
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id:
            # Nothing had arrived, or the merge (just now, or on an earlier call
            # that latched shards_finalizing and was waiting on a watchdog poll
            # this cancel is about to kill) found no eligible winner. End either
            # way, and clear cloud_failed so no local run starts.
            _OPTIMIZE.update(state="failed", cancel=False, cloud_failed=False,
                             error="stopped: no usable result had come back yet, "
                                   "so the current plan is unchanged")


def _all_lines_schedule(setup, masters, ranks):
    """A schedule covering ALL active lines, built the SAME way `_plan` builds it
    (one pass, saved ranks replayed with expedite off, operator absences reserved)
    so both sides of the Optimize before/after are scored on the same domain.
    Returns (schedule, all_lines)."""
    all_lines = list(setup.target)
    ranked_config = replace(setup.config, expedite_window_min=0) if ranks else setup.config
    pr = PlanRun(so_lines=list(all_lines))
    run_forward(pr, ranked_config, masters, reserved=setup.absence_reserved,
                priority_rank=ranks, frozen=getattr(setup, "frozen", None) or None)
    return pr.schedule, all_lines


# How many days later an order must move (vs the plan that was on screen) before the
# Thursday note flags it. 1 = only flag moves of 2+ days. Reporting-only threshold.
_MOVE_LATER_THRESHOLD_DAYS = 1


def _expected_by_order(schedule):
    """Each order's expected completion DATE, keyed (so_no, item_code) — the ONE shared
    definition (optimizer.expected_completion), not a local copy of it."""
    return optimizer.expected_completion(schedule)


def _movers(exp_old, exp_new, threshold):
    """(key, days_later, new_date) for every order whose new expected date is more
    than ``threshold`` days later than before. Worst mover first."""
    out = []
    for k, nd in exp_new.items():
        od = exp_old.get(k)
        if od is not None and (nd - od).days > threshold:
            out.append((k, (nd - od).days, nd))
    out.sort(key=lambda m: m[1], reverse=True)
    return out


def _format_movers(movers):
    """One-line ' ⚠ N order(s) now finish later …' suffix; '' when nothing moved."""
    if not movers:
        return ""
    top = movers[:3]
    parts = [f"{k[0]}-{k[1]} +{d}d (→ {nd.strftime('%d-%b')})" for k, d, nd in top]
    more = f", +{len(movers) - 3} more" if len(movers) > 3 else ""
    return (f" ⚠ {len(movers)} order(s) now finish later than before: "
            + "; ".join(parts) + more + ".")


def _movement_note(new_ranks):
    """Compare the winning plan to the currently-applied one (both replayed on
    today's book) and summarize which orders now finish later. '' if none.
    Must be called BEFORE _optimize_apply() persists the new ranks."""
    config = _resolve_config(_load_plan_config())
    masters = _current_masters()
    setup = optimize_service.prepare_contest(
        book_store.load_active_orders(), book_store.load_actuals(), masters, config,
        absences=book_store.load_absences(),
        operator_table=book_store.load_operator_table(),
        frozen=book_store.load_frozen_ops())
    prio = book_store.load_plan_priority()
    old_ranks = (prio or {}).get("ranks") or None
    old_sched, _ = _all_lines_schedule(setup, setup.masters, old_ranks)
    new_sched, _ = _all_lines_schedule(setup, setup.masters, new_ranks or None)
    movers = _movers(_expected_by_order(old_sched),
                     _expected_by_order(new_sched), _MOVE_LATER_THRESHOLD_DAYS)
    return _format_movers(movers)


def _metrics_for_ranks(ranks, overlap=None, flexible=None, *, with_distribution=True,
                       crew_rank=None):
    """Metrics of the plan that replays ``ranks`` through the SAME local path ``_plan``
    uses (optionally at a given overlap). This is the ONE source of truth for "what
    this optimized plan achieves": the contest (cloud worker OR local sweep) can report
    a ``best`` computed a hair differently from how the app actually replays, so the
    number the Optimize panel shows and the plan the user gets on Apply could disagree
    (the 2026-07-25 52.5-promised / 55.6-applied gap). Recomputing here makes them one
    number by construction. Returns None on any failure (caller keeps the contest's)."""
    try:
        config = _resolve_config(_load_plan_config())
        if overlap is not None:
            knob = optimizer.knob_for(config)[0]
            config = replace(config, **{knob: overlap})
        if flexible is not None:
            config = replace(config, flexible_machines=bool(flexible))
        if crew_rank:
            # The roster engine's second genome. Apply has not persisted it yet at
            # this point, so replaying without it would measure the winning ranks
            # against the OLD roster and report a plan the apply does not reproduce.
            config = replace(config, crew_rank=dict(crew_rank))
        masters = _current_masters()
        actuals = book_store.load_actuals()
        orders = book_store.load_active_orders()
        absences = book_store.load_absences()
        setup = optimize_service.prepare_contest(
            orders, actuals, masters, config, absences=absences,
            operator_table=book_store.load_operator_table(),
            frozen=book_store.load_frozen_ops())
        schedule, all_lines = _all_lines_schedule(setup, setup.masters, ranks or None)
        return optimizer.plan_metrics(
            schedule, all_lines, setup.config.plan_start_date,
            ceiling_days=getattr(config, "worst_ceiling_days", None),
            with_distribution=with_distribution,
            promise_slack_days=getattr(config, "committed_promise_slack_days", 3))
    except Exception:  # noqa: BLE001 — a metrics recompute must never crash finalize
        return None


def _incumbent_metrics(*, with_distribution=False):
    """THE one measure of "the plan you have right now": the applied ranks (if any)
    replayed on TODAY'S book, over ALL active lines — the same domain the contest's
    ``best`` is scored on. Expedite off when ranks replay.

    Every "before" number the owner reads comes from here: the auto-note's "was N", the
    auto-apply gate, and (since 2026-08-07) the Optimize panel's "Now" column. The panel
    used to measure the book with NO optimized sequence at all, so once an optimization
    was applied it reported a plan the owner did not have — on Test9, 967 late-days /
    61.68 days against the note's 956 / 61.5 at the same instant. ``with_distribution``
    adds the panel's lateness bands."""
    config = _resolve_config(_load_plan_config())   # None -> today (IST) for the engine
    masters = _current_masters()
    actuals = book_store.load_actuals()
    orders = book_store.load_active_orders()
    absences = book_store.load_absences()
    setup = optimize_service.prepare_contest(orders, actuals, masters, config,
                                             absences=absences,
                                             operator_table=book_store.load_operator_table(),
                                             frozen=book_store.load_frozen_ops())
    prio = book_store.load_plan_priority()
    ranks = (prio or {}).get("ranks") or None
    schedule, all_lines = _all_lines_schedule(setup, setup.masters, ranks)
    return optimizer.plan_metrics(
        schedule, all_lines, setup.config.plan_start_date,
        with_distribution=with_distribution,
        promise_slack_days=getattr(config, "committed_promise_slack_days", 3))


def _auto_apply_result():
    """Called after an auto contest lands in state=done: apply iff strictly
    better than the incumbent; write the note either way."""
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result") or {}
        best = res.get("best")
    if not best:
        _auto_note_write("Auto-optimize found no plan. Kept the current plan.")
        return
    try:
        # `best` was scored on the book as it stood when this contest started
        # (its snapshot); `_incumbent_metrics()` below scores the incumbent
        # against TODAY's book. If production punched an actual mid-run, that's
        # a transiently mismatched comparison — the next scheduled run
        # re-compares against the fresh book and self-heals.
        inc = _incumbent_metrics()
    except Exception as e:  # noqa: BLE001
        _auto_note_write(f"Auto-optimize finished but could not compare: {e}")
        return
    stamp = _ist_now().strftime("%H:%M")
    worst_ok = best.get("max_late_days", 0) <= inc.get("max_late_days", 0)
    _slack = _resolve_config(_load_plan_config()).committed_promise_slack_days
    promise_ok = best.get("max_committed_slip", 0) <= max(_slack, inc.get("max_committed_slip", 0))
    if optimizer.score(best) < optimizer.score(inc) and worst_ok and promise_ok:
        try:
            move = _movement_note(res.get("ranks") or None)
        except Exception:  # noqa: BLE001 - the note is advisory; never block an apply
            move = ""
        meta = _optimize_apply()          # persists ranks + overlap + inputs_sig + book_sig
        ov = res.get("best_overlap"); cur = res.get("current_overlap")
        word = "chunks" if res.get("knob") == "flow_chunks" else "overlap"
        ov_txt = f", {word} {cur} → {ov}" if ov != cur else ""
        _auto_note_write(f"Plan auto-re-optimized {stamp}: "
                         f"{best['total_late_days']} late-days "
                         f"(was {inc['total_late_days']}){ov_txt}.{move}")
    elif not worst_ok:
        regress = best.get("max_late_days", 0) - inc.get("max_late_days", 0)
        _auto_note_write(f"Checked {stamp}: kept the current plan to protect the worst "
                         f"order — the best alternative would push it {regress}d later.")
    elif not promise_ok:
        regress = best.get("max_committed_slip", 0) - inc.get("max_committed_slip", 0)
        _auto_note_write(f"Checked {stamp}: kept the current plan to protect a committed "
                         f"promise — the best alternative would slip a committed order "
                         f"{regress}d more.")
    else:
        _auto_note_write(f"Checked {stamp}: current plan still best "
                         f"({inc['total_late_days']} late-days).")


def _optimize_apply():
    """Persist the last completed run's ranks — from then on every Plan replays
    them (admin, user, and the auto re-plan after actuals)."""
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result")
        if _OPTIMIZE["state"] != "done" or not res:
            raise HTTPException(status_code=409,
                                detail="no completed optimization to apply")
        # Committed-promise backstop (owner rule: a committed order never slips past
        # promise+slack via re-optimize, on ANY apply path). The auto/Done path already
        # gates on this in _auto_apply_result; enforce it here too so the manual
        # "Apply this plan" button can't push a committed order past its promise.
        inc = _incumbent_metrics()
        best = res.get("best") or {}
        _slack = _resolve_config(_load_plan_config()).committed_promise_slack_days
        if best.get("max_committed_slip", 0) > max(_slack, inc.get("max_committed_slip", 0)):
            raise HTTPException(status_code=409, detail=(
                "This plan would push a committed order past its promised date "
                "(committed orders are capped at promised + "
                f"{_slack} days). "
                "Uncommit the affected order first, or apply a plan that keeps it within the cap."))
        meta = {"saved_at": _ist_now().isoformat(timespec="seconds"),
                "budget": res["budget"], "seed": res["seed"],
                "baseline": res["baseline"], "best": res["best"],
                "covered": len(res["ranks"]),
                "inputs_sig": res.get("inputs_sig"),
                "best_overlap": res.get("best_overlap"),
                # The roster engine's winning crew genome ({} for every other engine).
                "crew_rank": dict(res.get("crew_rank") or {}),
                "book_sig": _current_book_sig(),
                # The date this optimization's clock started from. A later day must
                # re-search rather than replay a sequence built for another start.
                "plan_start": _current_plan_start_sig(),
                # The delivery dates this optimization was computed against, so a
                # later plan can flag "the dates moved, re-run the deep search".
                "dates": _delivery_dates()}
        book_store.save_plan_priority(res["ranks"], meta)
        # Persist the applied plan's per-op assignment (machine/operator/time) so the
        # next "Done" can freeze whatever is in progress on its real machine. Recompute
        # from the winning ranks the same way the incumbent is scored.
        try:
            setup = optimize_service.prepare_contest(
                book_store.load_active_orders(), book_store.load_actuals(),
                _current_masters(), _resolve_config(_load_plan_config()),
                absences=book_store.load_absences(),
                operator_table=book_store.load_operator_table(),
                frozen=book_store.load_frozen_ops())
            sched, _ = _all_lines_schedule(setup, setup.masters, res["ranks"])
            book_store.save_last_applied_schedule(freeze.schedule_projection(sched))
        except Exception:
            pass  # never let schedule-snapshotting break an apply
        # Settings sweep: the winning overlap AND machine-set become THE saved plan
        # settings (the single config every Plan loads and Settings shows). Unchanged
        # winner -> no write, no churn.
        best_ov = res.get("best_overlap")
        best_flex = res.get("flexible_machines")
        cfg = _load_plan_config()
        knob = res.get("knob") or optimizer.knob_for(cfg)[0]
        target = cfg
        if best_ov is not None:
            target = replace(target, **{knob: best_ov})
        if best_flex is not None:
            target = replace(target, flexible_machines=bool(best_flex))
        # The roster engine's crew genome is a plan input exactly like the overlap:
        # replay the same ranks against a different roster and the plan moves. Persist
        # it so every later plan rebuilds the SAME roster — a stable crew for the
        # floor, not a fresh one on every refresh. An EMPTY genome is never written:
        # every other engine returns one, and it must not wipe what is on file.
        best_crew = res.get("crew_rank")
        if best_crew:
            target = replace(target, crew_rank=dict(best_crew))
        if target.to_dict() != cfg.to_dict():
            book_store.save_plan_config(json.dumps(target.to_dict()))
        # Clear the in-memory job so a later page refresh doesn't re-show the
        # "Apply" panel for a plan that's already applied.
        _OPTIMIZE.update(state="idle", result=None, best=None, baseline=None)
        return meta


def _optimize_clear():
    book_store.clear_plan_priority()
    return {"cleared": True}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """Merge an uploaded workbook into the order book. New SO numbers become
    pending orders; known ones are flagged. Masters are updated (latest-wins,
    kept if the file omits them). Admin only."""
    require_admin(request)
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 10 MB)")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 10 MB)")
    try:
        so_lines, masters = load_all(io.BytesIO(contents))
    except Exception as e:  # noqa: BLE001 — surface parse failures to the user
        raise HTTPException(status_code=400, detail=f"Could not read Excel: {e}")

    masters_updated = False
    if masters.routings:  # only replace masters when the file actually has them
        book_store.save_masters_bytes(contents)
        _MASTERS_CACHE["masters"] = None  # invalidate cache → re-read on next plan
        masters_updated = True

    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    new_orders, updated_orders, flags = orderbook.merge_upload(
        so_lines, active, completed, first_seen=_ist_today().isoformat())
    # `add_orders` writes by (SO#, item) with hset, so an updated order overwrites
    # in place — an update needs no separate storage path.
    book_store.add_orders(new_orders + updated_orders)

    result = {
        "name": file.filename,
        "added": len(new_orders),
        "updated": len(updated_orders),
        "flagged": flags,
        "masters_updated": masters_updated,
        "summary": {"items": len(masters.routings), "machines": len(masters.machines)},
        "report": _report_after_upload(masters),
    }
    return result


@app.post("/run")
def run(request: Request, req: Optional[RunRequest] = None):
    """Plan the order book. Admin may set the config (and persist it on an
    explicit Plan click); a user always plans with the admin's saved config, so
    everyone sees one consistent plan."""
    role = getattr(request.state, "role", auth.USER)
    sent = req.config if req else None
    persist = bool(req.persist) if req else False

    # The persist flag is the single switch: an admin's explicit Plan click
    # (persist=True) applies AND saves the submitted config; everything else — an
    # admin auto-load on page open, or any user — plans with the saved config
    # (defaults if none saved). So everyone sees one consistent, planner-set plan.
    if role == auth.ADMIN and persist:
        # MERGE the submitted fields over the currently-saved config, so knobs the
        # Settings form does NOT send keep their stored values instead of resetting to
        # code defaults. The form omits `scheduler` (deploy-level engine) and the
        # optimizer-owned `consolidation_window_days`/`flow_chunks`; a bare
        # from_dict(sent) would silently flip the live engine to the retired "classic"
        # and undo the optimizer's tuned consolidation window. (Overlap % is already
        # echoed by the form; merging is the general guard for the rest.)
        base = {}
        _raw_saved = book_store.load_plan_config()
        if _raw_saved:
            try:
                base = json.loads(_raw_saved)
            except Exception:  # noqa: BLE001 — a corrupt blob just means "no base"
                base = {}
        merged = {**base, **(sent or {})}
        try:
            config = Config.from_dict(merged)   # bad date/type raises here
            config.validate()
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid config: {e}")
        # Engine is a DEPLOY-level choice (DEFAULT_SCHEDULER), never a Settings field —
        # pin the authoritative one so a Save can never persist a stale/other engine
        # (and a store already corrupted with "classic" self-heals on the next Save).
        config.scheduler = _load_plan_config().scheduler
        book_store.save_plan_config(json.dumps(config.to_dict()))
    elif book_store.load_plan_config():
        config = _load_plan_config()   # a saved plan exists → everyone sees it
    elif sent is not None and role == auth.ADMIN:
        # No saved plan yet → honor the ADMIN caller's config (the web UI defaults,
        # e.g. operator logic / downtime ON), falling back to defaults if it's invalid.
        # Admin-only since 2026-08-09: a user's browser also posts a config on every
        # re-plan (Daily Entry save, Done), read from Settings form fields that are
        # CSS-hidden for that role and therefore NEVER refreshed from the server
        # (_plan's applyConfig runs for admins only). With no config saved yet that
        # stale DOM steered the plan, so the two portals could show two different
        # schedules. The user role must never be able to shape a plan.
        config = Config.from_dict(sent)
        try:
            config.validate()
        except ValueError:
            config = Config()
    else:
        config = _load_plan_config()   # engine defaults
    return _plan(config)


@app.post("/rerun")
def rerun(request: Request, req: Optional[RunRequest] = None):
    return run(request, req)  # unified — Run and Rerun are the same action now


@app.get("/orders")
def orders():
    return {"orders": _orders_table()}


@app.post("/orders/delete")
def delete_orders(req: DeleteRequest, request: Request):
    """Permanently delete the given orders — each a (SO number, item code) pair —
    plus their actuals. Admin only, guarded by re-entering the admin password."""
    require_admin(request)
    require_password(request, req.password)
    pairs = [(o[0], o[1]) for o in req.orders if len(o) == 2]
    n = book_store.delete_orders(pairs)
    return {"deleted": n}


@app.post("/orders/clear")
def clear_orders(req: ClearRequest, request: Request):
    """Permanently delete ALL orders + actuals (masters are kept). Admin only, and
    guarded by re-entering the admin password."""
    require_admin(request)
    require_password(request, req.password)
    book_store.delete_all()
    return {"cleared": True}


# --------------------------------------------------------------------------- #
# Mirror (duplicate deployment only)
# --------------------------------------------------------------------------- #
# Friendly names for the store keys a user can detach from / re-attach to the
# live site. Keys absent from this map are internal and never offered.
MIRROR_KEY_LABELS = {
    book_store.ORDERS_KEY: "Order book",
    book_store.COMPLETED_KEY: "Completed orders",
    book_store.ACTUALS_KEY: "Daily entries",
    book_store.MASTERS_KEY: "Master workbook",
    book_store.OPERATORS_KEY: "Operators",
    book_store.ABSENCES_KEY: "Absences",
    book_store.PLAN_CONFIG_KEY: "Plan settings",
}


def _mirror_store():
    """The OverlayStore, or None when this deployment is not a mirror."""
    store = storage.get_store()
    # get_store() may hand back a per-request cache wrapper; the overlay is
    # underneath it.
    store = getattr(store, "_b", store)
    return store if hasattr(store, "reattach") else None


@app.get("/mirror/status")
def mirror_status(request: Request):
    """Whether this deployment mirrors a live site, and which data has been
    detached from it. Drives the header badge and the Settings panel.

    Readable by both roles (the middleware already requires a valid session) —
    the badge must show for the user role too, so nobody mistakes this
    deployment for the real site."""
    store = _mirror_store()
    if store is None:
        return {"enabled": False, "detached": []}
    detached = store.detached_keys()
    return {
        "enabled": True,
        "detached": [
            {"key": k, "label": MIRROR_KEY_LABELS.get(k, k)}
            for k in sorted(detached) if k in MIRROR_KEY_LABELS
        ],
    }


class ReattachRequest(BaseModel):
    key: str
    password: str = ""


@app.post("/mirror/reattach")
def mirror_reattach(req: ReattachRequest, request: Request):
    """Discard everything this deployment did to one key and mirror the live
    site again. Admin only, guarded by re-entering the admin password — it
    throws away local data."""
    require_admin(request)
    require_password(request, req.password)
    store = _mirror_store()
    if store is None:
        raise HTTPException(status_code=404, detail="This deployment is not a mirror")
    if req.key not in MIRROR_KEY_LABELS:
        raise HTTPException(status_code=400, detail="Unknown key")
    store.reattach(req.key)
    return {"reattached": req.key}


@app.post("/orders/commit")
def commit_orders_ep(req: CommitRequest, request: Request):
    """Lock the given orders into the committed lane: each order's current
    expected completion (from a fresh plan) becomes its locked promise. Admin
    only. 404 while the lanes are hidden (COMMITMENT_FEATURE_ENABLED)."""
    _require_commitment_feature()
    require_admin(request)
    _commit_orders([(o[0], o[1]) for o in req.orders if len(o) == 2])
    return {"committed": len(req.orders)}


@app.post("/orders/uncommit")
def uncommit_orders_ep(req: CommitRequest, request: Request):
    """Release the given orders back to the open lane (clears their promise).
    Admin only. 404 while the lanes are hidden (COMMITMENT_FEATURE_ENABLED)."""
    _require_commitment_feature()
    require_admin(request)
    for o in req.orders:
        if len(o) == 2:
            book_store.clear_commitment(o[0], o[1])
    return {"uncommitted": len(req.orders)}


@app.post("/orders/complete")
def complete_order_ep(req: CompleteRequest, request: Request):
    """Mark one SO+item complete (archive it) with ONLY the (SO No, item code) — NO
    operator and no production punch (owner rule, 2026-07-28): closing out an order is
    not a production entry, so it shouldn't demand the operator the Capture-Actuals form
    requires. Available to both roles, like Capture Actuals. The production records
    (actuals) are left untouched, so reports/efficiency still count them. 404 if the
    (SO No, item code) isn't an active order."""
    if not book_store.complete_order(req.so_no, req.item_code):
        raise HTTPException(status_code=404,
                            detail=f"no active order {req.so_no} / {req.item_code} to complete")
    return {"completed": True}


@app.get("/absences")
def get_absences():
    """Operator absences on file, any logged-in role (read-only view feeds the
    Task 16 UI dropdown/list). `orphans` flags absences whose operator name is
    no longer in the current masters — informational, also surfaced in the
    validation report as ABSENT_OPERATOR_UNKNOWN."""
    masters = _current_masters()
    absences = book_store.load_absences()
    return {"absences": absences,
            "orphans": _absence_orphans(masters, absences=absences),
            "operators": sorted({o.name for o in masters.operators})}


@app.post("/absences")
def create_absence(req: AbsenceRequest, request: Request):
    """Record an operator absence window. Admin only. Dates must parse
    (YYYY-MM-DD); a reversed range is accepted and normalized (swapped) rather
    than rejected. The operator must be a name in the current masters."""
    require_admin(request)
    try:
        d_from = date.fromisoformat(req.from_date)
        d_to = date.fromisoformat(req.to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="dates must be YYYY-MM-DD")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    names = {o.name for o in _current_masters().operators}
    if req.operator not in names:
        raise HTTPException(status_code=400,
                            detail=f"unknown operator '{req.operator}'")
    saved = book_store.save_absence({"operator": req.operator,
                                     "from_date": d_from.isoformat(),
                                     "to_date": d_to.isoformat()})
    return {"absence": saved}


@app.delete("/absences/{absence_id}")
def delete_absence_ep(absence_id: str, request: Request):
    """Remove an absence entry. Admin only."""
    require_admin(request)
    if not book_store.delete_absence(absence_id):
        raise HTTPException(status_code=404, detail="absence not found")
    return {"deleted": True}


_VALID_SHIFTS = {"First shift", "Second shift", ""}


def _machine_options(masters):
    """The machines the Settings operator picker may offer, as plain dicts.

    Straight off the uploaded workbook's **Machine master** sheet (already parsed
    and cached by `_current_masters()`), so a machine added to that sheet appears
    in the picker with no code change — the same "edit Excel, never edit code"
    rule the rest of the masters follow.

    Machines a routing references but Machine master does not list yet
    (`provisional`, e.g. CNC7) ARE offered, flagged so the UI can say so:
    excluding them would make it impossible to qualify anyone to run them, and
    their work could then never be staffed. The `OS` sentinel is never a machine
    and is never registered by the loader, so it cannot appear here.

    Read-only — nothing about how a plan is computed depends on this."""
    return [{"id": m.machine_no,
             "name": m.display_name or m.machine_no,
             "type": m.machine_type or "",
             "provisional": bool(m.provisional)}
            for m in sorted(masters.machines.values(),
                            key=lambda m: (bool(m.provisional),
                                           (m.machine_type or "").lower(),
                                           m.machine_no))]


@app.get("/operators")
def get_operators():
    """The app-owned operator/shift table, any logged-in role. Calling
    `_current_masters()` first ensures the one-time seed-from-workbook (if a
    workbook exists and the table has never been seeded) is applied before the
    rows are read back. `next_rotation` is always `None` (2026-08-05): shifts
    no longer rotate automatically, so there is no future rotation date to
    report — the key is kept on the wire so nothing breaks on the client."""
    masters = _current_masters()
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    return {"operators": rows,
            "machines": _machine_options(masters),
            "next_rotation": None}


@app.post("/operators")
def create_operator(req: OperatorRequest, request: Request):
    """Add a new operator to the app-owned table. Admin only. `name` must be
    non-empty and unique among existing rows (case-insensitive, stripped);
    `shift` must be one of the 3 exact values. `_current_masters()` runs FIRST
    so the seed-once-from-workbook path can never be suppressed by a direct
    POST on a fresh deploy (a bare table here would permanently skip migrating
    the workbook's operators). After that, a still-None table means there is
    genuinely nothing to seed from (fresh install, no workbook) — POST then
    creates it (`week_anchor` = the most recent Friday). No optimize trigger —
    scheduled-only contest rules stand."""
    require_admin(request)
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if req.shift not in _VALID_SHIFTS:
        raise HTTPException(status_code=400,
                            detail="shift must be 'First shift', 'Second shift', or ''")
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    if table is None:
        table = {"week_anchor": operator_master.last_friday(_ist_today()).isoformat(),
                 "operators": []}
    existing = {r.get("name", "").strip().lower() for r in table["operators"]}
    if name.lower() in existing:
        raise HTTPException(status_code=400,
                            detail=f"operator '{name}' already exists")
    row = {"id": uuid.uuid4().hex, "name": name, "machines_raw": req.machines_raw,
           "shift": req.shift, "pinned": False}
    table["operators"].append(row)
    book_store.save_operator_table(table)
    return {"operator": row}


@app.patch("/operators/{operator_id}")
def update_operator(operator_id: str, req: OperatorPatchRequest, request: Request):
    """Partially update an operator's `machines_raw`/`shift`/`pinned`. Admin
    only. 404 if the id is unknown (including when no table exists yet);
    `shift`, if given, is validated the same as POST. `_current_masters()`
    runs first (same seed-once guarantee as POST/GET, call-order independent)."""
    require_admin(request)
    if req.shift is not None and req.shift not in _VALID_SHIFTS:
        raise HTTPException(status_code=400,
                            detail="shift must be 'First shift', 'Second shift', or ''")
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    for row in rows:
        if row.get("id") == operator_id:
            if req.machines_raw is not None:
                row["machines_raw"] = req.machines_raw
            if req.shift is not None:
                row["shift"] = req.shift
            if req.pinned is not None:
                row["pinned"] = req.pinned
            book_store.save_operator_table(table)
            return {"operator": row}
    raise HTTPException(status_code=404, detail="operator not found")


@app.delete("/operators/{operator_id}")
def delete_operator(operator_id: str, request: Request):
    """Remove an operator. Admin only. 404 if the id is unknown (including
    when no table exists yet). Absences referencing the removed name become
    orphans — non-blocking, covered by the existing ABSENT_OPERATOR_UNKNOWN
    report row (same as a masters re-upload that drops an operator).
    `_current_masters()` runs first (same seed-once guarantee as POST/GET,
    call-order independent)."""
    require_admin(request)
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    keep = [r for r in rows if r.get("id") != operator_id]
    if len(keep) == len(rows):
        raise HTTPException(status_code=404, detail="operator not found")
    table["operators"] = keep
    book_store.save_operator_table(table)
    return {"deleted": True}


def _validate_year_month(year: int, month: int) -> None:
    """Shared 400 guard for both efficiency endpoints. 2000-2100 is a generous
    sanity window (not a business rule) — just enough to reject fat-fingered
    input without hard-coding "current year"."""
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if not (2000 <= year <= 2100):
        raise HTTPException(status_code=400, detail="year must be 2000-2100")


def _csv_safe(value):
    """Neutralize CSV formula injection: a cell whose text starts with
    =, +, -, @, a tab, or a CR would be executed as a formula by spreadsheet
    apps that open the download — prefix a leading single quote (mirrors the
    client-side tableToCsv guard in web/app.js)."""
    s = str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return value


def _efficiency_rows(year: int, month: int) -> list:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    absences = book_store.load_absences()
    config = _load_plan_config()
    return efficiency.monthly_report(actuals, absences, masters, config, year, month)


@app.get("/efficiency")
def efficiency_report(year: int, month: int, request: Request):
    """Monthly operator efficiency report (admin only). Pure reporting — no
    schedule/plan impact. See engine/efficiency.py for the formula."""
    require_admin(request)
    _validate_year_month(year, month)
    return {"year": year, "month": month, "rows": _efficiency_rows(year, month)}


@app.get("/efficiency.csv")
def efficiency_report_csv(year: int, month: int, request: Request):
    """Same report as a CSV download (admin only), built server-side (unlike the
    Rule-6 schedule CSVs, which are generated client-side in app.js from the
    on-screen table — this one has no on-screen table to scrape until Preview
    is clicked, and admin-only auth is easier to enforce server-side)."""
    require_admin(request)
    _validate_year_month(year, month)
    rows = _efficiency_rows(year, month)
    columns = list(rows[0].keys()) if rows else list(efficiency.REPORT_COLUMNS)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["-" if row[c] is None else _csv_safe(row[c]) for c in columns])
    filename = f"operator-efficiency-{year:04d}-{month:02d}.csv"
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_DELAY_FILLS = {
    "RUNNING": "C6E0B4",                 # green
    "WAITING (machine busy)": "FFE699",  # amber
    "WAITING (off-hours)": "D9D9D9",     # grey
    "WAITING (crew)": "F8CBAD",          # orange
    "OUTSOURCED": "BDD7EE",              # blue — away at a vendor, not our capacity
    "OFF-MACHINE": "BDD7EE",
    "IDLE (capacity free)": "FFC7CE",    # red — machine AND operator free, work waiting
}
_DELAY_SUMMARY_COLS = ["SO No", "Item Code", "Item Name", "Ordered Qty", "SO Delivery Date",
                       "Expected Completion", "Days Late", "Working (days)",
                       "Waiting: machine (days)", "Waiting: off-hours (days)",
                       "Waiting: crew (days)", "Outsourced (days)",
                       "Idle: capacity free (days)", "Why"]
_DELAY_DETAIL_COLS = ["SO No", "Item Code", "State", "Process", "Machine", "Operator",
                      "From", "To", "Hours", "Why"]


def _delay_report_xlsx(report) -> bytes:
    """Serialize the delay report into a 2-sheet .xlsx: Summary + Detail. Datetimes ->
    'DD-MM-YYYY HH:MM', dates -> 'DD-MM-YYYY'. Detail rows are colour-coded by State via
    ONE conditional-formatting rule per state (O(1) styling) — styling every cell of
    ~2000 rows took ~2s locally / ~20s on the free tier and timed the download out."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.formatting.rule import FormulaRule

    bold = Font(bold=True)

    def _cell(v, datetime_col):
        if v is None:
            return ""
        if datetime_col and hasattr(v, "hour"):          # datetime
            return v.strftime("%d-%m-%Y %H:%M")
        if hasattr(v, "isoformat") and not hasattr(v, "hour"):   # date
            return v.strftime("%d-%m-%Y")
        return v

    def _sheet(ws, rows, cols):
        ws.append(cols)
        for c in ws[1]:
            c.font = bold
        ws.freeze_panes = "A2"
        for r in rows:
            ws.append([_cell(r.get(c), c in ("From", "To")) for c in cols])
        for i, name in enumerate(cols, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(12, min(48, len(name) + 2))
        return ws.max_row

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    _sheet(summary, report["summary"], _DELAY_SUMMARY_COLS)

    detail = wb.create_sheet("Detail")
    last = _sheet(detail, report["detail"], _DELAY_DETAIL_COLS)
    if last >= 2:
        state_col = get_column_letter(_DELAY_DETAIL_COLS.index("State") + 1)   # "C"
        rng = f"A2:{get_column_letter(len(_DELAY_DETAIL_COLS))}{last}"
        for state, hexc in _DELAY_FILLS.items():
            detail.conditional_formatting.add(rng, FormulaRule(
                formula=[f'${state_col}2="{state}"'],
                fill=PatternFill("solid", fgColor=hexc)))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _plan_run_for_report(config: Config):
    """(plan_run, so_lines, masters, cfg) for THE plan every tab is showing.

    Runs ``_plan`` and reads back the artifacts it cached, so a report is a VIEW of the
    displayed plan and can never disagree with it. This used to build its own plan; with
    an auto start that resolved to 'the next full hour from now', a report generated an
    hour after the Gantt was a materially different schedule (live 2026-08-07).

    The standalone rebuild below is the fallback for the one case the cache can't cover
    (artifacts missing — e.g. an older cache entry after a hot reload). It reproduces
    _plan's setup exactly: active orders at remaining qty, actuals-advanced start,
    operator overlay as-of the effective start, applied ranks, absence reservations."""
    _plan(config)
    art = _PLAN_CACHE.get("artifacts")
    if art:
        return art["plan_run"], art["so_lines"], art["masters"], art["config"]

    masters = _current_masters()
    config = _resolve_config(config)
    active = book_store.load_active_orders()
    actuals = book_store.load_actuals()
    ab = optimize_service.absence_reservations(book_store.load_absences())
    so_lines = orderbook.active_so_lines(active, actuals, masters)
    eff_start = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                                    masters.calendar)
    if eff_start != config.plan_start_date:
        config = replace(config, plan_start_date=eff_start)
    op_table = book_store.load_operator_table()
    if op_table:
        masters = replace(masters, operators=operator_master.operators_as_of(op_table, eff_start))
    prio = book_store.load_plan_priority()
    ranks = prio["ranks"] if prio else None
    frozen = book_store.load_frozen_ops()
    ranked_config = replace(config, expedite_window_min=0) if ranks else config
    plan_run = PlanRun(so_lines=so_lines)
    run_forward(plan_run, ranked_config, masters, reserved=ab or None, priority_rank=ranks,
                frozen=frozen or None)
    return plan_run, so_lines, masters, config


@app.get("/delay-report.xlsx")
def delay_report_xlsx(request: Request):
    """Per-(SO#, item) delay justification (either role): every hour from plan start to
    completion accounted for, waits attributed to machine contention (each blocking order
    named), off-hours, or crew. A 2-sheet .xlsx download.

    Role-open since 2026-08-09 (director asked for the two portals to match): this is a
    read-only view of the SAME plan both roles already see on the Schedule and Gantt
    tabs, so it exposes nothing the user role could not already read. Contrast
    /efficiency, which stays admin-only because it ranks named people."""
    from engine import delay_report as _dr
    plan_run, so_lines, masters, cfg = _plan_run_for_report(_load_plan_config())
    report = _dr.build_delay_report(plan_run.schedule, so_lines,
                                    plan_run.batches_prioritized, cfg, masters)
    data = _delay_report_xlsx(report)
    fname = f"delay-justification-{_ist_today().isoformat()}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/optimize")
def optimize_ep(req: OptimizeRequest, request: Request):
    """Start a sequence-search job on the current order book (admin only).
    Returns the job status immediately; poll GET /optimize/status for progress."""
    require_admin(request)
    if req.budget not in _OPT_BUDGETS:
        raise HTTPException(status_code=400,
                            detail=f"budget must be one of {sorted(_OPT_BUDGETS)}")
    return _start_optimize(_OPT_BUDGETS[req.budget], req.budget)


@app.get("/optimize/status")
def optimize_status_ep():
    """Live progress of the current/last optimization (any logged-in role)."""
    return _optimize_status()


@app.post("/optimize/cancel")
def optimize_cancel_ep(request: Request):
    """Stop a running search; it keeps the best plan found so far. Admin only."""
    require_admin(request)
    return _optimize_cancel()


@app.post("/optimize/apply")
def optimize_apply_ep(request: Request):
    """Persist the last completed run's optimized order — every Plan replays it.
    Admin only."""
    require_admin(request)
    return _optimize_apply()


@app.post("/optimize/clear")
def optimize_clear_ep(request: Request):
    """Remove the applied optimization (back to the pure Rule-3 order). Admin only."""
    require_admin(request)
    return _optimize_clear()


# --------------------------------------------------------------------------- #
# Cloud-worker endpoints — called by the GitHub Actions runner, authenticated
# by the OPTIMIZE_WORKER_SECRET header (see the gatekeeper bypass). They only
# answer for the currently running job id.
# --------------------------------------------------------------------------- #
def _require_worker(request: Request):
    if getattr(request.state, "role", None) != "worker":
        raise HTTPException(status_code=403, detail="worker secret required")


class WorkerProgress(BaseModel):
    job_id: str
    evals: int = 0
    best: Optional[dict] = None
    shard_index: Optional[int] = None


class WorkerResult(BaseModel):
    job_id: str
    winner_overlap: Optional[int] = None
    winner_flexible: Optional[bool] = None
    ranks: dict = Field(default_factory=dict)
    best: Optional[dict] = None
    rows: list = Field(default_factory=list)
    evals: int = 0
    cancelled: bool = False
    # Forward-compatible: today's worker never sends this (scripts/ is deliberately
    # untouched), so the roster's crew genome is recovered from `rows` instead.
    crew_rank: dict = Field(default_factory=dict)
    error: Optional[str] = None


@app.get("/optimize/job/{job_id}")
def optimize_job_ep(job_id: str, request: Request):
    """The contest payload for the worker (book snapshot + config + budget)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if (_OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == job_id
                and _OPTIMIZE.get("cloud_payload")):
            _OPTIMIZE["claimed"] = True
            return {"payload": _OPTIMIZE["cloud_payload"],
                    "cancel": bool(_OPTIMIZE["cancel"])}
    raise HTTPException(status_code=404, detail="no such running job")


@app.get("/optimize/pending")
def optimize_pending_ep(request: Request):
    """Poll point for the always-on (Oracle) worker: the waiting cloud job's id, or
    null. A job is 'pending' only while running, payload stored, and UNCLAIMED —
    a claimed job (any worker fetched its payload) is no longer offered."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if (_OPTIMIZE["state"] == "running" and _OPTIMIZE.get("cloud_payload")
                and not _OPTIMIZE.get("claimed")):
            return {"job_id": _OPTIMIZE.get("job_id")}
    return {"job_id": None}


@app.post("/optimize/progress")
def optimize_progress_ep(req: WorkerProgress, request: Request):
    """Worker heartbeat: updates the progress bar; the response tells the
    worker whether the admin pressed Stop (it then posts its best-so-far)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == req.job_id:
            if req.shard_index is None:
                _OPTIMIZE["evals"] = max(int(req.evals), _OPTIMIZE["evals"])
            else:
                _OPTIMIZE["shard_evals"][req.shard_index] = int(req.evals)
                _OPTIMIZE["evals"] = sum(_OPTIMIZE["shard_evals"].values())
            if req.best:
                _OPTIMIZE["best"] = req.best
            return {"cancel": bool(_OPTIMIZE["cancel"])}
    raise HTTPException(status_code=404, detail="no such running job")


@app.post("/optimize/result")
def optimize_result_ep(req: WorkerResult, request: Request):
    """The finished contest from the worker. An error report makes the watchdog
    fall back to local compute immediately (the button must always work)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        running = (_OPTIMIZE["state"] == "running"
                   and _OPTIMIZE.get("job_id") == req.job_id)
        base_config = _OPTIMIZE.get("base_config")
        baseline = _OPTIMIZE.get("baseline")
        label = _OPTIMIZE.get("label")
        if running and (req.error or req.best is None or req.winner_overlap is None):
            _OPTIMIZE["cloud_failed"] = True     # watchdog → local fallback now
            _OPTIMIZE["error"] = req.error or "cloud worker returned no result"
            return {"ok": True, "fallback": "local"}
    if not running:
        raise HTTPException(status_code=404, detail="no such running job")
    stored = _finalize_optimize(req.job_id, base_config, baseline, label,
                                winner_overlap=req.winner_overlap,
                                winner_flexible=bool(req.winner_flexible),
                                ranks=req.ranks, best=req.best, evals=req.evals,
                                table=req.rows, cancelled=req.cancelled,
                                # A worker posts the MERGED table, which carries no
                                # top-level crew field (scripts/ is untouched, and an
                                # in-flight old worker could not send one anyway), so
                                # the winner's roster is read back out of its row.
                                crew_rank=(req.crew_rank or
                                           optimize_service.crew_rank_of_winner(
                                               req.rows, req.winner_overlap,
                                               bool(req.winner_flexible), req.best)))
    if not stored:
        raise HTTPException(status_code=409, detail="job superseded")
    return {"ok": True}


class WorkerShardResult(BaseModel):
    job_id: str
    shard_index: int = 0
    shard_total: int = 1
    rows: list = Field(default_factory=list)
    evals: int = 0
    cancelled: bool = False
    error: Optional[str] = None


def _shards_available(job_id) -> bool:
    """True when a running job for `job_id` has at least one accumulated
    shard worth salvaging. Used by the watchdog to prefer a partial finalize
    over burning a full local recompute."""
    with _OPTIMIZE_LOCK:
        return (_OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id
                and bool(_OPTIMIZE.get("shards")))


def _finalize_from_shards(job_id):
    """Merge every accumulated shard's rows and finalize the job — or set
    cloud_failed when the merged set has no eligible winner. Caller holds no
    lock; this takes it. Safe to call from the collector (all-arrived) and the
    watchdog (partial)."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] != "running" or _OPTIMIZE["job_id"] != job_id:
            return
        payload = _OPTIMIZE.get("cloud_payload")
        shards = list(_OPTIMIZE.get("shards", {}).values())
        base_config = _OPTIMIZE.get("base_config")
        baseline = _OPTIMIZE.get("baseline")
        label = _OPTIMIZE.get("label")
    all_rows = [r for s in shards for r in s.get("rows", [])]
    total_evals = sum(int(s.get("evals", 0)) for s in shards)
    any_cancel = any(bool(s.get("cancelled")) for s in shards)
    merged = optimize_service.merge_shard_rows(payload, all_rows, total_evals, any_cancel)
    if merged["best"] is None:
        with _OPTIMIZE_LOCK:
            if _OPTIMIZE["state"] == "running" and _OPTIMIZE["job_id"] == job_id:
                _OPTIMIZE["cloud_failed"] = True   # watchdog → local fallback
                _OPTIMIZE["error"] = "no eligible plan from any shard"
                # Clear the accumulated shards too: the watchdog's `have_shards`
                # check would otherwise still see them as present (even though
                # they yielded no winner) and neither claim a shard-finalize
                # (already claimed by this call) nor fall to `go_local` — the
                # job would wedge in state="running" forever. An empty shards
                # dict makes the next watchdog tick see have_shards=False and
                # go local. Do NOT reset shards_finalizing here — that would
                # reopen a claim window for a concurrent finalize.
                _OPTIMIZE["shards"] = {}
        return
    _finalize_optimize(job_id, base_config, baseline, label,
                       winner_overlap=merged["winner_overlap"],
                       winner_flexible=bool(merged["winner_flexible"]),
                       ranks=merged["ranks"], best=merged["best"],
                       evals=merged["evals"], table=merged["rows"],
                       cancelled=merged["cancelled"],
                       crew_rank=merged.get("winner_crew_rank"))


@app.post("/optimize/shard-result")
def optimize_shard_result_ep(req: WorkerShardResult, request: Request):
    """One matrix shard's rows. Accumulate; when all shards for this job have
    reported, merge and finalize. A stale/late/duplicate shard is a 200 no-op.
    An out-of-range shard_index (bad data) is also a no-op — it must never pad
    the count toward a premature finalize. The worker retries a POST up to 5x
    (at-least-once delivery), so a duplicate of the already-recorded LAST
    shard could otherwise re-enter this handler while the first finalize is
    still running (finalize does slow work outside the lock) and see the
    threshold true again — `shards_finalizing` is claimed exactly once, under
    the lock, so only the one POST that flips it ever calls
    `_finalize_from_shards`. Recording also requires `not shards_finalizing`:
    once a finalize is claimed (including the no-winner path, which clears
    `shards` back to `{}` so the watchdog can fall to local), a further
    duplicate/late POST must not repopulate `shards` — that would fool the
    watchdog's `have_shards` check into thinking there's still something to
    salvage, re-wedging the job. The shard that itself triggers the claim
    still records fine: it writes into `shards` and only THEN checks/claims
    `shards_finalizing` in the same locked block, so the guard never blocks
    the triggering POST — only ones arriving after the claim."""
    _require_worker(request)
    ready = False
    with _OPTIMIZE_LOCK:
        if (_OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == req.job_id
                and 0 <= req.shard_index < req.shard_total
                and not _OPTIMIZE.get("shards_finalizing")):
            _OPTIMIZE["shard_total"] = req.shard_total
            _OPTIMIZE["shards"][req.shard_index] = {
                "rows": req.rows, "evals": req.evals, "cancelled": req.cancelled}
            if (not _OPTIMIZE["shards_finalizing"]
                    and len(_OPTIMIZE["shards"]) >= req.shard_total):
                _OPTIMIZE["shards_finalizing"] = True
                ready = True
    if ready:
        try:
            _finalize_from_shards(req.job_id)
        except Exception as e:  # noqa: BLE001 — a finalize crash must not wedge the job
            # Without this, `shards_finalizing` stays latched with `shards` populated
            # and state stays "running" forever: _start_optimize's guard then 409s
            # every future optimize, including the daily auto-run, until a restart.
            with _OPTIMIZE_LOCK:
                if (_OPTIMIZE["state"] == "running"
                        and _OPTIMIZE.get("job_id") == req.job_id):
                    _OPTIMIZE.update(state="failed", cancel=False, cloud_failed=False,
                                     error=f"could not finalize the cloud result: {e}")
            raise
    return {"ok": True}


@app.post("/optimize/done")
def optimize_done_ep(request: Request):
    """'Done entering — update plan'. Any logged-in role. Runs an auto-applying
    RESTRICTED re-optimization (freezes in-progress ops, re-optimizes the rest)
    every day — no weekday gate. Poll GET /optimize/status for progress when a
    contest starts; the winner auto-applies if strictly better."""
    # No require_admin: the gatekeeper already verified a valid session for any
    # non-public path, and this must be reachable by the user role.
    started = _try_start_auto(by=getattr(request.state, "user", "") or "")
    return {"started": started, "reason": ("started" if started else "skipped"),
            "state": _optimize_status()["state"]}


@app.get("/gantt")
def gantt():
    return _plan(_load_plan_config())["gantt"]


@app.get("/trace/{run_id}")
def get_trace(run_id: str):
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "trace": _RUNS[run_id]}


@app.get("/report")
def report():
    masters = _current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    return _report_for_book(masters, so_lines)


@app.get("/items")
def items():
    """Item metadata for the Daily Production Entry form."""
    masters = _current_masters()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    # Every SO from the orders tab (active + completed), keyed by (SO#, item).
    all_orders = {**completed, **active}   # active wins on any conflict
    items_map = {
        code: {"item_name": routing.description,
               "processes": [p.name for p in routing.processes],
               # Outsourced (OS) steps (Allotted M/c = OS) need no operator at capture;
               # the Daily Entry form uses this to relax the required-operator check.
               "os_processes": [p.name for p in routing.processes
                                if orderbook.process_is_outsourced(routing, p.name)]}
        for code, routing in masters.routings.items()
    }

    # Two-step picker: pick an SO number, then pick one of THAT SO's item lines.
    # Since an SO number can carry several items, map each SO# -> its open item
    # lines (item code + name), so the second dropdown lists exactly those.
    so_to_items = defaultdict(list)
    for o in active.values():
        so_to_items[o.so_no].append({"item_code": o.item_code, "item_name": o.item_name})
    for so in so_to_items:
        so_to_items[so].sort(key=lambda it: it["item_code"])

    return {
        "items": items_map,
        "shifts": ["1st shift", "2nd shift"],
        "so_to_items": dict(so_to_items),          # {so_no: [{item_code, item_name}, ...]}
        "so_nos": sorted({so for so, _ in all_orders}),      # all SO numbers (distinct)
        "open_so_nos": sorted(so_to_items.keys()),           # SO numbers with an open line
    }


@app.post("/actuals")
def post_actuals(req: ActualRequest):
    try:
        entry_date = date.fromisoformat(req.entry_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="entry_date must be YYYY-MM-DD")
    if entry_date > _ist_today():
        raise HTTPException(status_code=400, detail="the date cannot be after today")
    operator = req.operator.strip()
    masters = _current_masters()
    # OUTSOURCED (OS) steps — Allotted M/c = OS — run off-site; no in-house operator
    # runs them, so Capture Actuals does not require an operator for them (owner rule,
    # 2026-07-26). Every other step still requires one. A supplied operator is still
    # validated against the master either way, so typos are caught.
    os_step = orderbook.process_is_outsourced(
        masters.routings.get(req.item_code), req.process)
    if not operator:
        if not os_step:
            raise HTTPException(status_code=400, detail="please pick an operator")
    elif operator not in {o.name for o in masters.operators}:
        raise HTTPException(status_code=400, detail=f"'{operator}' is not in the operator list, pick one from the dropdown")
    actual = Actual(
        so_no=req.so_no, item_code=req.item_code,
        entry_date=entry_date,
        qty_produced=req.qty_produced, qty_rejected=req.qty_rejected,
        shift=req.shift, item_name=req.item_name, process=req.process,
        operator=operator,
        actual_setup_min=req.actual_setup_min,
        no_power_min=req.no_power_min, no_operator_min=req.no_operator_min,
        tool_problem_min=req.tool_problem_min,
        machine_breakdown_min=req.machine_breakdown_min,
        no_load_min=req.no_load_min, other_work_min=req.other_work_min,
        remarks=req.remarks, mark_complete=req.mark_complete,
    )
    # Feedback precedence guard (2026-07-25 spec): a process's recorded qty can't
    # exceed the good qty that cleared the process before it (first step ≤ ordered).
    # Reject out-of-order / over-cap punches BEFORE they are stored.
    _routing = _current_masters().routings.get(req.item_code)
    _order = book_store.load_active_orders().get((req.so_no, req.item_code))
    _ordered = _order.ordered_qty if _order else float("inf")
    _err = orderbook.precedence_cap_error(
        book_store.load_actuals() + [actual], req.so_no, req.item_code,
        req.process, _routing, _ordered)
    if _err:
        raise HTTPException(status_code=400, detail=_err)

    all_actuals = r7.run(actual)
    completed = False
    if req.mark_complete:
        completed = book_store.complete_order(req.so_no, req.item_code)
    visible = orderbook.actuals_on_latest_date(all_actuals)   # show only the latest day
    return {
        "saved": len(all_actuals),
        "completed_order": completed,
        "actuals": to_table(visible),
        "actuals_ids": [a.id for a in visible],
        "by_item": to_table(r7.aggregate_by_item(all_actuals)),
    }


class RollbackRequest(BaseModel):
    id: str


@app.post("/actuals/rollback")
def rollback_actual(req: RollbackRequest):
    """Roll back ONE saved actual (a mis-punched entry), returning that order to
    normal. If the rolled-back entry was the one that marked the order complete,
    the order is un-archived back to active — unless another remaining entry still
    marks it complete. Available to both roles (it fixes a capture mistake).

    Only the LATEST punched date's entries can be rolled back — earlier days are
    locked (keeps the list small; a completed day stays committed)."""
    before = book_store.load_actuals()
    target = next((a for a in before if a.id == req.id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="entry not found (already rolled back?)")
    if target.entry_date != orderbook.latest_actual_date(before):
        raise HTTPException(
            status_code=400,
            detail="you can only undo the latest day's entries. Earlier days are locked",
        )
    # Feedback precedence guard: don't let a rollback retro-create the illegal
    # downstream>upstream state (rolling back an upstream punch that a downstream
    # punch still depends on). Checked BEFORE the delete.
    _routing = _current_masters().routings.get(target.item_code)
    _err = orderbook.rollback_cap_error(
        [a for a in before if a.id != req.id], target, _routing)
    if _err:
        raise HTTPException(status_code=400, detail=_err)

    removed = book_store.delete_actual(req.id)
    if removed is None:
        raise HTTPException(status_code=404, detail="entry not found (already rolled back?)")

    uncompleted = False
    if removed.mark_complete and removed.so_no:
        remaining = book_store.load_actuals()
        still_complete = any(a.key == removed.key and a.mark_complete for a in remaining)
        if not still_complete and removed.key in book_store.load_completed_orders():
            uncompleted = book_store.uncomplete_order(removed.so_no, removed.item_code)

    all_actuals = book_store.load_actuals()
    visible = orderbook.actuals_on_latest_date(all_actuals)   # show only the latest day
    return {
        "removed": True,
        "uncompleted_order": uncompleted,
        "actuals": to_table(visible),
        "actuals_ids": [a.id for a in visible],
        "by_item": to_table(r7.aggregate_by_item(all_actuals)),
        "orders": _orders_table(),
    }


# Static frontend (mounted last so the API routes above take precedence).
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
