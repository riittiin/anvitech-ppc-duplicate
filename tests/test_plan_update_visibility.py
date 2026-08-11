"""'Done entering — update plan' must never be invisible (live 2026-08-09).

An operator on the floor pressed Done; the owner 10 km away saw nothing on his
screen and had no way to tell whether a search had started, been skipped, failed,
or been killed. Three paths produced that symptom and NONE of them left a trace:

  * `_try_start_auto` swallowed any exception with a bare `return False`;
  * a `_start_optimize` HTTPException did the same;
  * a contest lives in process memory only, so a Render restart or free-tier
    spin-down erased it silently — state back to idle, no note, no error.

So: every Done click ends in a durable, human-readable line on the Orders tab,
including "still running" and "interrupted". And because that line is DISPLAY,
not a plan input, it must not force a re-plan (the 2026-08-08 cache lesson).
"""
import importlib
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")     # the trigger is ON in production
    import api.main as m
    importlib.reload(m)
    return m


def _seed(m):
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()


def _client(m, user="anvitech", pw="1930rail"):
    c = TestClient(m.app)
    c.post("/login", data={"username": user, "password": pw})
    return c


def _note(c):
    return (c.post("/run", json={"persist": False}).json().get("auto_note")
            or {}).get("text", "")


def test_pressing_done_says_a_search_started_and_who_started_it():
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp); _seed(m)
        started = []
        mp.setattr(m, "_start_optimize", lambda *a, **k: started.append(k))
        floor = _client(m, "anvitech_user", "anvitech12345678")

        assert floor.post("/optimize/done").json()["started"] is True
        assert started, "the contest should have been started"

        owner_sees = _note(_client(m))
        assert "anvitech_user" in owner_sees
        assert "running" in owner_sees.lower() or "search" in owner_sees.lower()


def test_a_search_killed_by_a_restart_is_reported_as_interrupted():
    """A contest only lives in this process. If the server restarts (deploy) or
    Render's free tier spins it down, nobody was ever told."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp); _seed(m)
        # A note left behind by a DIFFERENT process — i.e. before a restart.
        book_store.save_auto_note({"text": "Plan update started 14:32 by ravi — searching…",
                                   "at": "2026-08-09T14:32:00", "running": True,
                                   "process": "a-process-that-is-gone"})
        text = _note(_client(m))
        assert "interrupted" in text.lower()
        assert "again" in text.lower(), "it must say what to do about it"


def test_a_live_search_is_never_mislabelled_as_interrupted():
    """The window between a contest finishing and its result note being written is
    real. Crying 'interrupted' there would tell the floor to press Done again on a
    search that is about to land — so only a note from a DEAD process counts."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp); _seed(m)
        book_store.save_auto_note({"text": "searching…", "at": "2026-08-09T14:32:00",
                                   "running": True, "process": m._PROCESS_TOKEN})
        assert m._OPTIMIZE["state"] != "running"      # nothing running in-process
        assert "interrupted" not in _note(_client(m)).lower()


def test_a_search_that_cannot_start_is_never_silent():
    """The bare `except Exception: return False` reported nothing at all."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp); _seed(m)

        # Something used ONLY by the trigger's gate, so /run itself stays healthy —
        # the point is that a failure to start still reaches the owner's screen.
        def boom():
            raise RuntimeError("store unreachable")
        mp.setattr(m, "_applied_plan_meta", boom)

        c = _client(m)
        assert c.post("/optimize/done").json()["started"] is False
        text = _note(c)
        assert "store unreachable" in text
        assert "could not" in text.lower()


def test_a_refused_start_is_never_silent():
    """`_start_optimize` raising HTTPException (e.g. nothing to optimize) was
    also swallowed into a bare False."""
    with pytest.MonkeyPatch.context() as mp:
        from fastapi import HTTPException
        m = _api(mp); _seed(m)

        def refuse(*a, **k):
            raise HTTPException(status_code=400, detail="no active orders to optimize")
        mp.setattr(m, "_start_optimize", refuse)

        c = _client(m)
        assert c.post("/optimize/done").json()["started"] is False
        assert "no active orders to optimize" in _note(c)


def test_the_note_is_display_only_and_never_forces_a_re_plan():
    """The Orders-tab note is not a plan input. Folding it into the cache key made
    every status message throw away a good plan and recompute it on Render's free
    CPU — the worst possible moment, since a contest is usually running."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp); _seed(m)
        c = _client(m)
        first = c.post("/run", json={"persist": False}).json()

        m._auto_note_write("Plan update started 09:15 by ravi — searching…", running=True)

        second = c.post("/run", json={"persist": False}).json()
        assert second["run_id"] == first["run_id"], "the plan must NOT be recomputed"
        assert "09:15" in second["auto_note"]["text"], "but the note must be fresh"
