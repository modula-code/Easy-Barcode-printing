"""
Sweep every purchase order for one vendor against the Drawing/ECO tracker.

The print-time check in eco_tracker only sees the PO an operator happens to scan.
This answers the other question: across all of a vendor's orders, which panels have
had a drawing released after the order was approved?

    python eco_audit.py                         # locked POs for the default vendor
    python eco_audit.py --state purchase        # a different state
    python eco_audit.py --vendor 7 --csv out.csv

Read-only: it queries Odoo and the sheet, writes nothing back to either.
"""

import argparse
import csv
import os
import sys
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

import eco_tracker  # noqa: E402
import odoo_client  # noqa: E402

DEFAULT_VENDOR_ID = 7  # Dongkwang Precision India Pvt. Ltd - 27AADCD4956C1ZR
LINE_BATCH = 200  # order ids per purchase.order.line query


def purchase_orders(execute, vendor_id, state):
    return odoo_client._search_read(
        execute,
        "purchase.order",
        [("partner_id", "=", vendor_id), ("state", "=", state)],
        ["id", "name", "partner_ref", "date_approve", "date_order"],
        order="date_approve desc",
    )


def panels_by_order(execute, order_ids):
    """order id -> sorted panel part codes, read off the line's product_id label."""
    panels: dict[int, set[str]] = {}
    for start in range(0, len(order_ids), LINE_BATCH):
        batch = order_ids[start : start + LINE_BATCH]
        for line in odoo_client._search_read(
            execute,
            "purchase.order.line",
            [("order_id", "in", batch)],
            ["order_id", "product_id"],
        ):
            order_id = odoo_client._many2one_id(line.get("order_id"))
            code = odoo_client._field_code(
                odoo_client._many2one_name(line.get("product_id"))
            )
            if order_id is not None and code:
                panels.setdefault(order_id, set()).add(code)
    return {order_id: sorted(codes) for order_id, codes in panels.items()}


def audit(vendor_id=DEFAULT_VENDOR_ID, state="done"):
    """
    Returns {"findings": [...], "orders": [...], plus counts}.

    `orders` is the same data grouped one entry per affected purchase order, which
    is how the Drawing changes page lists it.
    """
    execute, _ = odoo_client._connect()
    orders = purchase_orders(execute, vendor_id, state)
    if not orders:
        return {
            "vendor_id": vendor_id, "state": state, "po_count": 0,
            "line_count": 0, "index_size": 0, "findings": [], "orders": [],
        }

    panels = panels_by_order(execute, [order["id"] for order in orders])
    index_size = len(eco_tracker.get_index())

    findings, affected = [], []
    for order in orders:
        po_date = order.get("date_approve") or order.get("date_order")
        alerts = eco_tracker.check_codes(panels.get(order["id"], []), po_date)
        if not alerts:
            continue
        rows = [
            {
                "po_number": order["name"],
                "po_approved": alert["po_date"],
                "partner_ref": order.get("partner_ref") or "",
                "panel": alert["code"],
                "revision": alert["revision"],
                "drawing_code": alert["drawing_code"],
                "released_on": alert["released_on"],
                "description": alert["description"],
            }
            for alert in alerts
        ]
        findings.extend(rows)
        affected.append(
            {
                "po_number": order["name"],
                "po_approved": rows[0]["po_approved"],
                "partner_ref": rows[0]["partner_ref"],
                "latest_release": max(row["released_on"] for row in rows),
                "panels": [
                    {key: row[key] for key in
                     ("panel", "revision", "drawing_code", "released_on", "description")}
                    for row in rows
                ],
            }
        )

    return {
        "vendor_id": vendor_id,
        "state": state,
        "po_count": len(orders),
        "line_count": sum(len(codes) for codes in panels.values()),
        "index_size": index_size,
        "findings": findings,
        "orders": affected,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", type=int, default=DEFAULT_VENDOR_ID)
    parser.add_argument("--state", default="done")
    parser.add_argument("--csv", default="")
    parser.add_argument("--limit", type=int, default=25, help="rows printed to screen")
    args = parser.parse_args()

    if not eco_tracker.is_enabled():
        print(
            "ECO_REVISION_CHECK_ENABLED is off, so every check would return nothing.",
            file=sys.stderr,
        )
        return 1

    report = audit(args.vendor, args.state)
    findings = report["findings"]
    print(
        f"{report['po_count']} purchase orders in state {args.state!r} "
        f"for vendor {args.vendor}"
    )
    print(f"{report['line_count']} order lines with a part code")
    print(f"tracker index: {report['index_size']} items\n")
    if not findings:
        print("No panel on these orders has a newer drawing.")
        return 0

    print(f"{len(findings)} affected lines across {len(report['orders'])} purchase orders\n")
    width = max(len(row["po_number"]) for row in findings)
    for row in findings[: args.limit]:
        print(
            f"  {row['po_number']:<{width}}  approved {row['po_approved']}  "
            f"{row['panel']:<12} -> {row['revision']}"
            f"{' / ' + row['drawing_code'] if row['drawing_code'] else ''}"
            f"  released {row['released_on']}"
        )
    if len(findings) > args.limit:
        print(f"  ... {len(findings) - args.limit} more")

    top = Counter(row["panel"] for row in findings).most_common(10)
    print("\nmost affected panels:")
    for panel, count in top:
        print(f"  {panel:<12} on {count} orders")

    if args.csv:
        parent = os.path.dirname(os.path.abspath(args.csv))
        os.makedirs(parent, exist_ok=True)
        with open(args.csv, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(findings[0]))
            writer.writeheader()
            writer.writerows(findings)
        print(f"\nwrote {len(findings)} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
