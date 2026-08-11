"""GET/POST/PATCH/DELETE /operators: role-gated CRUD over the app-owned
operator/shift master table (Task 3 of the operator-master-rotation plan).
`_current_masters()` seeds the table once from the uploaded workbook and
applies any due Friday rotation before GET reads it back; POST/PATCH/DELETE
never touch the workbook and never trigger the scheduled-only optimize
contest."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, orderbook
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _admin_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _user_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    return c


# --- GET: seeding + shape ------------------------------------------------ #
def test_get_before_any_upload_returns_empty_list(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.get("/operators")
    assert r.status_code == 200
    body = r.json()
    assert body["operators"] == []
    assert body["next_rotation"] is None   # rotation removed 2026-08-05; key kept, value None


def test_get_after_upload_seeds_from_masters(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/operators")
    assert r.status_code == 200
    names = {row["name"] for row in r.json()["operators"]}
    assert "Operator One" in names
    assert "Operator Two" in names
    row_one = next(row for row in r.json()["operators"] if row["name"] == "Operator One")
    assert "id" in row_one
    assert row_one["pinned"] is False


# --- role gating ---------------------------------------------------------- #
def test_user_can_get_but_not_mutate(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    user = _user_client(m)

    r = user.get("/operators")
    assert r.status_code == 200

    r = user.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 403

    # Grab a real id as admin to attempt user-side PATCH/DELETE against.
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = user.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 403

    r = user.delete(f"/operators/{op_id}")
    assert r.status_code == 403


# --- POST ------------------------------------------------------------------ #
def test_post_creates_table_when_none_existed(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    assert book_store.load_operator_table() is None

    r = admin.post("/operators", json={"name": "New Hire",
                                       "machines_raw": "CNC 1",
                                       "shift": "First shift"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["name"] == "New Hire"
    assert row["machines_raw"] == "CNC 1"
    assert row["shift"] == "First shift"
    assert row["pinned"] is False
    assert "id" in row

    table = book_store.load_operator_table()
    assert table is not None
    assert table["week_anchor"] == m.operator_master.last_friday(date.today()).isoformat()
    assert len(table["operators"]) == 1


def test_post_defaults_machines_raw_and_shift_to_blank(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "Blank Defaults"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["machines_raw"] == ""
    assert row["shift"] == ""


def test_post_before_any_masters_access_seeds_from_workbook_first(monkeypatch):
    """Regression (reviewer, 2026-07-18): a direct POST on a fresh deploy —
    BEFORE any GET/_current_masters call — must NOT build a bare table and
    permanently suppress the seed-once migration of the stored workbook's
    operators. POST calls _current_masters() first, so the stored table ends
    up with the workbook's operators PLUS the new one."""
    m = _api(); _seed_book()
    admin = _admin_client(m)
    assert book_store.load_operator_table() is None  # no prior masters access

    r = admin.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 200

    table = book_store.load_operator_table()
    names = {row["name"] for row in table["operators"]}
    assert "Operator One" in names          # workbook operators were seeded
    assert "Operator Two" in names
    assert "New Hire" in names              # ...and the POSTed row appended
    assert len(table["operators"]) > 1


def test_post_appends_to_existing_table(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed
    before = len(book_store.load_operator_table()["operators"])

    r = admin.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 200

    after = book_store.load_operator_table()["operators"]
    assert len(after) == before + 1
    assert any(row["name"] == "New Hire" for row in after)


def test_post_empty_name_is_400(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "   "})
    assert r.status_code == 400


def test_post_duplicate_name_case_insensitive_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed ("Operator One" exists)

    r = admin.post("/operators", json={"name": "  operator one  "})
    assert r.status_code == 400


def test_post_invalid_shift_is_400(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "Bad Shift", "shift": "Night shift"})
    assert r.status_code == 400


def test_post_no_optimize_trigger(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api(); _seed_book()
    admin = _admin_client(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))

    r = admin.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 200
    assert not starts


# --- PATCH ------------------------------------------------------------------ #
def test_patch_updates_partial_fields(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert r.json()["operator"]["pinned"] is True

    # Other fields untouched by a partial patch.
    r2 = admin.get("/operators")
    row = next(row for row in r2.json()["operators"] if row["id"] == op_id)
    assert row["pinned"] is True
    assert row["machines_raw"] != ""  # seeded value preserved


def test_patch_updates_machines_raw_and_shift(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}",
                    json={"machines_raw": "CNC 3/CNC 4", "shift": "Second shift"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["machines_raw"] == "CNC 3/CNC 4"
    assert row["shift"] == "Second shift"


def test_patch_invalid_shift_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"shift": "Nope"})
    assert r.status_code == 400


def test_patch_unknown_id_is_404(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed
    r = admin.patch("/operators/not-a-real-id", json={"pinned": True})
    assert r.status_code == 404


def test_patch_unknown_id_is_404_when_table_never_created(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.patch("/operators/not-a-real-id", json={"pinned": True})
    assert r.status_code == 404


# --- DELETE ------------------------------------------------------------------ #
def test_delete_happy_path(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 200 and r.json() == {"deleted": True}

    remaining_ids = [row["id"] for row in admin.get("/operators").json()["operators"]]
    assert op_id not in remaining_ids

    # deleting again (unknown id now) -> 404
    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 404


def test_delete_unknown_id_is_404(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.delete("/operators/not-a-real-id")
    assert r.status_code == 404


def test_delete_unknown_id_is_404_when_table_never_created(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.delete("/operators/not-a-real-id")
    assert r.status_code == 404


def test_save_preserves_optimizer_and_deploy_owned_knobs(monkeypatch):
    """A Settings 'Save & re-plan' must NOT reset knobs the UI doesn't send. The form
    omits `scheduler` (deploy-level engine) and `consolidation_window_days`/`flow_chunks`
    (optimizer-owned). Persisting a readConfig()-style body must PRESERVE their stored
    values, not fall back to code defaults — which would flip the live engine to the
    retired 'classic' and undo the optimizer's consolidation window."""
    import json
    from engine.config import Config
    m = _api(); _seed_book()
    saved = Config().to_dict()
    saved.update(scheduler="new", consolidation_window_days=1, overlap_percent=88)
    book_store.save_plan_config(json.dumps(saved))

    admin = _admin_client(m)
    body = {"config": {"setup_time_min": 90, "overlap_mode": "overlap",
                       "overlap_percent": 88, "apply_operator_logic": True,
                       "split_parallel": False, "expedite_window_min": 0,
                       "balance_operator_load": False, "plan_start_date": None},
            "persist": True}
    assert admin.post("/run", json=body).status_code == 200

    stored = json.loads(book_store.load_plan_config())
    assert stored["scheduler"] == "new", "Save flipped the engine away from 'new'"
    assert stored["consolidation_window_days"] == 1, "Save reset the optimizer's consolidation window"


def test_upload_report_orphans_use_app_operators_not_workbook(monkeypatch):
    """The post-upload validation banner must judge absence orphans against the
    APP-OWNED operator table, not the just-parsed workbook sheet (operators are
    app-owned; the sheet is a fossil). An operator added in Settings — never in any
    workbook — who is marked absent must NOT be flagged ABSENT_OPERATOR_UNKNOWN when
    a workbook is re-uploaded."""
    import io
    from engine.loaders import load_all
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")                     # seed the app table from the workbook
    # Add an app-only operator (not present in any workbook sheet) + an absence.
    table = book_store.load_operator_table()
    table["operators"].append({"id": "app-only-1", "name": "Priya",
                               "machines_raw": "MI1", "shift": "First shift",
                               "pinned": False})
    book_store.save_operator_table(table)
    book_store.save_absence({"operator": "Priya",
                             "from_date": "2025-03-10", "to_date": "2025-03-12"})

    # Simulate the upload handler: it parses the workbook (no 'Priya') and builds the
    # report from THAT masters object.
    _, workbook_masters = load_all(io.BytesIO(build_sample_bytes()))
    report = m._report_after_upload(workbook_masters)
    refs = {dict(zip(report["columns"], row))["Reference"] for row in report["rows"]}
    assert "Priya" not in refs, "an app-owned operator was wrongly flagged as an orphan"


# --- orphan absences after delete (reuses tests/test_absences_api.py pattern) - #
def test_delete_orphans_absences_non_blocking(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    rows = admin.get("/operators").json()["operators"]
    op_one = next(row for row in rows if row["name"] == "Operator One")

    book_store.save_absence({"operator": "Operator One",
                             "from_date": "2025-03-10",
                             "to_date": "2025-03-12"})

    r = admin.delete(f"/operators/{op_one['id']}")
    assert r.status_code == 200

    masters = m._current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    report = m._report_for_book(masters, so_lines)
    cols = report["columns"]
    kinds_refs = [(dict(zip(cols, row))["Kind"], dict(zip(cols, row))["Reference"])
                 for row in report["rows"]]
    assert ("ABSENT_OPERATOR_UNKNOWN", "Operator One") in kinds_refs

    r = admin.get("/absences")
    assert "Operator One" in r.json()["orphans"]


# --- no event trigger for PATCH/DELETE (scheduled-optimize design) -------- #
def test_patch_and_delete_do_not_start_a_contest(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert not starts

    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 200
    assert not starts


# --- machine options for the Settings machine picker (2026-08-04) --------- #
# The picker replaces the free-text machines box, so the browser needs the
# Machine-master list. GET /operators serves it read-only; nothing about how a
# plan is computed changes.
def test_get_returns_machine_options_from_machine_master(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    machines = admin.get("/operators").json()["machines"]

    by_id = {row["id"]: row for row in machines}
    assert {"CNC1", "CNC2", "VMC1", "BS1", "MI1", "MW1"} <= set(by_id)
    assert by_id["CNC1"]["name"] == "CNC 1"          # the master's own spelling
    assert by_id["CNC1"]["type"] == "CNC lathe"      # drives the dropdown grouping
    assert by_id["CNC1"]["provisional"] is False


def test_get_machine_options_include_provisional_machines_flagged(monkeypatch):
    # CNC9 is referenced by Item B's routing but is not in Machine master. It must
    # still be offerable — otherwise nobody could ever be qualified to run it and
    # its work could never be staffed.
    m = _api(); _seed_book()
    admin = _admin_client(m)
    by_id = {row["id"]: row for row in admin.get("/operators").json()["machines"]}

    assert "CNC9" in by_id
    assert by_id["CNC9"]["provisional"] is True


def test_get_machine_options_never_offer_the_os_sentinel(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    ids = [row["id"] for row in admin.get("/operators").json()["machines"]]
    assert "OS" not in ids        # OS means outsourced, not a machine


def test_get_machine_options_empty_before_any_upload(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    body = admin.get("/operators").json()
    assert body["machines"] == []
    assert body["operators"] == []      # existing fields unchanged


def test_get_machine_options_visible_to_the_user_role(monkeypatch):
    # The read-only role sees the same panel (as plain text), so it needs the
    # same list to render machine display names.
    m = _api(); _seed_book()
    user = _user_client(m)
    r = user.get("/operators")
    assert r.status_code == 200
    assert any(row["id"] == "CNC1" for row in r.json()["machines"])


# --- rotation removal (Tasks 1-3): old fields stay inert, the banner still fires --- #
def test_a_store_with_the_old_pinned_and_anchor_fields_still_loads(monkeypatch):
    """Rotation was removed but the fields stay on disk. An existing store must not
    500, and PATCHing `pinned` must still be accepted so nothing breaks mid-deploy."""
    m = _api(); _seed_book()
    admin = _admin_client(m)

    ops = admin.get("/operators").json()["operators"]
    assert ops, "seeded table expected"
    op_id = ops[0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert admin.get("/operators").status_code == 200


def test_changing_an_operators_shift_flags_the_applied_plan_stale(monkeypatch):
    """The owner's requirement: if a shift changes, the banner must say the applied
    optimization no longer matches, the same way a settings change does.

    `_inputs_signature` takes a single `Config` (checked against the real
    signature in api/main.py — it is not `(masters, config)` as one might
    guess). It must be computed on the SAVED (unresolved) config, exactly as
    `_plan` does internally (`current_inputs_sig = _inputs_signature(config)`
    BEFORE `_resolve_config` runs) — resolving the auto `plan_start_date`
    first would fold today's date into the signature and make it disagree
    with `_plan`'s own for a reason that has nothing to do with operators.

    `save_plan_priority` also requires a non-empty `ranks` dict:
    `book_store.load_plan_priority` treats `{"ranks": {}, ...}` as absent
    (falls back to plain Rule 3), so `optimize_meta` never gets `active: True`
    or an `inputs_changed` key at all with an empty dict — the brief's literal
    `{}` was checked against the store code and adapted to real keys.
    """
    m = _api(); _seed_book()
    admin = _admin_client(m)

    ops = admin.get("/operators").json()["operators"]
    target = next(o for o in ops if o["shift"] in ("First shift", "Second shift"))

    # Pin an "applied optimization" whose inputs signature matches the book right
    # now — same config basis `_plan` uses (SAVED/unresolved, no plan saved yet).
    sig = m._inputs_signature(m._load_plan_config())
    ranks = {f"SO1{m.KEY_SEP}{ITEM_A}": 1}
    m.book_store.save_plan_priority(ranks, {"saved_at": "2026-08-05T10:00:00",
                                            "inputs_sig": sig})
    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["active"] is True
    assert meta["inputs_changed"] is False

    flipped = "Second shift" if target["shift"] == "First shift" else "First shift"
    assert admin.patch(f"/operators/{target['id']}", json={"shift": flipped}).status_code == 200

    meta = admin.post("/run", json={}).json()["optimize_meta"]
    assert meta["inputs_changed"] is True
