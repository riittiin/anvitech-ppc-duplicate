"""Alternating descent over two genomes: the JOB FLOW and the CREW.

Inside a shift, who mans what is SOLVED exactly (assign.py), so no evaluation is
ever spent on a roster that is locally dominated. What the exact matching cannot
know is the future — it will man the machine that is busy at 08:00 and leave dark
the machine whose big batch releases at 11:00. That lookahead is the crew genome,
and it is what this searches, alongside the job order.

A joint random walk over two genomes wastes evaluations; alternating descent does
not — hold the crew fixed and climb the sequence, then hold the sequence fixed
and climb the crew, and repeat until neither moves. The crew genome is
deliberately a PERMUTATION, the same object type as the sequence, so one set of
moves serves both.

Guarantees, matching the live search so the A/B is fair:

  * **Never worse than the starting point.** The dispatch-rule seeds are evaluated
    first, the best of them becomes the incumbent, and a move is accepted only if
    it STRICTLY improves. The incumbent IS the returned result — there is no
    separate "best seen" ledger, deliberately: a ledger would keep the promise
    true by bookkeeping even if the acceptance rule were broken, and this promise
    is the reason an owner will press Apply.
  * **Deterministic.** A fixed seed and eval budget give the same result every
    run, in any process: every collection that feeds a decision is a sorted list,
    never a set or a dict in iteration order, and the only randomness is one
    seeded `random.Random`.
  * **Budgeted and cancellable.** `should_cancel()` stops the search where it
    stands and the incumbent so far is returned.
  * **Objective-blind.** It knows nothing about how the score is built.

Some genomes are INFEASIBLE, and the crew genome is why. Rank a downstream
machine ahead of the one that feeds it and the shop's only operator mans an empty
chuck every shift while the upstream machine stays dark — the plan never places
anything and `scheduler.schedule` fails loud, exactly as it should. A candidate
like that is scored `+inf` and dropped, never crashed on; but if NO genome could
be planned, the scheduler's own typed error is re-raised, because then it is the
book that cannot be built and the caller must hear about it.

Because a plan costs ~35 ms at 50 jobs and the contest builds thousands of them,
every decoded genome is memoised: revisiting one costs a dict lookup and does not
spend budget.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from roster_engine import objective, scheduler

# How much of the budget the FIRST job-sequence phase may take before the crew
# gets its turn. Without it, on a small budget the sequence phase eats everything
# and the second dimension is never searched at all. A starting point, to be
# MEASURED on the owner's book rather than assumed.
_JOB_SHARE = 0.6

# A phase gives up after this many consecutive draws that cost nothing — a
# neighbour identical to the incumbent, or one already in the memo. It is not a
# patience setting (the budget is what bounds a real search); it exists so a
# genome whose whole neighbourhood is one or two permutations — two jobs, two
# machines — cannot spin forever drawing the same move. Cache hits do not spend
# budget, so without this bound the loop would never terminate.
_SATURATED = 24


@dataclass
class Result:
    sequence: list = field(default_factory=list)
    crew_rank: dict = field(default_factory=dict)
    score: float = float("inf")
    metrics: object = None
    evaluations: int = 0
    baseline_score: float = float("inf")
    cancelled: bool = False


class _Evaluator:
    """Decode + score, memoised — the search revisits genomes constantly."""

    def __init__(self, jobs, shop, config, overlap, frozen, on_eval, should_cancel,
                 budget):
        self._jobs, self._shop, self._config = jobs, shop, config
        self._overlap, self._frozen = overlap, frozen
        self._on_eval, self._should_cancel = on_eval, should_cancel
        self._budget = max(0, int(budget))
        self._cache: dict = {}
        self.count = 0
        self.cancelled = False
        self.infeasible = 0
        self.first_error = None

    @property
    def exhausted(self) -> bool:
        return self.cancelled or self.count >= self._budget

    def __call__(self, sequence, crew):
        """Returns (score, metrics, was_free). ``was_free`` is True when the answer
        came out of the memo or the search is over — the caller needs to tell a
        genuinely-explored neighbour from one that cost nothing, or a saturated
        neighbourhood looks like an endless supply of new plans."""
        key = (tuple(sequence), tuple(crew))
        hit = self._cache.get(key)
        if hit is not None:
            return hit[0], hit[1], True
        if self.exhausted:
            return float("inf"), None, True
        if self._should_cancel is not None and self._should_cancel():
            self.cancelled = True
            return float("inf"), None, True
        try:
            plan = scheduler.schedule(self._jobs, list(sequence), self._shop,
                                      self._config, overlap=self._overlap,
                                      crew_rank=_rank(crew), frozen=self._frozen)
        except scheduler.Unschedulable as exc:
            # This genome cannot be run, so it is infinitely bad — but it is not a
            # reason to abandon the search. Only `Unschedulable` is caught: any
            # other exception is a defect in the engine and must stay loud.
            self.count += 1
            self.infeasible += 1
            if self.first_error is None:
                self.first_error = exc
            self._cache[key] = (float("inf"), None)
            if self._on_eval is not None:
                self._on_eval(self.count, None)
            return float("inf"), None, False
        metrics = objective.compute_metrics(plan, self._jobs, self._config)
        value = objective.score(metrics, self._config)
        self.count += 1
        self._cache[key] = (value, metrics)
        if self._on_eval is not None:
            self._on_eval(self.count, metrics)
        return value, metrics, False


def optimize(jobs, shop, config, *, overlap, budget_evals=150, seed=42,
             on_eval=None, should_cancel=None, frozen=None) -> Result:
    """Search the job sequence and the crew priorities together."""
    if not jobs:
        return Result()

    rng = random.Random(int(seed))
    machines = sorted(shop.machining_ids)      # sorted: a frozenset otherwise
    budget = max(0, int(budget_evals))         # re-orders between processes
    ev = _Evaluator(jobs, shop, config, overlap, frozen, on_eval, should_cancel,
                    budget)

    seeds = _seed_sequences(jobs)
    best_seq, best_crew = seeds[0], list(machines)
    best_score, best_metrics = float("inf"), None
    for candidate in seeds:
        value, metrics, _free = ev(candidate, best_crew)
        if value < best_score:
            best_seq, best_score, best_metrics = candidate, value, metrics
        if ev.exhausted:
            break
    baseline = best_score

    job_ceiling = int(budget * _JOB_SHARE)
    while not ev.exhausted:
        spent = ev.count
        best_seq, best_score, best_metrics = _climb(
            best_seq, best_score, best_metrics, ev, rng,
            lambda seq: (seq, best_crew), job_ceiling)
        best_crew, best_score, best_metrics = _climb(
            best_crew, best_score, best_metrics, ev, rng,
            lambda crew: (best_seq, crew), None)
        capped = job_ceiling is not None
        job_ceiling = None                    # the share applies to round 1 only
        if ev.count == spent and not capped:
            # Both neighbourhoods saturated with budget still in hand: there is
            # nothing left to try. Checked only on an UNCAPPED round, or a round
            # in which the share had already stopped the sequence phase would end
            # the whole search with budget unspent.
            break

    if best_score == float("inf") and ev.first_error is not None:
        # Nothing this search tried could be built. That is not a bad plan, it is
        # a book the shop cannot run, and the scheduler's own typed error names
        # the blocking step — fail loud rather than hand back an empty plan.
        raise ev.first_error

    return Result(sequence=list(best_seq), crew_rank=_rank(best_crew),
                  score=best_score, metrics=best_metrics, evaluations=ev.count,
                  baseline_score=baseline, cancelled=ev.cancelled)


def _climb(genome, value, metrics, ev, rng, genomes, ceiling):
    """First-improvement hill climb on one genome, the other held fixed.

    ``genomes(candidate)`` returns the (sequence, crew) pair to evaluate, so this
    one climber serves both dimensions. Only a strictly better score is accepted:
    that, and nothing else, is what makes the returned plan never worse than the
    seed it started from.

    ``ceiling`` is an absolute ceiling on ``ev.count`` for this phase — the
    sequence/crew budget SHARE — or None for "no phase cap". The eval budget
    itself is NOT re-checked here: ``ev.exhausted`` is its one enforcement point,
    so there is exactly one place that can get it wrong.
    """
    genome = list(genome)
    idle = 0
    while (not ev.exhausted and idle < _SATURATED
           and (ceiling is None or ev.count < ceiling)):
        candidate = _neighbour(genome, rng)
        if candidate is None:
            idle += 1
            continue
        sequence, crew = genomes(candidate)
        cand_value, cand_metrics, free = ev(sequence, crew)
        # "Did this draw cost a plan?" is a fact about the evaluator, and the
        # saturation guard must not depend on whether the move was ACCEPTED —
        # otherwise a rule that accepts everything (including memo hits, which
        # spend no budget) never lets `idle` grow and the phase spins forever.
        idle = idle + 1 if free else 0
        if cand_value < value:
            genome, value, metrics = candidate, cand_value, cand_metrics
    return genome, value, metrics


def _neighbour(genome, rng):
    """One random insertion, swap or block move — the three that carry a
    permutation search. Returns None when the draw left the genome unchanged; the
    caller treats that as a free draw, not as an evaluation."""
    n = len(genome)
    if n < 2:
        return None
    out = list(genome)
    kind = rng.randrange(3 if n >= 4 else 2)
    if kind == 0:                                     # swap two positions
        i = rng.randrange(n)
        j = rng.randrange(n - 1)
        if j >= i:
            j += 1
        out[i], out[j] = out[j], out[i]
    elif kind == 1:                                   # lift one job, re-insert it
        i = rng.randrange(n)
        item = out.pop(i)
        out.insert(rng.randrange(n), item)
    else:                                             # move a contiguous block
        a = rng.randrange(n - 1)
        size = rng.randint(2, min(4, n - a))
        chunk = out[a:a + size]
        del out[a:a + size]
        at = rng.randrange(len(out) + 1)
        out[at:at] = chunk
    return None if out == list(genome) else out


def _seed_sequences(jobs) -> list:
    """Dispatch-rule starting points, best-first.

    EDD is the strongest opener under an on-time objective, so it goes first: on a
    budget so small that only one seed is ever decoded, that one is the good one,
    and the search is never worse than it. The book's own order is included too —
    on a re-plan it is the order the floor is already following.
    """
    far = max((j.due for j in jobs if j.due is not None), default=None)

    def due_key(job):
        due = job.due if job.due is not None else far
        return (due is None, due.toordinal() if due is not None else 0, job.key)

    edd = [j.key for j in sorted(jobs, key=due_key)]
    spt = [j.key for j in sorted(jobs, key=lambda j: (_work(j), j.key))]
    seeds, seen = [], set()
    for candidate in (edd, spt, list(reversed(spt)), [j.key for j in jobs]):
        marker = tuple(candidate)
        if marker not in seen:
            seen.add(marker)
            seeds.append(candidate)
    return seeds


def _work(job) -> float:
    """Total cutting minutes still owed by this job — SPT/LPT's ordering key.
    Uses ``qty_for`` per step, so a part-finished job is ranked by what is LEFT."""
    return sum(job.qty_for(op.seq) * float(op.cycle_min or 0.0) for op in job.ops)


def _rank(machines) -> dict:
    """Machine ids in priority order -> {machine id: rank}. Always a permutation:
    every machining machine appears exactly once and the ranks are 0..n-1, which
    is what `roster.roster_for_shift` orders its columns by."""
    return {mid: i for i, mid in enumerate(machines)}
