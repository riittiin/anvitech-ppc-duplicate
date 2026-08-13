"""A constraint-programming scheduler and optimizer.

One model decides the job sequence, the machine, the crew roster and the overlap
together, under all four shop rules. The previous engine split those decisions
between a greedy dispatcher and a local search around it, so neither could see
what the other was doing.

Nothing here imports pyjobshop at package level: the REPLAY path (domain,
windows, genome, decode, report) runs on Render, where pyjobshop is deliberately
not installed. Only model.py, rules.py, objective.py and solve.py need it, and
they are worker-only.

Spec: docs/superpowers/specs/2026-08-14-cp-scheduler-optimizer-design.md
"""

# Bumped whenever a change here can move a plan. api._inputs_signature folds
# this in (under scheduler == "cp" only), so a genome solved under an older
# version is correctly flagged stale rather than replayed under new semantics
# behind a green banner.
SCHEDULER_FINGERPRINT = "cp-engine-v1"
