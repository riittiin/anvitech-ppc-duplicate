"""commit / uncommit — the owner status actions.

Post-pivot (2026-07-16): lanes are pure status labels with no scheduling effect.
Commit snapshots the current expected completion as an INFORMATIONAL promise;
it does not constrain the plan (the byte-identical regression lives in
test_replay_single_pass). There is no push-warning preview. The Urgent lane
was removed 2026-07-29 (`POST /orders/urgent` is gone; stored "urgent" rows
migrate to "committed" on load — see tests/test_no_urgent.py).

Drives the internal helpers directly (`_commit_orders`) plus the thin endpoint
functions; HTTP auth/CSRF wiring is covered elsewhere.
"""
from datetime import date

import pytest


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_masters():
    from engine import book_store
    from tests.sample_workbook import build_sample_bytes
    book_store.save_masters_bytes(build_sample_bytes())


# --------------------------------------------------------------------------- #
# _commit_orders
# --------------------------------------------------------------------------- #
def test_commit_snapshots_current_expected_as_promise():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    m._commit_orders([("SO1", ITEM_A)])

    o = book_store.load_active_orders()[("SO1", ITEM_A)]
    assert o.commitment == "committed"
    assert o.promised_date is not None
    assert isinstance(o.promised_date, date)
    assert o.committed_at  # snapshot timestamp recorded


def test_commit_orders_skips_unknown_pair_without_raising():
    m = _api()
    _seed_masters()
    # No orders in the book at all — should just no-op silently.
    m._commit_orders([("NOPE", "NOPE")])


# --------------------------------------------------------------------------- #
# uncommit
# --------------------------------------------------------------------------- #
def test_uncommit_resets_to_open():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 3, 20), committed_at="x"),
    ])

    ok = book_store.clear_commitment("SO1", ITEM_A)
    assert ok is True

    o = book_store.load_active_orders()[("SO1", ITEM_A)]
    assert o.commitment == "open"
    assert o.promised_date is None
    assert o.committed_at is None


# --------------------------------------------------------------------------- #
# endpoint functions (thin — driven directly, not via HTTP/TestClient)
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """Stand-in for FastAPI Request with the admin role already set, matching
    how require_admin reads request.state.role."""
    class _State:
        role = "admin"
    state = _State()


def test_commit_endpoint_function_commits_and_counts(monkeypatch):
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    # The lanes are hidden by default (2026-08-04 feature gate); this test covers
    # the feature itself, so it turns the flag on.
    monkeypatch.setattr(m, "COMMITMENT_FEATURE_ENABLED", True)
    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    req = m.CommitRequest(orders=[["SO1", ITEM_A]])
    result = m.commit_orders_ep(req, _FakeRequest())
    assert result == {"committed": 1}
    assert book_store.load_active_orders()[("SO1", ITEM_A)].commitment == "committed"


def test_uncommit_endpoint_function_clears(monkeypatch):
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    monkeypatch.setattr(m, "COMMITMENT_FEATURE_ENABLED", True)   # see the note above
    _seed_masters()
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 3, 20), committed_at="x"),
    ])

    req = m.CommitRequest(orders=[["SO1", ITEM_A]])
    result = m.uncommit_orders_ep(req, _FakeRequest())
    assert result == {"uncommitted": 1}
    assert book_store.load_active_orders()[("SO1", ITEM_A)].commitment == "open"


# --------------------------------------------------------------------------- #
# Feature gate (2026-08-04): the directors asked for commit/uncommit to be
# HIDDEN, not removed. One constant controls the UI and the endpoints together —
# see docs/superpowers/specs/2026-08-04-hide-commitment-feature-design.md
# --------------------------------------------------------------------------- #
def test_commitment_feature_is_off_by_default():
    m = _api()
    assert m.COMMITMENT_FEATURE_ENABLED is False


def test_me_tells_the_browser_the_feature_is_off():
    """The UI hides its buttons/columns from this flag, so /me must report it —
    one source of truth, so the screen and the server can never disagree."""
    m = _api()

    class _Req:
        class _State:
            user, role = "anvitech", "admin"
        state = _State()

    assert m.me(_Req())["commitment_enabled"] is False


def test_commit_endpoint_is_closed_while_the_feature_is_hidden():
    """Closing the endpoint is the point: with the buttons gone but the endpoint
    live, an order could still be committed through the API and would then steer
    the optimizer with nothing on screen to reveal or undo it."""
    import pytest as _pytest
    from fastapi import HTTPException

    m = _api()
    _seed_masters()
    req = m.CommitRequest(orders=[["SO1", "A"]])
    with _pytest.raises(HTTPException) as e:
        m.commit_orders_ep(req, _FakeRequest())
    assert e.value.status_code == 404


def test_uncommit_endpoint_is_closed_while_the_feature_is_hidden():
    import pytest as _pytest
    from fastapi import HTTPException

    m = _api()
    _seed_masters()
    req = m.CommitRequest(orders=[["SO1", "A"]])
    with _pytest.raises(HTTPException) as e:
        m.uncommit_orders_ep(req, _FakeRequest())
    assert e.value.status_code == 404


def test_flipping_the_flag_brings_the_whole_feature_back(monkeypatch):
    """Hidden, not broken: one constant restores the endpoints AND what /me tells
    the UI, so the buttons and columns come back with them."""
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    m = _api()
    monkeypatch.setattr(m, "COMMITMENT_FEATURE_ENABLED", True)
    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    class _Req:
        class _State:
            user, role = "anvitech", "admin"
        state = _State()

    assert m.me(_Req())["commitment_enabled"] is True
    assert m.commit_orders_ep(m.CommitRequest(orders=[["SO1", ITEM_A]]),
                              _FakeRequest()) == {"committed": 1}
    assert book_store.load_active_orders()[("SO1", ITEM_A)].commitment == "committed"
    assert m.uncommit_orders_ep(m.CommitRequest(orders=[["SO1", ITEM_A]]),
                                _FakeRequest()) == {"uncommitted": 1}
