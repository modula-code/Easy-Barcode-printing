"""Planner sync checks. Needs a reachable Postgres; runs inside its
own schema and drops it afterwards.

    python -m unittest test_sync
"""
import os
import unittest
import uuid
from unittest.mock import patch

SCHEMA = f"barcode_test_{uuid.uuid4().hex[:8]}"
BASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql://modula:modula@localhost:5432/modula_planner"
)
os.environ["DATABASE_URL"] = (
    f"{BASE_URL}{'&' if '?' in BASE_URL else '?'}options=-csearch_path%3D{SCHEMA}"
)
os.environ["PLANNER_RETRY_INTERVAL"] = "0"  # the test drives the outbox itself

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

import app  # noqa: E402
import queue_store  # noqa: E402
from planner_client import PlannerSyncError  # noqa: E402

try:
    with psycopg.connect(BASE_URL, autocommit=True) as _connection:
        _connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCHEMA)))
    DATABASE_READY = True
except psycopg.Error:
    DATABASE_READY = False


def tearDownModule():
    if DATABASE_READY:
        with psycopg.connect(BASE_URL, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(SCHEMA))
            )


def _planner_ok(payload):
    return {
        "planId": "plan-1",
        "planLabel": "W1-A1",
        "soNumber": "SO1",
        "allocations": [{"soNumber": "SO1", "quantity": payload["quantity"]}],
    }


class RequestGuardTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_cross_site_write_is_refused(self):
        response = self.client.delete(
            "/api/print-queue", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403)

    def test_planner_po_check_never_guesses(self):
        url = "/api/planner-po?po_number=PO1"
        with patch.object(app, "_planner_sync_enabled", return_value=False):
            self.assertEqual(self.client.get(url).json, {"checked": False})
        with patch.object(app, "_planner_sync_enabled", return_value=True):
            with patch.object(
                app, "planner_po_status", side_effect=PlannerSyncError("down")
            ):
                self.assertEqual(self.client.get(url).json, {"checked": False})
            with patch.object(
                app,
                "planner_po_status",
                return_value={"known": False, "open": False, "plans": []},
            ):
                self.assertEqual(
                    self.client.get(url).json,
                    {"checked": True, "known": False, "open": False, "plans": []},
                )


@unittest.skipUnless(DATABASE_READY, "Postgres is not reachable")
class PlannerSyncTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.today = queue_store.current_work_date()
        enabled = patch.object(app, "_planner_sync_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def _print(self, quantity, error):
        """Record a print while Planner answers with `error`."""
        event_id = f"evt-{uuid.uuid4().hex}"
        with patch.object(app, "sync_production_event", side_effect=error):
            response = self.client.post(
                "/api/print-queue",
                json={
                    "event_id": event_id,
                    "po_number": f"PO-{uuid.uuid4().hex[:6]}",
                    "part_code": "PART1",
                    "quantity": quantity,
                    "work_date": self.today,
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["sync_status"], "error")
        return event_id, response.json["item"]["id"]

    def _make_due(self, event_id):
        with queue_store._connect() as connection:
            connection.execute(
                "UPDATE production_events SET updated_at = '2000-01-01T00:00:00+00:00' "
                "WHERE event_id = %s",
                (event_id,),
            )

    def _rows(self, po_number):
        return [
            row
            for row in queue_store.list_printed_parts(self.today)["items"]
            if row["po_number"] == po_number
        ]

    def test_refused_print_is_retried_until_planner_accepts(self):
        event_id, row_id = self._print(3, PlannerSyncError("No matching plan.", 404))
        po = queue_store.get_printed_part(row_id)["po_number"]
        self.assertNotIn(
            event_id, [e["event_id"] for e in queue_store.list_due_production_events()]
        )  # backoff not up yet
        self._make_due(event_id)
        with patch.object(app, "sync_production_event", side_effect=_planner_ok) as sync:
            app._retry_due_events()
            app._retry_due_events()  # nothing left to send
        self.assertEqual(
            [c.args[0]["eventId"] for c in sync.call_args_list].count(event_id), 1
        )
        self.assertEqual(queue_store.get_production_event(event_id)["status"], "synced")
        rows = self._rows(po)
        self.assertEqual([(r["status"], r["quantity"]) for r in rows], [("synced", 3)])

    def test_planner_takes_part_of_a_print_and_the_rest_stays_local(self):
        event_id = f"evt-{uuid.uuid4().hex}"
        po = f"PO-{uuid.uuid4().hex[:6]}"
        partial = {
            "planId": "plan-1",
            "allocations": [{"soNumber": "SO1", "quantity": 3}],
            "overflow": 2,
        }
        with patch.object(app, "sync_production_event", return_value=partial):
            response = self.client.post(
                "/api/print-queue",
                json={
                    "event_id": event_id,
                    "po_number": po,
                    "part_code": "PART1",
                    "quantity": 5,
                    "work_date": self.today,
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            sorted((r["status"], r["quantity"]) for r in self._rows(po)),
            [("queued", 2), ("synced", 3)],
        )

    def test_local_rejection_shrinks_the_waiting_event(self):
        event_id, row_id = self._print(3, PlannerSyncError("No matching plan.", 404))
        response = self.client.post(
            f"/api/print-queue/{row_id}/reject",
            json={
                "event_id": f"qc-{uuid.uuid4().hex}",
                "problem_description": "Scratch",
                "disposition": "Scrap",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(queue_store.get_production_event(event_id)["quantity"], 2)
        self._make_due(event_id)
        with patch.object(app, "sync_production_event", side_effect=_planner_ok) as sync:
            app._retry_due_events()
        sent = [c.args[0] for c in sync.call_args_list if c.args[0]["eventId"] == event_id]
        self.assertEqual(sent[0]["quantity"], 2)  # not the original 3

    def test_deleting_a_refused_print_cancels_its_event(self):
        event_id, row_id = self._print(2, PlannerSyncError("No matching plan.", 404))
        self.assertEqual(self.client.delete(f"/api/print-queue/{row_id}").status_code, 200)
        self.assertEqual(
            queue_store.get_production_event(event_id)["status"], "cancelled"
        )
        self._make_due(event_id)
        self.assertNotIn(
            event_id, [e["event_id"] for e in queue_store.list_due_production_events()]
        )

    def test_unconfirmed_print_cannot_be_deleted(self):
        # A timeout leaves it unknown whether Planner counted the print.
        event_id, row_id = self._print(2, PlannerSyncError("Could not reach Planner."))
        response = self.client.delete(f"/api/print-queue/{row_id}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not confirmed", response.json["error"])
        self.assertEqual(queue_store.get_production_event(event_id)["status"], "error")

    def test_fulfilled_on_retry_keeps_one_local_copy(self):
        event_id, row_id = self._print(4, PlannerSyncError("Planner timed out.", 504))
        self._make_due(event_id)
        fulfilled = PlannerSyncError("Only 0 PART1 remain for PO X.", 409)
        with patch.object(app, "sync_production_event", side_effect=fulfilled):
            app._retry_due_events()
        self.assertEqual(queue_store.get_production_event(event_id)["status"], "skipped")
        row = queue_store.get_printed_part(row_id)
        self.assertEqual((row["status"], row["quantity"]), ("queued", 4))


if __name__ == "__main__":
    unittest.main()


class PlannerBackoffTest(unittest.TestCase):
    """No database needed: drives planner_client against a fake urlopen."""

    def setUp(self):
        import planner_client

        self.pc = planner_client
        env = patch.dict(
            os.environ, {"PLANNER_API_URL": "http://planner", "BARCODE_SYNC_TOKEN": "t"}
        )
        env.start()
        self.addCleanup(env.stop)
        self.pc._strikes, self.pc._paused_until = 0, 0.0
        self.addCleanup(setattr, self.pc, "_paused_until", 0.0)
        self.addCleanup(setattr, self.pc, "_strikes", 0)

    def _throttled(self, retry_after=None):
        import io as _io
        from email.message import Message
        from urllib.error import HTTPError

        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return HTTPError("http://planner", 429, "Too Many", headers, _io.BytesIO(b"{}"))

    def test_throttle_pauses_every_caller_and_doubles(self):
        with patch.object(self.pc, "urlopen", side_effect=self._throttled()) as call:
            with self.assertRaises(PlannerSyncError) as first:
                self.pc.sync_production_event({})
            self.assertEqual((first.exception.status_code, first.exception.retry_after), (429, 30))
            # Paused: the next caller fails fast and Planner sees nothing.
            with self.assertRaises(PlannerSyncError) as paused:
                self.pc.planner_po_status("PO1")
            self.assertEqual(paused.exception.status_code, 429)
            self.assertEqual(call.call_count, 1)
            self.pc._paused_until = 0.0  # pause over
            with self.assertRaises(PlannerSyncError) as second:
                self.pc.sync_production_event({})
            self.assertEqual(second.exception.retry_after, 60)  # doubled

    def test_retry_after_header_wins_when_longer(self):
        with patch.object(self.pc, "urlopen", side_effect=self._throttled(500)):
            with self.assertRaises(PlannerSyncError) as caught:
                self.pc.sync_production_event({})
        self.assertEqual(caught.exception.retry_after, 500)

    def test_outbox_skips_its_pass_while_paused(self):
        self.pc._paused_until = self.pc.time.monotonic() + 60
        with patch.object(app, "list_due_production_events") as due:
            app._retry_due_events()
        due.assert_not_called()
