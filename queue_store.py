from contextlib import closing
from datetime import date, datetime, timezone
import json
import os
import sqlite3
import threading
from zoneinfo import ZoneInfo


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
_queue_lock = threading.Lock()


def current_work_date() -> str:
    return datetime.now(APP_TIMEZONE).date().isoformat()


def _work_date(value: str | None = None) -> str:
    candidate = str(value or current_work_date()).strip()
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ValueError("work_date must use YYYY-MM-DD.") from exc


def _connect() -> sqlite3.Connection:
    path = os.getenv(
        "PRINT_QUEUE_DB_PATH",
        os.path.join(BASE_DIR, "printed_parts.sqlite3"),
    )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _created_work_date(created_at: str) -> str:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created.astimezone(APP_TIMEZONE).date().isoformat()
    except (TypeError, ValueError):
        return current_work_date()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS printed_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number TEXT NOT NULL DEFAULT '',
            part_code TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            so_number TEXT NOT NULL DEFAULT '',
            planner_plan_id TEXT NOT NULL DEFAULT '',
            work_date TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(printed_parts)")
    }
    if "po_number" not in columns:
        connection.execute(
            "ALTER TABLE printed_parts ADD COLUMN po_number TEXT NOT NULL DEFAULT ''"
        )
    if "work_date" not in columns:
        connection.execute("ALTER TABLE printed_parts ADD COLUMN work_date TEXT")
    if "so_number" not in columns:
        connection.execute(
            "ALTER TABLE printed_parts ADD COLUMN so_number TEXT NOT NULL DEFAULT ''"
        )
    if "planner_plan_id" not in columns:
        connection.execute(
            "ALTER TABLE printed_parts ADD COLUMN planner_plan_id TEXT NOT NULL DEFAULT ''"
        )
    for row in connection.execute(
        "SELECT id, created_at FROM printed_parts WHERE work_date IS NULL OR work_date = ''"
    ):
        connection.execute(
            "UPDATE printed_parts SET work_date = ? WHERE id = ?",
            (_created_work_date(row["created_at"]), row["id"]),
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shift_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_date TEXT NOT NULL,
            po_number TEXT NOT NULL,
            label_filename TEXT NOT NULL,
            label_pdf BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(work_date, po_number)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS production_events (
            event_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            po_number TEXT NOT NULL,
            so_number TEXT NOT NULL,
            part_code TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            target_row_id INTEGER,
            planner_plan_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            planner_response TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _part_qty(part_code: str, quantity: int) -> tuple[str, int]:
    code = str(part_code or "").strip().upper()
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 0
    if not code:
        raise ValueError("part_code is required.")
    if qty < 1 or qty > 500:
        raise ValueError("quantity must be between 1 and 500.")
    return code, qty


def _po_number(value: str) -> str:
    po_number = str(value or "").strip().upper()
    if not po_number:
        raise ValueError("po_number is required.")
    return po_number


def _so_number(value: str) -> str:
    so_number = str(value or "").strip().upper()
    if not so_number:
        raise ValueError("so_number is required.")
    return so_number


def get_printed_part(row_id: int) -> dict:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT * FROM printed_parts WHERE id = ? AND work_date = ?",
            (row_id, current_work_date()),
        ).fetchone()
    if not row:
        raise ValueError("Queue row was not found.")
    return dict(row)


def stage_production_event(
    event_id: str,
    action: str,
    po_number: str,
    so_number: str,
    part_code: str,
    quantity: int,
    work_date: str,
    *,
    planner_plan_id: str = "",
    target_row_id: int | None = None,
) -> dict:
    event = str(event_id or "").strip()
    if len(event) < 8 or len(event) > 200:
        raise ValueError("event_id must be between 8 and 200 characters.")
    if action not in {"produced", "rejected"}:
        raise ValueError("action must be produced or rejected.")
    code, qty = _part_qty(part_code, quantity)
    po = _po_number(po_number)
    so = _so_number(so_number) if action == "rejected" else str(so_number or "").strip().upper()
    if not str(work_date or "").strip():
        raise ValueError("work_date is required.")
    work_date = _work_date(work_date)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        existing = connection.execute(
            "SELECT * FROM production_events WHERE event_id = ?",
            (event,),
        ).fetchone()
        if existing:
            expected = (action, po, so, code, qty, work_date, target_row_id)
            actual = (
                existing["action"],
                existing["po_number"],
                existing["so_number"],
                existing["part_code"],
                existing["quantity"],
                existing["work_date"],
                existing["target_row_id"] if action == "rejected" else None,
            )
            if actual != expected:
                raise ValueError("event_id was already used with different production data.")
            return dict(existing)
        if action == "rejected":
            row = connection.execute(
                "SELECT * FROM printed_parts WHERE id = ? AND work_date = ?",
                (target_row_id, work_date),
            ).fetchone()
            if not row or row["status"] != "synced" or int(row["quantity"]) < qty:
                raise ValueError("Rejected quantity exceeds the synced good quantity.")
        connection.execute(
            """
            INSERT INTO production_events
                (event_id, action, po_number, so_number, part_code, quantity,
                 work_date, target_row_id, planner_plan_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event,
                action,
                po,
                so,
                code,
                qty,
                work_date,
                target_row_id,
                str(planner_plan_id or "").strip(),
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM production_events WHERE event_id = ?",
            (event,),
        ).fetchone()
    return dict(row)


def fail_production_event(event_id: str, error: str) -> None:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        connection.execute(
            """
            UPDATE production_events
            SET status = 'error', error = ?, updated_at = ?
            WHERE event_id = ? AND status != 'synced'
            """,
            (
                str(error or "Planner sync failed.")[:1000],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                event_id,
            ),
        )


def complete_production_event(event_id: str, planner_response: dict) -> dict | None:
    plan_id = str(planner_response.get("planId") or "").strip()
    if not plan_id:
        raise ValueError("Planner response did not include planId.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        event = connection.execute(
            "SELECT * FROM production_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if not event:
            raise ValueError("Production event was not found.")
        if event["status"] == "synced":
            row = connection.execute(
                "SELECT * FROM printed_parts WHERE id = ?",
                (event["target_row_id"],),
            ).fetchone()
            return dict(row) if row else None

        if event["action"] == "produced":
            allocations = planner_response.get("allocations") or [
                {
                    "soNumber": planner_response.get("soNumber"),
                    "quantity": event["quantity"],
                }
            ]
            normalized_allocations = [
                (_so_number(item.get("soNumber")), int(item.get("quantity") or 0))
                for item in allocations
            ]
            if (
                any(quantity <= 0 for _, quantity in normalized_allocations)
                or sum(quantity for _, quantity in normalized_allocations)
                != int(event["quantity"])
            ):
                raise ValueError("Planner response included invalid SO allocations.")
            row_ids = []
            for so_number, quantity in normalized_allocations:
                row = connection.execute(
                    """
                    SELECT * FROM printed_parts
                    WHERE work_date = ? AND po_number = ? AND so_number = ?
                      AND part_code = ? AND planner_plan_id = ? AND status = 'synced'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        event["work_date"],
                        event["po_number"],
                        so_number,
                        event["part_code"],
                        plan_id,
                    ),
                ).fetchone()
                if row:
                    connection.execute(
                        "UPDATE printed_parts SET quantity = quantity + ? WHERE id = ?",
                        (quantity, row["id"]),
                    )
                    row_ids.append(row["id"])
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO printed_parts
                            (po_number, so_number, part_code, quantity, status,
                             planner_plan_id, work_date, created_at)
                        VALUES (?, ?, ?, ?, 'synced', ?, ?, ?)
                        """,
                        (
                            event["po_number"],
                            so_number,
                            event["part_code"],
                            quantity,
                            plan_id,
                            event["work_date"],
                            now,
                        ),
                    )
                    row_ids.append(cursor.lastrowid)
            row_id = row_ids[0]
            connection.execute(
                "UPDATE production_events SET target_row_id = ? WHERE event_id = ?",
                (row_id, event_id),
            )
        else:
            row_id = event["target_row_id"]
            row = connection.execute(
                "SELECT quantity FROM printed_parts WHERE id = ?",
                (row_id,),
            ).fetchone()
            if not row or int(row["quantity"]) < int(event["quantity"]):
                raise ValueError("Rejected quantity exceeds the synced good quantity.")
            connection.execute(
                "UPDATE printed_parts SET quantity = quantity - ? WHERE id = ?",
                (event["quantity"], row_id),
            )

        connection.execute(
            """
            UPDATE production_events
            SET planner_plan_id = ?, status = 'synced', error = NULL,
                planner_response = ?, updated_at = ?
            WHERE event_id = ?
            """,
            (plan_id, json.dumps(planner_response), now, event_id),
        )
        row = connection.execute(
            "SELECT * FROM printed_parts WHERE id = ? AND quantity > 0",
            (row_id,),
        ).fetchone()
    return dict(row) if row else None


def add_printed_part(
    part_code: str,
    quantity: int = 1,
    po_number: str = "",
    work_date: str | None = None,
) -> dict:
    code, qty = _part_qty(part_code, quantity)
    po = _po_number(po_number)
    work_date = _work_date(work_date)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        existing = connection.execute(
            """
            SELECT * FROM printed_parts
            WHERE work_date = ? AND po_number = ? AND part_code = ?
              AND status IN ('queued', 'error')
            ORDER BY id DESC LIMIT 1
            """,
            (work_date, po, code),
        ).fetchone()
        if existing:
            new_quantity = int(existing["quantity"]) + qty
            if new_quantity > 500:
                raise ValueError("quantity must be between 1 and 500.")
            connection.execute(
                "UPDATE printed_parts SET quantity = ?, status = 'queued' WHERE id = ?",
                (new_quantity, existing["id"]),
            )
            row_id = existing["id"]
        else:
            cursor = connection.execute(
                """
                INSERT INTO printed_parts
                    (po_number, part_code, quantity, work_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    po,
                    code,
                    qty,
                    work_date,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            row_id = cursor.lastrowid
        row = connection.execute(
            "SELECT * FROM printed_parts WHERE id = ?",
            (row_id,),
        ).fetchone()
    return dict(row)


def update_printed_part(
    row_id: int,
    po_number: str,
    part_code: str,
    quantity: int,
) -> dict:
    code, qty = _part_qty(part_code, quantity)
    po = _po_number(po_number)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT status, work_date FROM printed_parts WHERE id = ?",
            (row_id,),
        ).fetchone()
        if not row or row["status"] in {"pushed", "synced"}:
            raise ValueError("Queue row was not found.")
        if row["work_date"] != current_work_date():
            raise ValueError("Archived queue rows cannot be edited.")
        connection.execute(
            """
            UPDATE printed_parts
            SET po_number = ?, part_code = ?, quantity = ?, status = 'queued'
            WHERE id = ?
            """,
            (po, code, qty, row_id),
        )
        updated = connection.execute(
            "SELECT * FROM printed_parts WHERE id = ?",
            (row_id,),
        ).fetchone()
    return dict(updated)


def delete_printed_part(row_id: int) -> None:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            """
            DELETE FROM printed_parts
            WHERE id = ? AND work_date = ? AND status NOT IN ('pushed', 'synced')
            """,
            (row_id, current_work_date()),
        )
        if not cursor.rowcount:
            raise ValueError("Queue row was not found or is archived.")


def clear_printed_parts() -> int:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            """
            DELETE FROM printed_parts
            WHERE work_date = ? AND status IN ('queued', 'error')
            """,
            (current_work_date(),),
        )
        return cursor.rowcount


def list_printed_parts(work_date: str | None = None) -> dict:
    selected_date = _work_date(work_date)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT * FROM printed_parts
            WHERE work_date = ? AND status IN ('queued', 'error', 'synced')
              AND quantity > 0
            ORDER BY id DESC
            """,
            (selected_date,),
        ).fetchall()
    return {
        "date": selected_date,
        "today": current_work_date(),
        "items": [dict(row) for row in rows],
    }


def get_production_event(event_id: str) -> dict:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT * FROM production_events WHERE event_id = ?",
            (str(event_id or "").strip(),),
        ).fetchone()
    if not row:
        raise ValueError("Production event was not found.")
    return dict(row)


def list_production_events(work_date: str | None = None) -> dict:
    """Planner sync ledger: what was sent, what Planner answered, what failed.

    'synced' means Planner accepted it and told us which plan/SO/day it landed
    on; anything else never reached the plan and can be retried."""
    selected_date = _work_date(work_date)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT * FROM production_events
            WHERE work_date = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (selected_date,),
        ).fetchall()

    items = []
    for row in rows:
        try:
            response = json.loads(row["planner_response"] or "{}")
        except json.JSONDecodeError:
            response = {}
        items.append(
            {
                "event_id": row["event_id"],
                "action": row["action"],
                "po_number": row["po_number"],
                "so_number": row["so_number"] or response.get("soNumber") or "",
                "part_code": row["part_code"],
                "quantity": row["quantity"],
                "work_date": row["work_date"],
                "status": row["status"],
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "plan_id": row["planner_plan_id"] or response.get("planId") or "",
                "plan_label": response.get("planLabel") or "",
                "production_day": response.get("productionDay"),
                "panel_total": response.get("panelTotal"),
                "allocations": response.get("allocations") or [],
                "associated": response.get("associated") or [],
            }
        )
    return {
        "date": selected_date,
        "today": current_work_date(),
        "items": items,
        "pending": sum(1 for item in items if item["status"] != "synced"),
    }


def list_history_dates() -> list[str]:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT work_date FROM printed_parts
            UNION
            SELECT work_date FROM production_events
            ORDER BY work_date DESC
            """
        ).fetchall()
    return [row["work_date"] for row in rows if row["work_date"]]


def save_shift_plan(po_number: str, label_filename: str, label_pdf: bytes) -> dict:
    po = _po_number(po_number)
    if not label_pdf.startswith(b"%PDF-"):
        raise ValueError("The label must be a valid PDF.")
    filename = str(label_filename or "label.pdf").strip() or "label.pdf"
    work_date = current_work_date()
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO shift_plans
                (work_date, po_number, label_filename, label_pdf, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(work_date, po_number) DO UPDATE SET
                label_filename = excluded.label_filename,
                label_pdf = excluded.label_pdf,
                created_at = excluded.created_at
            """,
            (
                work_date,
                po,
                filename,
                label_pdf,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        row = connection.execute(
            """
            SELECT id, work_date, po_number, label_filename, created_at
            FROM shift_plans WHERE work_date = ? AND po_number = ?
            """,
            (work_date, po),
        ).fetchone()
    return dict(row)


def list_shift_plans() -> list[dict]:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT id, work_date, po_number, label_filename, created_at
            FROM shift_plans WHERE work_date = ? ORDER BY id
            """,
            (current_work_date(),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_shift_plan(plan_id: int) -> dict:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute(
            """
            SELECT * FROM shift_plans WHERE id = ? AND work_date = ?
            """,
            (plan_id, current_work_date()),
        ).fetchone()
    if not row:
        raise ValueError("Today's PO label was not found.")
    return dict(row)
