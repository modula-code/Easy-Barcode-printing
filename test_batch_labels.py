import io
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("PLANNER_RETRY_INTERVAL", "0")

from pypdf import PdfReader, PdfWriter

import app
from pdf_service import PrintArtifact, _artifact_cache
from queue_store import _part_qty


def _label_token(pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return _artifact_cache.put(PrintArtifact(output.getvalue(), pages))


class BatchLabelsTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_quantity_validation(self):
        self.assertEqual(_part_qty("part1", 500), ("PART1", 500))
        self.assertEqual(_part_qty("part1", "1"), ("PART1", 1))
        for quantity in (0, -1, 501, 1.5, True, None, "", "1.5", float("inf")):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                _part_qty("part1", quantity)

    def test_print_count_slices_pages_and_caps_at_label_count(self):
        token = _label_token(4)
        for count, pages in ((None, 4), (1, 1), (3, 3), (4, 4)):
            query = {} if count is None else {"count": count}
            response = self.client.get(f"/api/print-pages/{token}.pdf", query_string=query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(PdfReader(io.BytesIO(response.data)).pages), pages)
        for count in (0, 5):
            response = self.client.get(f"/api/print-pages/{token}.pdf?count={count}")
            self.assertEqual(response.status_code, 400)
        redirect = self.client.get(f"/print/{token}?count=2")
        self.assertTrue(redirect.location.endswith(f"/api/print-pages/{token}.pdf?count=2"))

    def test_record_rejects_quantity_above_label_count(self):
        token = _label_token(3)
        event = dict(event_id="batch-3", po_number="PO001", part_code="PART1",
                     work_date="2026-11-04", label_token=token)
        with patch.object(app, "_planner_sync_enabled", return_value=False), \
             patch.object(app, "stage_production_event") as stage:
            too_many = self.client.post("/api/print-queue", json={**event, "quantity": 4})
            expired = self.client.post("/api/print-queue",
                                       json={**event, "quantity": 1, "label_token": "gone"})
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("cannot exceed the 3 labels", too_many.json["error"])
        self.assertEqual(expired.status_code, 400)
        stage.assert_not_called()

    def test_record_within_label_count_reaches_planner(self):
        token = _label_token(3)
        event = dict(event_id="batch-3", po_number="PO001", part_code="PART1",
                     quantity=3, work_date="2026-11-04", action="produced",
                     status="pending", planner_response=None, planner_plan_id=None,
                     so_number="", label_token=token)
        with patch.object(app, "_planner_sync_enabled", return_value=True), \
             patch.object(app, "stage_production_event", return_value=event) as stage, \
             patch.object(app, "sync_production_event", return_value={"ok": True}) as sync, \
             patch.object(app, "complete_production_event", return_value={"quantity": 3}) as complete:
            response = self.client.post("/api/print-queue", json=event)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(stage.call_args.args[5], 3)
        self.assertEqual(sync.call_args.args[0]["quantity"], 3)
        complete.assert_called_once_with("batch-3", {"ok": True})


if __name__ == "__main__":
    unittest.main()
