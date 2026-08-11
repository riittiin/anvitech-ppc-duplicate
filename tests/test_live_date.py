"""Live current-date mode (go-live 2026-07-19): every date the app reasons from
follows the REAL current date (IST), not a frozen test-era date. Config's
plan_start_date defaults to None ("auto: start from today"); the API boundary
resolves None -> _ist_today() at every planning entry, while the SAVED config
keeps None so a moving 'today' never looks like a settings change.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book(n_orders=2):
    book_store.save_masters_bytes(build_sample_bytes())
    items = [ITEM_A, ITEM_B]
    book_store.add_orders([
        Order(f"SO{i+1}", items[i % 2], items[i % 2], 10 + 5 * i, date(2025, 3, 20 + i))
        for i in range(n_orders)])


# --------------------------------------------------------------------------- #
# _ist_today / _resolve_config
# --------------------------------------------------------------------------- #
def test_ist_today_is_ist_now_date():
    m = _api()
    assert m._ist_today() == m._ist_now().date()


def test_resolve_config_fills_none_with_ist_today(monkeypatch):
    m = _api()
    fixed = date(2030, 1, 15)
    monkeypatch.setattr(m, "_ist_today", lambda: fixed)
    resolved = m._resolve_config(Config())  # plan_start_date None
    assert resolved.plan_start_date == fixed


def test_resolve_config_leaves_explicit_date_untouched(monkeypatch):
    m = _api()
    monkeypatch.setattr(m, "_ist_today", lambda: date(2030, 1, 15))
    cfg = Config(plan_start_date=date(2025, 3, 1))
    assert m._resolve_config(cfg).plan_start_date == date(2025, 3, 1)


# --------------------------------------------------------------------------- #
# _plan uses IST today when the config is auto (None)
# --------------------------------------------------------------------------- #
def test_plan_with_auto_config_starts_from_ist_today(monkeypatch):
    m = _api()
    _seed_book()
    fixed = date(2030, 6, 3)  # a Monday, safely a working day
    monkeypatch.setattr(m, "_ist_today", lambda: fixed)
    result = m._plan(Config())  # auto config
    exp = result.get("expected_end", {})
    assert exp, "expected at least one scheduled order"
    # Every scheduled completion must land on/after the auto start date — the
    # plan clock followed IST today, not the legacy 2025-03-01 default.
    for iso in exp.values():
        assert date.fromisoformat(iso) >= fixed


# --------------------------------------------------------------------------- #
# Response contract: the /run response must echo the SAVED (unresolved) config —
# auto mode must survive a page reload. The resolved start is a SEPARATE display
# key. (Live 2026-07-18 bug: returning the resolved config made the UI's auto
# checkbox untick itself, and the next Settings save persisted today as a FIXED
# date — auto mode silently lost.)
# --------------------------------------------------------------------------- #
def test_plan_response_config_keeps_null_and_carries_resolved_start(monkeypatch):
    m = _api()
    _seed_book()
    fixed = date(2030, 6, 3)
    monkeypatch.setattr(m, "_ist_today", lambda: fixed)
    result = m._plan(Config())  # auto config (plan_start_date None)
    assert result["config"]["plan_start_date"] is None
    assert result["resolved_plan_start"] == fixed.isoformat()


def test_plan_response_config_echoes_explicit_date(monkeypatch):
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_ist_today", lambda: date(2030, 6, 3))
    result = m._plan(Config(plan_start_date=date(2025, 3, 1)))
    assert result["config"]["plan_start_date"] == "2025-03-01"
    assert result["resolved_plan_start"] == "2025-03-01"


# --------------------------------------------------------------------------- #
# Persist path saves null for auto mode
# --------------------------------------------------------------------------- #
def test_persist_saves_null_for_auto_config():
    import json
    m = _api()
    _seed_book()
    cfg = Config.from_dict({"plan_start_date": None})
    book_store.save_plan_config(json.dumps(cfg.to_dict()))
    raw = book_store.load_plan_config()
    assert json.loads(raw)["plan_start_date"] is None
    # And loading it back keeps None (auto), never a resolved date.
    assert m._load_plan_config().plan_start_date is None


# --------------------------------------------------------------------------- #
# A moving 'today' must NOT look like a settings change
# --------------------------------------------------------------------------- #
# Two todays in the SAME operator-rotation week (no Friday between them), so the
# only thing that could move the fingerprint is plan_start_date — which must NOT,
# because the saved config keeps None. (A Friday rotation legitimately DOES change
# the signature; that is the separate 2026-07-18 behaviour, not under test here.)
_DAY1 = date(2030, 6, 3)   # Monday
_DAY2 = date(2030, 6, 5)   # Wednesday, same week


def test_inputs_signature_stable_across_todays(monkeypatch):
    m = _api()
    _seed_book()
    saved = Config()  # auto (None)

    monkeypatch.setattr(m, "_ist_today", lambda: _DAY1)
    m._current_masters()   # warm: seed the operator table once (as in production)
    sig1 = m._inputs_signature(saved)
    monkeypatch.setattr(m, "_ist_today", lambda: _DAY2)
    sig2 = m._inputs_signature(saved)
    assert sig1 == sig2


def test_scheduled_skip_holds_across_todays(monkeypatch):
    """Two scheduled ticks on different todays with no book/settings change must
    both find the inputs signature unchanged (auto mode must not self-trigger)."""
    import json
    m = _api()
    _seed_book()
    book_store.save_plan_config(json.dumps(Config().to_dict()))  # auto saved

    monkeypatch.setattr(m, "_ist_today", lambda: _DAY1)
    m._current_masters()   # warm: seed the operator table once (as in production)
    sig_day1 = m._inputs_signature(m._load_plan_config())
    monkeypatch.setattr(m, "_ist_today", lambda: _DAY2)
    sig_day2 = m._inputs_signature(m._load_plan_config())
    assert sig_day1 == sig_day2
