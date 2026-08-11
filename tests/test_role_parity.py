"""Admin portal vs user portal parity (2026-08-09, director escalation).

A director compared the two logins and reported that "the admin portal differs
from the user portal". The PLAN is identical for both roles (``POST /run``
ignores a user's config and always plans from the admin's saved one), so the
difference is purely what each role is allowed to SEE.

This suite pins the owner's decision on which of those asymmetries are real:

  EQUALIZED (the user role must now see them)
    - the Analytics tab
    - the delay justification download
    - the "Find a better job order" panel, READ-ONLY (progress + result, no
      Start / Stop / Apply / Discard)
    - the data-gap warning banner, which was never deliberately hidden at all:
      it lives inside the admin-only "Add orders" card, so the user role never
      saw a data-quality warning.

  DELIBERATELY STILL ADMIN-ONLY (asserted here so a future "make it all equal"
  sweep can't quietly take them with it)
    - the efficiency report (per-person performance ranking)
    - the Plan settings card
    - every WRITE control: upload, delete, commit, operators, absences,
      optimize start/stop/apply/clear.

The frontend assertions are structural reads of ``web/index.html`` /
``web/app.js`` because this app has no JS test harness — and structure is
exactly the bug class here: one stray ``admin-only`` class on an ancestor is
all it takes to blank a whole panel for the user role.
"""
import importlib
import io
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A

WEB = Path(__file__).resolve().parent.parent / "web"
INDEX_HTML = (WEB / "index.html").read_text()
APP_JS = (WEB / "app.js").read_text()

# Void elements never open a scope — pushing them would corrupt the ancestor stack.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
         "link", "meta", "param", "source", "track", "wbr"}


class _AncestorFinder(HTMLParser):
    """Record, for every id'd element, the classes of it and all its ancestors."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, classes)]
        self.by_id = {}          # id -> set of classes on it + every ancestor

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if a.get("id"):
            inherited = set(classes)
            for _, anc in self.stack:
                inherited |= anc
            self.by_id[a["id"]] = inherited
        if tag not in _VOID:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            inherited = set((a.get("class") or "").split())
            for _, anc in self.stack:
                inherited |= anc
            self.by_id[a["id"]] = inherited

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _classes_with_ancestors(element_id):
    """Every class on ``element_id`` plus every class on its ancestors.

    ``admin-only`` in here means the user role cannot see the element, whether
    the class sits on the element itself or on a card wrapping it.
    """
    p = _AncestorFinder()
    p.feed(INDEX_HTML)
    assert element_id in p.by_id, f"#{element_id} is not in web/index.html"
    return p.by_id[element_id]


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _seeded_api():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()
    return m


def _client(m, role):
    c = TestClient(m.app)
    creds = ({"username": "anvitech", "password": "1930rail"} if role == "admin"
             else {"username": "anvitech_user", "password": "anvitech12345678"})
    r = c.post("/login", data=creds)
    assert r.status_code in (200, 303), r.text
    return c


# ---- The plan itself is one plan (the thing the director actually cares about) ---- #

def test_both_roles_get_the_identical_plan():
    """Not a permission question: the two portals must agree on the DATES.

    ``POST /run`` plans from the admin's saved config for everyone, so a user's
    submitted config must not be able to move a single expected completion.
    """
    m = _seeded_api()
    admin, user = _client(m, "admin"), _client(m, "user")

    a = admin.post("/run", json={"persist": False}).json()
    # A user sending a deliberately different config must still get the admin's plan.
    u = user.post("/run", json={"persist": False,
                                "config": {"setup_time_min": 5, "overlap_percent": 10}}).json()

    assert u["expected_end"] == a["expected_end"]
    assert u["config"] == a["config"]


# ---- Equalized: things the user role must now be able to SEE ---- #

def test_the_user_role_can_download_the_delay_justification():
    m = _seeded_api()
    r = _client(m, "user").get("/delay-report.xlsx")
    assert r.status_code == 200, r.text
    assert "spreadsheetml" in r.headers["content-type"]


def test_the_analytics_tab_is_offered_to_the_user_role():
    nav = re.search(r'<a class="([^"]*)" data-view="analytics"', INDEX_HTML)
    assert nav, "the Analytics nav link is gone from web/index.html"
    assert "admin-only" not in nav.group(1).split(), \
        "the Analytics nav link is still CSS-hidden from the user role"


def test_the_user_role_is_not_bounced_off_the_analytics_view():
    """showView() used to force ``#analytics`` back to Orders for a non-admin."""
    assert not re.search(r'v\s*===\s*"analytics"\s*&&\s*currentRole', APP_JS), \
        "showView() still redirects the user role away from Analytics"


def test_the_data_gap_warnings_are_visible_to_the_user_role():
    """The validation banner was collateral damage of the admin-only upload card.

    ``renderReport`` runs for BOTH roles on every /run, but wrote into a panel
    nested inside ``#upload-card`` (class ``admin-only``) — so the user role
    could never see a NO_ROUTING / PENDING_MASTER_DATA warning.
    """
    for panel in ("report-panel", "report-noroute"):
        assert "admin-only" not in _classes_with_ancestors(panel), \
            f"#{panel} is still hidden from the user role by an admin-only ancestor"


def test_the_optimize_panel_is_visible_but_read_only_for_the_user_role():
    """The user sees that a search is running and what it found; not the buttons."""
    assert "admin-only" not in _classes_with_ancestors("optimize-panel"), \
        "the Find-a-better-job-order panel is still hidden from the user role"
    assert "admin-only" not in _classes_with_ancestors("optimize-progress"), \
        "the search progress line is still hidden from the user role"
    for control in ("optimize-start", "optimize-stop"):
        assert "admin-only" in _classes_with_ancestors(control), \
            f"#{control} is a write control and must stay admin-only"


def test_the_apply_and_discard_buttons_are_never_rendered_for_the_user_role():
    """renderOptimizeResult() builds these in JS, so CSS can't gate them."""
    block = re.search(r"function renderOptimizeResult\(st\)\s*\{(.*?)\n\}", APP_JS, re.S)
    assert block, "renderOptimizeResult is gone from web/app.js"
    body = block.group(1)
    assert "optimize-apply-btn" in body
    assert re.search(r'currentRole\s*===\s*"admin"', body), \
        "renderOptimizeResult renders Apply/Discard without checking the role"


def test_the_delay_download_button_is_offered_to_the_user_role():
    """Read the whole opening tag — the class attribute may be absent entirely."""
    tag = re.search(r'<button id="dl-delay"[^>]*>', APP_JS)
    assert tag, "the delay-justification button is gone from web/app.js"
    assert "admin-only" not in tag.group(0), \
        "the delay-justification button is still hidden from the user role"


def test_no_tab_is_hidden_from_the_user_role():
    """Whole-nav invariant: a future tab must not quietly become admin-only."""
    hidden = [m.group(1) for m in
              re.finditer(r'<a class="[^"]*admin-only[^"]*" data-view="([^"]+)"', INDEX_HTML)]
    assert hidden == [], f"these tabs are hidden from the user role: {hidden}"


# ---- Deliberately NOT equalized (pin the asymmetries the owner chose to keep) ---- #

def test_the_efficiency_report_stays_admin_only():
    """Per-person performance ranking — one shared 'user' login must not see it."""
    m = _seeded_api()
    user = _client(m, "user")
    assert user.get("/efficiency?year=2025&month=3").status_code == 403
    assert user.get("/efficiency.csv?year=2025&month=3").status_code == 403
    assert "admin-only" in _classes_with_ancestors("eff-preview-btn")


def test_the_plan_settings_card_stays_admin_only():
    assert "admin-only" in _classes_with_ancestors("cfg-setup")
    assert "admin-only" in _classes_with_ancestors("run-btn")


def test_every_write_endpoint_stays_admin_only():
    m = _seeded_api()
    user = _client(m, "user")
    for method, path, body in [
        ("post", "/orders/delete", {"orders": [["SO1", ITEM_A]], "password": "1930rail"}),
        ("post", "/orders/clear", {"password": "1930rail"}),
        ("post", "/absences", {"operator": "X", "from_date": "2025-03-01",
                               "to_date": "2025-03-02"}),
        ("post", "/operators", {"name": "X", "machines_raw": "CNC1", "shift": "First shift"}),
        ("post", "/optimize", {"mode": "quick"}),
        ("post", "/optimize/apply", None),
        ("post", "/optimize/clear", None),
    ]:
        r = getattr(user, method)(path, json=body) if body is not None else getattr(user, method)(path)
        assert r.status_code == 403, f"{method.upper()} {path} is reachable by the user role"
