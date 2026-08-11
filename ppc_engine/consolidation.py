"""Order consolidation — merge same-item orders with nearby due dates into one batch.

The idea (owner's): if two orders are the SAME item and their delivery dates are close,
producing them as one combined batch pays the machining setup once instead of twice and
runs a single flow — which can pull BOTH deliveries earlier. The risk is that a bigger
batch delays the earlier-due order, so it only helps within a due-date window; the best
window is book-specific and auto-tuned (see engine.optimize.tune_consolidation).

`consolidate` produces synthetic *batch* orders (combined qty, EARLIEST due date — the
tightest constraint) plus a map back to the original orders, so the scheduler plans
batches while tardiness is still measured per original order against its own due date.
"""

from __future__ import annotations

from collections import defaultdict

from ppc_engine.domain.order import Order

# Prefix for a consolidated batch's synthetic SO number (so its key never collides
# with a real order's key).
_BATCH_PREFIX = "CONS:"


def consolidate(
    orders: list[Order], window_days: float
) -> tuple[list[Order], dict[tuple[str, str], list[tuple[str, str]]]]:
    """Group same-item orders whose due dates fall within ``window_days`` into batches.

    Returns:
        batches: the orders to actually schedule (a mix of real single orders and
                 merged batch orders), each a normal Order.
        expand:  {batch key → [original order keys it covers]}. A completion time for a
                 batch key applies to every original order it covers.

    ``window_days <= 0`` means no consolidation (each order is its own batch).
    Greedy clustering: within an item, orders are sorted by due date and grouped while
    each next order's due date is within ``window_days`` of the cluster's EARLIEST due.
    """
    if not window_days or window_days <= 0:
        return list(orders), {o.key: [o.key] for o in orders}

    by_item: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_item[o.item_code].append(o)

    batches: list[Order] = []
    expand: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for item, group in by_item.items():
        group = sorted(group, key=lambda o: (o.due_date, o.so_no))
        i = 0
        while i < len(group):
            base_due = group[i].due_date
            j = i + 1
            while j < len(group) and (group[j].due_date - base_due).days <= window_days:
                j += 1
            cluster = group[i:j]
            if len(cluster) == 1:
                b = cluster[0]  # nothing to merge — keep the real order
                batches.append(b)
                expand[b.key] = [b.key]
            else:
                batch = Order(
                    so_no=_BATCH_PREFIX + "+".join(c.so_no for c in cluster),
                    item_code=item,
                    item_name=cluster[0].item_name,
                    qty=sum(c.qty for c in cluster),
                    due_date=min(c.due_date for c in cluster),  # tightest of the group
                )
                batches.append(batch)
                expand[batch.key] = [c.key for c in cluster]
            i = j
    return batches, expand
