"""Golden snapshot — run all forward rules on the generated sample workbook and
compare the per-rule output tables to a committed expected trace. Any logic change
shows as a diff. Regenerate intentionally with:  REGEN_GOLDEN=1 pytest -k golden
"""
import json
import os
from datetime import date
from pathlib import Path

from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward

GOLDEN = Path(__file__).parent / "golden_trace.json"
SNAPSHOT_RULES = ["rule1", "rule2", "rule3", "rule6"]


def _snapshot(loaded):
    so_lines, masters = loaded
    # The live default plan_start_date is now None ("auto: start from today");
    # the golden trace is pinned to the historical fixed date so it stays
    # byte-identical without regeneration.
    trace = run_forward(PlanRun(so_lines=so_lines),
                        Config(plan_start_date=date(2025, 3, 1)), masters)
    return {r: trace[r]["output"] for r in SNAPSHOT_RULES}


def test_golden(loaded):
    snap = _snapshot(loaded)
    if os.environ.get("REGEN_GOLDEN") or not GOLDEN.exists():
        GOLDEN.write_text(json.dumps(snap, indent=2))
    expected = json.loads(GOLDEN.read_text())
    assert snap == expected
