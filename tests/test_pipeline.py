"""Pipeline & trace backbone — error localization and not-reached marking."""
from engine.config import Config
from engine.models import PlanRun, Masters
from engine import pipeline
from engine.pipeline import run_rule, RuleError, to_table


def test_run_rule_snapshots_input_output_config_notes():
    trace = {}

    def fake(data, config=None, notes=None, **kw):
        notes.append("did a thing")
        return [{"x": 1}, {"x": 2}]

    out = run_rule(trace, "ruleX", fake, [{"a": 1}], config=Config())
    assert out == [{"x": 1}, {"x": 2}]
    entry = trace["ruleX"]
    assert entry["output"]["columns"] == ["x"]
    assert entry["notes"] == ["did a thing"]
    assert entry["config"]["consolidation_window_days"] == 10
    assert entry["error"] is None


def test_failing_rule_records_error_and_stops_chain():
    trace = {}

    def boom(data, config=None, notes=None, **kw):
        raise RuleError("ruleX", "rec1", "contract violated")

    try:
        run_rule(trace, "ruleX", boom, [], config=Config())
        assert False, "expected RuleError"
    except RuleError:
        pass
    assert trace["ruleX"]["error"]["record_id"] == "rec1"


def test_run_forward_marks_downstream_not_reached(monkeypatch, loaded):
    # Force Rule 2 to fail; Rules 3/6 must be marked not reached, Rule 1 ok.
    from engine.rules import rule2_sort_by_date

    def boom(data, config=None, notes=None, **kw):
        raise RuleError("rule2", "-", "boom")

    monkeypatch.setattr(rule2_sort_by_date, "run", boom)

    so_lines, masters = loaded
    trace = pipeline.run_forward(PlanRun(so_lines=so_lines), Config(), masters)

    assert trace["rule1"]["reached"] is True
    assert trace["rule2"]["error"] is not None
    assert trace["rule3"]["reached"] is False
    assert trace["rule6"]["reached"] is False


def test_to_table_handles_empty():
    assert to_table(None) == {"columns": [], "rows": []}
    assert to_table([]) == {"columns": [], "rows": []}
