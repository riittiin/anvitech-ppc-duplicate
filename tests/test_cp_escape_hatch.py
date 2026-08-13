"""The design rests on reaching pyjobshop's underlying CpModel. That is internal
API, so it is pinned by a canary rather than trusted. If this fails after a
pyjobshop upgrade, STOP: the engine cannot express Rule 1 or the fairness
objective without it, and every other cp test will fail in a less obvious way.

Verified against pyjobshop 0.0.9 on 2026-08-14."""
import pytest

pyjobshop = pytest.importorskip("pyjobshop")


def test_the_cpmodel_escape_hatch_still_works():
    from pyjobshop import Model
    from pyjobshop.solvers.ortools.CPModel import CPModel

    m = Model()
    machines = [m.add_machine(name=f"M{i}") for i in range(2)]
    for j in range(3):
        job = m.add_job(due_date=100, name=f"J{j}")
        task = m.add_task(job=job, name=f"T{j}")
        for mach in machines:
            m.add_mode(task, mach, 60)
    m.set_objective(weight_total_tardiness=1)

    cp = CPModel(m.data())
    model, variables = cp.model, cp.variables

    # The four handles the engine needs.
    assert len(variables.job_vars) == 3          # .end is the order's completion
    assert len(variables.tardiness_vars) == 3    # lazily built, must be reachable
    assert len(variables.mode_vars) == 6
    assert (0, 0) in variables.assign_vars       # (task, resource) -> present bool
    assert hasattr(variables.assign_vars[(0, 0)], "present")

    # Our own variables and our own objective must both be accepted, and ours
    # must WIN — pyjobshop already set one in CPModel.__init__.
    mine = model.new_bool_var("mine")
    model.add(mine == 1)
    days = []
    for tardiness in variables.tardiness_vars:
        d = model.new_int_var(0, 60, "")
        model.add(d * 1440 >= tardiness)
        days.append(d)
    model.minimize(250_000 * sum(days) + sum(d * 0 for d in days))

    result = cp.solve(time_limit=10, display=False)
    assert str(result.status) in ("SolveStatus.OPTIMAL", "SolveStatus.FEASIBLE")
    # Three 60-minute tasks on two machines against a due date of 100: one must
    # finish at 120, so exactly one late-day is unavoidable.
    assert result.objective == 250_000
