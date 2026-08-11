"""Rule 4 — Setup time per process (calc helper, consumed inside Rule 6).

Machine occupancy for a process = cycle_time x qty + setup_time, where setup
defaults to 90 min (config.setup_time_min). A missing cycle time counts as 0
run time, so the process still occupies the machine for the setup.

NOTE: Rule 6 charges this setup to **CNC/VMC machining only** (setup = machine
programming time). Manual/finishing steps get 0 setup — Rule 6 decides per the
chosen machine (``rule6_allocate._is_setup_machine``); this helper stays generic.
"""
from __future__ import annotations


def occupancy_minutes(cycle_time, qty, config=None) -> float:
    setup = config.setup_time_min if config else 90
    run_time = (cycle_time or 0.0) * (qty or 0.0)
    return run_time + setup


def run(item, config=None, notes=None, masters=None, **kw):
    """Test/trace shim: ``item`` is a dict with cycle_time and qty."""
    notes = notes if notes is not None else []
    occ = occupancy_minutes(item.get("cycle_time"), item.get("qty"), config)
    notes.append(
        f"occupancy = {item.get('cycle_time')} x {item.get('qty')} "
        f"+ {config.setup_time_min if config else 90} setup = {occ} min"
    )
    return {"occupancy_min": occ}
