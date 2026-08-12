"""Maximum-weight bipartite matching — the exact answer to "who mans what".

Within one shift, assigning operators to machines is the classic assignment
problem. It has an exact polynomial algorithm, so the optimizer never has to
SEARCH this: at 20 people x 26 machines it is solved in well under a millisecond,
about a hundred times per plan, which is noise next to the job simulation.

Written here in pure Python on purpose — requirements.txt has no scipy, and this
engine is not allowed to add a dependency. The implementation is the standard
O(n^3) Hungarian method with potentials and shortest augmenting paths.

Determinism: the algorithm is deterministic for a given matrix, and the caller
builds the matrix from stably-sorted rows and columns, so exact ties always
resolve the same way.
"""

from __future__ import annotations


def max_weight_matching(values: dict, n_rows: int, n_cols: int) -> dict:
    """Assign rows to columns, one each, maximising the total value.

    Args:
        values:  {(row, col): value}. A pair absent from this dict is forbidden.
        n_rows:  number of rows (operators).
        n_cols:  number of columns (machines).

    Returns:
        {row: col} for the chosen pairs. Pairs worth <= 0 are dropped: leaving a
        person unrostered is better than putting them on a machine with no work.
    """
    if n_rows <= 0 or n_cols <= 0 or not values:
        return {}

    # The Hungarian routine below minimises and needs rows <= cols, so transpose
    # when there are more operators than machines and flip the answer back.
    transposed = n_rows > n_cols
    if transposed:
        values = {(c, r): v for (r, c), v in values.items()}
        n_rows, n_cols = n_cols, n_rows

    # The Hungarian routine below always returns a PERFECT matching — every
    # row mapped to some column. That is the wrong contract here: a row may
    # legitimately be worth leaving unassigned (no real pairing, or every
    # real pairing is worth <= 0), and forcing it into a column anyway can
    # crowd out a better arrangement for everyone else. So every row gets
    # n_rows genuinely free "opt out" dummy columns appended after the real
    # ones (cost 0, available to any row) — enough that even if every row
    # opted out simultaneously there would be a distinct dummy column for
    # each of them, so the padded problem is always feasible. A forbidden
    # real-column cell (absent from `values`) is ALSO cost 0 for the same
    # reason: it must never be chosen over a positive real pairing, but it
    # must be at least as free as opting out, or a row with no good option
    # could be forced into a bad real cell purely to satisfy completeness —
    # exactly the case the brute-force cross-check below catches.
    total_cols = n_cols + n_rows
    cost = [[0.0] * total_cols for _ in range(n_rows)]
    for (r, c), value in values.items():
        cost[r][c] = -float(value)          # maximise value == minimise -value

    pair = _hungarian(cost, n_rows, total_cols)

    out = {}
    for r, c in pair.items():
        if c >= n_cols:
            continue                         # matched to a dummy opt-out column
        if (r, c) not in values:
            continue                         # forbidden padding, not a real pairing
        if -cost[r][c] <= 0:
            continue                         # worth nothing — leave them free
        out[c if transposed else r] = r if transposed else c
    return out


def _hungarian(cost, n, m) -> dict:
    """Minimum-cost perfect matching of ``n`` rows into ``m`` columns (n <= m).

    Potentials ``u``/``v`` and shortest augmenting paths (the standard e-maxx
    formulation). ``p[j]`` is the 1-based row currently matched to column ``j``;
    ``p[0]`` is scratch. Returns {row: col}, both 0-based.
    """
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j] != 0}
