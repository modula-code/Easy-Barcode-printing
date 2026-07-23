import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from xml.etree import ElementTree

from app import app
from planner_client import PlannerSyncError
from queue_store import (
    add_printed_part,
    clear_printed_parts,
    delete_printed_part,
    get_shift_plan,
    current_work_date,
    list_printed_parts,
    list_shift_plans,
    save_shift_plan,
    update_printed_part,
)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "queue.sqlite3")
        os.environ["PRINT_QUEUE_DB_PATH"] = self.db_path
        self.work_date = current_work_date()

    def tearDown(self):
        os.environ.pop("PRINT_QUEUE_DB_PATH", None)
        self.tmp.cleanup()

    def test_shared_queue_keeps_same_barcode_separate_by_po(self):
        first = add_printed_part("panel-1", 1, "po-1")
        add_printed_part("PANEL-1", 2, "PO-1")
        add_printed_part("PANEL-1", 4, "PO-2")

        items = list_printed_parts()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(
            {(item["po_number"], item["quantity"]) for item in items},
            {("PO-1", 3), ("PO-2", 4)},
        )

        updated = update_printed_part(first["id"], "PO-3", "panel-2", 5)
        self.assertEqual((updated["po_number"], updated["part_code"]), ("PO-3", "PANEL-2"))
        delete_printed_part(first["id"])
        self.assertEqual(len(list_printed_parts()["items"]), 1)

    def test_shift_plans_and_queue_survive_refresh(self):
        client = app.test_client()
        saved = client.post(
            "/api/shift-plans",
            data={
                "po_number": "po-1",
                "document": (io.BytesIO(b"%PDF-shift-label"), "label.pdf"),
            },
        )
        second = client.post(
            "/api/shift-plans",
            data={
                "po_number": "po-2",
                "document": (io.BytesIO(b"%PDF-second-label"), "second.pdf"),
            },
        )
        queued = client.post(
            "/api/print-queue",
            json={"po_number": "PO-1", "part_code": "PANEL-1", "quantity": 2},
        )

        refreshed_plans = app.test_client().get("/api/shift-plans")
        refreshed_queue = app.test_client().get("/api/print-queue")
        home = app.test_client().get("/")
        plans_page = app.test_client().get("/plans")
        pdf = app.test_client().get(
            f"/api/shift-plans/{saved.json['plan']['id']}/pdf"
        )

        self.assertEqual((saved.status_code, queued.status_code), (201, 201))
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            [plan["po_number"] for plan in refreshed_plans.json["plans"]],
            ["PO-1", "PO-2"],
        )
        self.assertEqual(refreshed_queue.json["items"][0]["quantity"], 2)
        self.assertEqual(pdf.data, b"%PDF-shift-label")
        self.assertIn(b"Today's PO labels", home.data)
        self.assertIn(b"Manage plans", home.data)
        self.assertIn(b"PO number", home.data)
        self.assertIn(b"Fetch Label PDF", home.data)
        self.assertNotIn(b"Save for today", home.data)
        self.assertIn(b"Save for today", plans_page.data)
        self.assertIn(b"Fetch from Odoo", plans_page.data)

    def test_new_day_archives_old_queue_and_clear_only_affects_today(self):
        with patch("queue_store.current_work_date", return_value="2026-07-15"):
            add_printed_part("PANEL-1", 2, "PO-1")
            plan = save_shift_plan("PO-1", "label.pdf", b"%PDF-old")
            self.assertEqual(get_shift_plan(plan["id"])["po_number"], "PO-1")

        with patch("queue_store.current_work_date", return_value="2026-07-16"):
            self.assertEqual(list_printed_parts()["items"], [])
            self.assertEqual(list_shift_plans(), [])
            add_printed_part("PANEL-2", 1, "PO-2")
            self.assertEqual(clear_printed_parts(), 1)
            self.assertEqual(list_printed_parts()["items"], [])
            archived = list_printed_parts("2026-07-15")["items"]
            self.assertEqual(archived[0]["part_code"], "PANEL-1")

    def test_existing_sqlite_rows_are_migrated_without_loss(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE printed_parts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO printed_parts (part_code, quantity, created_at)
                VALUES ('LEGACY-1', 3, '2026-07-15T00:00:00+00:00')
                """
            )

        items = list_printed_parts("2026-07-15")["items"]
        self.assertEqual((items[0]["part_code"], items[0]["quantity"]), ("LEGACY-1", 3))

    def test_queue_page_clear_and_dated_xlsx_export(self):
        add_printed_part("PANEL-1", 2, "PO-1")
        client = app.test_client()
        queue = client.get("/queue")
        export = client.get("/api/print-queue/export.xlsx")

        self.assertIn(b"Clear today's queue", queue.data)
        self.assertIn(b"PO number", queue.data)
        with zipfile.ZipFile(io.BytesIO(export.data)) as workbook:
            sheet = workbook.read("xl/worksheets/sheet1.xml")
        ElementTree.fromstring(sheet)
        self.assertIn(b"PO-1", sheet)
        self.assertIn(b"PANEL-1", sheet)

        cleared = client.delete("/api/print-queue")
        self.assertEqual(cleared.json["cleared"], 1)
        self.assertEqual(client.get("/api/print-queue").json["items"], [])

    def test_production_and_rejection_sync_once_and_update_good_quantity(self):
        client = app.test_client()

        def planner_result(payload):
            so_number = payload.get("soNumber") or "SO-1"
            allocations = (
                [{"soNumber": so_number, "quantity": payload["quantity"]}]
                if payload["action"] == "rejected"
                else [
                    {"soNumber": "SO-1", "quantity": 1},
                    {"soNumber": "SO-2", "quantity": 1},
                ]
            )
            return {
                "eventId": payload["eventId"],
                "planId": "plan-1",
                "planLabel": "July plan",
                "poNumber": payload["poNumber"],
                "soNumber": so_number,
                "partCode": payload["partCode"],
                "quantity": payload["quantity"],
                "action": payload["action"],
                "productionDay": 1,
                "panelTotal": 1,
                "allocations": allocations,
                "associated": [{"vendor": "HKK", "code": "FRAME-1", "quantity": 1}],
            }

        produced_payload = {
            "event_id": "event-produced-1",
            "po_number": "PO-1",
            "part_code": "PANEL-1",
            "quantity": 2,
            "work_date": self.work_date,
        }
        with patch("app.sync_production_event", side_effect=planner_result) as sync:
            produced = client.post("/api/print-queue", json=produced_payload)
            replay = client.post("/api/print-queue", json=produced_payload)
            rows = client.get("/api/print-queue").json["items"]
            row = rows[0]
            rejected = client.post(
                f"/api/print-queue/{row['id']}/reject",
                json={"event_id": "event-rejected-1", "quantity": 1},
            )

        self.assertEqual((produced.status_code, replay.status_code, rejected.status_code), (201, 201, 200))
        self.assertEqual(sync.call_count, 2)
        self.assertNotIn("soNumber", sync.call_args_list[0].args[0])
        self.assertEqual(sync.call_args_list[0].args[0]["workDate"], self.work_date)
        self.assertEqual(sum(item["quantity"] for item in rows), 2)
        self.assertEqual(
            sum(item["quantity"] for item in client.get("/api/print-queue").json["items"]),
            1,
        )
        self.assertEqual(sync.call_args.args[0]["action"], "rejected")
        self.assertEqual(sync.call_args.args[0]["planId"], "plan-1")

    def test_failed_planner_call_retries_same_event_without_double_counting(self):
        payload = {
            "event_id": "event-produced-retry",
            "po_number": "PO-1",
            "part_code": "PANEL-1",
            "quantity": 2,
            "work_date": self.work_date,
        }
        planner_result = {
            "eventId": payload["event_id"],
            "planId": "plan-1",
            "poNumber": payload["po_number"],
            "soNumber": "SO-1",
            "partCode": payload["part_code"],
            "quantity": payload["quantity"],
            "action": "produced",
            "productionDay": 1,
            "panelTotal": 2,
            "allocations": [{"soNumber": "SO-1", "quantity": 2}],
            "associated": [],
        }

        with patch(
            "app.sync_production_event",
            side_effect=[PlannerSyncError("Planner unavailable"), planner_result],
        ) as sync:
            failed = app.test_client().post("/api/print-queue", json=payload)
            retried = app.test_client().post("/api/print-queue", json=payload)

        self.assertEqual((failed.status_code, retried.status_code), (502, 201))
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(list_printed_parts()["items"][0]["quantity"], 2)

    def test_ledger_shows_what_planner_accepted_and_retries_what_it_did_not(self):
        payload = {
            "event_id": "event-produced-ledger",
            "po_number": "PO-1",
            "part_code": "PANEL-1",
            "quantity": 2,
            "work_date": self.work_date,
        }
        planner_result = {
            "eventId": payload["event_id"],
            "planId": "plan-1",
            "planLabel": "July plan",
            "poNumber": payload["po_number"],
            "soNumber": "SO-1",
            "partCode": payload["part_code"],
            "quantity": 2,
            "action": "produced",
            "productionDay": 3,
            "panelTotal": 7,
            "allocations": [{"soNumber": "SO-1", "quantity": 2}],
            "associated": [{"vendor": "HKK", "code": "FRAME-1", "quantity": 2}],
        }
        client = app.test_client()

        with patch("app.sync_production_event", side_effect=PlannerSyncError("Planner unavailable")):
            self.assertEqual(client.post("/api/print-queue", json=payload).status_code, 502)

        failed = client.get("/api/production-events").json
        self.assertEqual(failed["pending"], 1)
        self.assertEqual(failed["items"][0]["status"], "error")
        self.assertEqual(failed["items"][0]["error"], "Planner unavailable")
        # Nothing reached the plan, so nothing shows as good quantity yet.
        self.assertEqual(client.get("/api/print-queue").json["items"], [])

        with patch("app.sync_production_event", return_value=planner_result):
            retried = client.post(f"/api/production-events/{payload['event_id']}/retry")
        self.assertEqual(retried.status_code, 200)

        ledger = client.get("/api/production-events").json
        entry = ledger["items"][0]
        self.assertEqual(ledger["pending"], 0)
        self.assertEqual(entry["status"], "synced")
        self.assertEqual((entry["plan_label"], entry["production_day"]), ("July plan", 3))
        self.assertEqual(entry["allocations"], [{"soNumber": "SO-1", "quantity": 2}])
        self.assertEqual(entry["associated"][0]["code"], "FRAME-1")
        self.assertEqual(client.get("/api/print-queue").json["items"][0]["quantity"], 2)
        # A synced event must not be sent twice.
        self.assertEqual(
            client.post(f"/api/production-events/{payload['event_id']}/retry").status_code,
            409,
        )

    def test_panel_actions_require_a_work_date(self):
        client = app.test_client()
        home = client.get("/")
        missing = client.post(
            "/api/print-queue",
            json={
                "event_id": "event-no-date",
                "po_number": "PO-1",
                "part_code": "PANEL-1",
                "quantity": 1,
            },
        )
        selected_date = "2026-07-01"
        planner_result = {
            "eventId": "event-with-date",
            "planId": "plan-1",
            "poNumber": "PO-1",
            "soNumber": "SO-1",
            "partCode": "PANEL-1",
            "quantity": 1,
            "action": "produced",
            "productionDay": 1,
            "panelTotal": 1,
            "allocations": [{"soNumber": "SO-1", "quantity": 1}],
            "associated": [],
        }
        with patch("app.sync_production_event", return_value=planner_result) as sync:
            recorded = client.post(
                "/api/print-queue",
                json={
                    "event_id": "event-with-date",
                    "po_number": "PO-1",
                    "part_code": "PANEL-1",
                    "quantity": 1,
                    "work_date": selected_date,
                },
            )

        self.assertEqual((missing.status_code, missing.json["error"]), (400, "work_date is required."))
        self.assertEqual(recorded.status_code, 201)
        self.assertEqual(sync.call_args.args[0]["workDate"], selected_date)
        self.assertEqual(list_printed_parts(selected_date)["items"][0]["work_date"], selected_date)
        self.assertIn(b'id="work-date"', home.data)
        self.assertIn(b"Print and record 1", home.data)
        self.assertIn(b"Print and record all quantity", home.data)
        self.assertNotIn(b"Reprint", home.data)


if __name__ == "__main__":
    unittest.main()
