from contextlib import closing
from datetime import date, datetime, timezone
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


def add_printed_part(
    part_code: str,
    quantity: int = 1,
    po_number: str = "",
) -> dict:
    code, qty = _part_qty(part_code, quantity)
    po = _po_number(po_number)
    work_date = current_work_date()
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
        if not row or row["status"] == "pushed":
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
            WHERE id = ? AND work_date = ? AND status != 'pushed'
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
            WHERE work_date = ? AND status IN ('queued', 'error')
            ORDER BY id DESC
            """,
            (selected_date,),
        ).fetchall()
    return {
        "date": selected_date,
        "today": current_work_date(),
        "items": [dict(row) for row in rows],
    }


def list_history_dates() -> list[str]:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT DISTINCT work_date FROM printed_parts
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
