from datetime import date, datetime
import pytest
from engine import optimizer
from engine.config import Config
from engine.models import SOLine, ScheduleEntry

def _line(so, item, due, commitment="open", promised=None, qty=10):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=qty,
                  delivery_date=due, commitment=commitment, promised_date=promised)

def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1, process_name="CNC",
                         machine="CNC1", qty=10, occupancy_min=60,
                         start=datetime(2026,7,29,8,0), end=end, so_refs=[so])

def test_committed_promise_breach_and_max_slip():
    ps = date(2026,7,29)
    # committed, promised 05-Aug, finishes 10-Aug -> slip 5 days; slack 3 -> over 2 -> breach 4
    lines = [_line("SO1","IT-A",date(2026,8,20),"committed",date(2026,8,5)),
             _line("SO2","IT-B",date(2026,8,20),"open")]                 # open -> ignored
    sched = [_entry("SO1","IT-A",datetime(2026,8,10,17,0)),
             _entry("SO2","IT-B",datetime(2026,8,30,17,0))]             # open late, irrelevant
    m = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert m["max_committed_slip"] == 5           # (10-Aug − 5-Aug)
    assert m["committed_promise_breach"] == 4.0    # (5-3)^2

def test_committed_within_slack_is_zero():
    ps = date(2026,7,29)
    lines = [_line("SO1","IT-A",date(2026,8,20),"committed",date(2026,8,5))]
    sched = [_entry("SO1","IT-A",datetime(2026,8,7,17,0))]              # slip 2 <= slack 3
    m = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert m["committed_promise_breach"] == 0.0
    assert m["max_committed_slip"] == 2

def test_no_committed_is_byte_identical():
    ps = date(2026,7,29)
    lines = [_line("SO1","IT-A",date(2026,8,20),"open")]
    sched = [_entry("SO1","IT-A",datetime(2026,8,30,17,0))]
    base = optimizer.plan_metrics(sched, lines, ps)                     # no promise_slack_days
    withp = optimizer.plan_metrics(sched, lines, ps, promise_slack_days=3)
    assert withp["committed_promise_breach"] == 0.0
    assert withp["max_committed_slip"] == 0
    assert optimizer.score(base) == optimizer.score(withp)

def test_config_slack_default_and_validation():
    """committed_promise_slack_days has default 3 and validates >= 0."""
    assert Config().committed_promise_slack_days == 3
    with pytest.raises(Exception):
        Config(committed_promise_slack_days=-1).validate()

def test_config_slack_roundtrip():
    """to_dict/from_dict preserve committed_promise_slack_days."""
    cfg = Config(committed_promise_slack_days=5)
    roundtrip = Config.from_dict(cfg.to_dict())
    assert roundtrip.committed_promise_slack_days == 5

def test_config_slack_missing_defaults():
    """from_dict with missing committed_promise_slack_days defaults to 3."""
    cfg = Config.from_dict({})
    assert cfg.committed_promise_slack_days == 3
