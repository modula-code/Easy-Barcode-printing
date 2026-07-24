"""Postgres queue store check.

    TEST_DATABASE_URL=postgresql://barcode:barcode@127.0.0.1:5432/barcode_test \
        python -m unittest test_queue_pg

Skipped when TEST_DATABASE_URL is unset. Truncates the three tables between
cases, so point it at a scratch database, never production.
"""

import os
import unittest
from unittest.mock import patch

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import queue_store  # noqa: E402
from queue_store import (  # noqa: E402
    add_printed_part,
    clear_printed_parts,
    complete_production_event,
    current_work_date,
    delete_printed_part,
    fail_production_event,
    get_shift_plan,
    list_history_dates,
    list_printed_parts,
    list_production_events,
    list_shift_plans,
    save_shift_plan,
    stage_production_event,
    update_printed_part,
)


@unittest.skipUnless(TEST_DATABASE_URL, "set TEST_DATABASE_URL to run")
class QueuePostgresTest(unittest.TestCase):
    def setUp(self):
        with queue_store._connect() as connection:
            connection.execute(
                "TRUNCATE printed_parts, production_events, shift_plans"
                " RESTART IDENTITY"
            )

    def test_same_part_merges_per_po_then_edits_and_deletes(self):
        first = add_printed_part("panel-1", 1, "po-1")
        add_printed_part("PANEL-1", 2, "PO-1")
        add_printed_part("PANEL-1", 4, "PO-2")

        items = list_printed_parts()["items"]
        self.assertEqual(
            {(item["po_number"], item["quantity"]) for item in items},
            {("PO-1", 3), ("PO-2", 4)},
        )

        updated = update_printed_part(first["id"], "PO-3", "panel-2", 5)
        self.assertEqual(
            (updated["po_number"], updated["part_code"], updated["quantity"]),
            ("PO-3", "PANEL-2", 5),
        )
        delete_printed_part(first["id"])
        self.assertEqual(len(list_printed_parts()["items"]), 1)

    def test_new_day_archives_queue_and_clear_only_touches_today(self):
        with patch("queue_store.current_work_date", return_value="2026-07-15"):
            add_printed_part("PANEL-1", 2, "PO-1")
            plan = save_shift_plan("PO-1", "label.pdf", b"%PDF-old")
            self.assertEqual(get_shift_plan(plan["id"])["po_number"], "PO-1")
            self.assertEqual(get_shift_plan(plan["id"])["label_pdf"], b"%PDF-old")

        with patch("queue_store.current_work_date", return_value="2026-07-16"):
            self.assertEqual(list_printed_parts()["items"], [])
            self.assertEqual(list_shift_plans(), [])
            add_printed_part("PANEL-2", 1, "PO-2")
            self.assertEqual(clear_printed_parts(), 1)
            archived = list_printed_parts("2026-07-15")["items"]
            self.assertEqual(archived[0]["part_code"], "PANEL-1")
        self.assertIn("2026-07-15", list_history_dates())

    def test_shift_plan_upsert_replaces_the_pdf_for_the_same_po(self):
        first = save_shift_plan("PO-1", "a.pdf", b"%PDF-a")
        second = save_shift_plan("po-1", "b.pdf", b"%PDF-b")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(get_shift_plan(second["id"])["label_pdf"], b"%PDF-b")
        self.assertEqual(len(list_shift_plans()), 1)

    def test_produced_then_rejected_moves_the_synced_quantity(self):
        today = current_work_date()
        staged = stage_production_event(
            "event-produced-1", "produced", "PO-1", "", "PANEL-1", 6, today
        )
        self.assertEqual(staged["status"], "pending")

        row = complete_production_event(
            "event-produced-1",
            {"planId": "PLAN-1", "soNumber": "SO-1", "planLabel": "Shift A"},
        )
        self.assertEqual((row["status"], row["quantity"], row["so_number"]), ("synced", 6, "SO-1"))

        # Re-staging the same event_id is a no-op, not a duplicate.
        self.assertEqual(
            stage_production_event(
                "event-produced-1", "produced", "PO-1", "", "PANEL-1", 6, today
            )["event_id"],
            "event-produced-1",
        )

        stage_production_event(
            "event-rejected-1",
            "rejected",
            "PO-1",
            "SO-1",
            "PANEL-1",
            2,
            today,
            target_row_id=row["id"],
        )
        after = complete_production_event("event-rejected-1", {"planId": "PLAN-1"})
        self.assertEqual(after["quantity"], 4)

        ledger = list_production_events(today)
        self.assertEqual(ledger["pending"], 0)
        self.assertEqual(
            [item["event_id"] for item in ledger["items"]],
            ["event-rejected-1", "event-produced-1"],
        )

    def test_produced_splits_across_allocations_and_failures_are_recorded(self):
        today = current_work_date()
        stage_production_event(
            "event-split-1", "produced", "PO-9", "", "PANEL-9", 5, today
        )
        complete_production_event(
            "event-split-1",
            {
                "planId": "PLAN-9",
                "allocations": [
                    {"soNumber": "SO-A", "quantity": 3},
                    {"soNumber": "SO-B", "quantity": 2},
                ],
            },
        )
        self.assertEqual(
            {(item["so_number"], item["quantity"]) for item in list_printed_parts()["items"]},
            {("SO-A", 3), ("SO-B", 2)},
        )

        stage_production_event(
            "event-split-2", "produced", "PO-9", "", "PANEL-9", 1, today
        )
        fail_production_event("event-split-2", "Planner unreachable")
        ledger = list_production_events(today)
        self.assertEqual(ledger["pending"], 1)
        failed = next(i for i in ledger["items"] if i["event_id"] == "event-split-2")
        self.assertEqual((failed["status"], failed["error"]), ("error", "Planner unreachable"))

    def test_rejecting_more_than_was_synced_is_refused(self):
        today = current_work_date()
        stage_production_event(
            "event-produced-2", "produced", "PO-2", "", "PANEL-2", 2, today
        )
        row = complete_production_event(
            "event-produced-2", {"planId": "PLAN-2", "soNumber": "SO-2"}
        )
        with self.assertRaises(ValueError):
            stage_production_event(
                "event-rejected-2",
                "rejected",
                "PO-2",
                "SO-2",
                "PANEL-2",
                3,
                today,
                target_row_id=row["id"],
            )

    def test_legacy_production_events_table_gains_sequence_column(self):
        with queue_store._connect() as connection:
            connection.execute("SAVEPOINT legacy_schema")
            try:
                connection.execute(
                    """
                    INSERT INTO production_events
                        (event_id, action, po_number, so_number, part_code,
                         quantity, work_date, created_at, updated_at)
                    VALUES
                        ('event-legacy-1', 'produced', 'PO-1', '', 'PANEL-1',
                         1, '2026-07-25', '2026-07-25T00:00:00+00:00',
                         '2026-07-25T00:00:00+00:00')
                    """
                )
                connection.execute("ALTER TABLE production_events DROP COLUMN seq")
                queue_store._ensure_schema(connection)
                row = connection.execute(
                    "SELECT seq FROM production_events WHERE event_id = 'event-legacy-1'"
                ).fetchone()
                self.assertIsInstance(row["seq"], int)
            finally:
                connection.execute("ROLLBACK TO SAVEPOINT legacy_schema")


if __name__ == "__main__":
    unittest.main()
