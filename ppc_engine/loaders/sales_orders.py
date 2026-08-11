"""Read the "Sales Order (SO) list" into Order objects.

We keep only the columns we plan against (RULES.md Part 1 §5): SO number, item code,
item name, SO Delivery Qty (the planning quantity), and SO Delivery Date (the due
date).
"""

from __future__ import annotations

from ppc_engine.domain.order import Order
from ppc_engine.loaders.masters_loader import _as_date
from ppc_engine.loaders.workbook import Table, find_sheet, locate_header_row, rows_of

# A far-future fallback due date so an unparseable date never crashes the load; such
# orders simply never look late (and are rare in practice).
from datetime import date as _date

_FALLBACK_DUE = _date(2999, 12, 31)


def load_orders(wb) -> list[Order]:
    """Read all sales-order lines into Orders."""
    ws = find_sheet(wb, "Sales Order (SO) list", "Sales Order SO list", "SO list")
    rows = rows_of(ws)
    h = locate_header_row(rows, "SONo", "SO No", "Sales Item Code")
    t = Table.from_rows(rows, h)
    c_so = t.col("SONo", "SO No")
    c_item = t.col("Sales Item Code")
    c_name = t.col("Sales Item Name")
    c_dqty = t.col("SO Delivery Qty")
    c_ddate = t.col("SO Delivery Date")

    orders: list[Order] = []
    for row in t.data_rows:
        so_no = str(t.get(row, c_so) or "").strip()
        item = str(t.get(row, c_item) or "").strip()
        if not so_no or not item:
            continue
        dqty = t.get(row, c_dqty)
        qty = int(dqty) if isinstance(dqty, (int, float)) else 0
        due = _as_date(t.get(row, c_ddate)) or _FALLBACK_DUE
        name = str(t.get(row, c_name) or "").strip()
        orders.append(Order(so_no=so_no, item_code=item, item_name=name, qty=qty, due_date=due))
    return orders
