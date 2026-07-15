from contextlib import closing
from datetime import datetime, timezone
import os
import sqlite3
import threading


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_queue_lock = threading.Lock()


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


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS printed_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part_code TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL
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


def add_printed_part(part_code: str, quantity: int = 1) -> dict:
    code, qty = _part_qty(part_code, quantity)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        existing = connection.execute(
            """
            SELECT * FROM printed_parts
            WHERE part_code = ? AND status IN ('queued', 'error')
            ORDER BY id DESC LIMIT 1
            """,
            (code,),
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
                INSERT INTO printed_parts (part_code, quantity, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    code,
                    qty,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            row_id = cursor.lastrowid
        row = connection.execute(
            "SELECT * FROM printed_parts WHERE id = ?",
            (row_id,),
        ).fetchone()
    return dict(row)


def update_printed_part(row_id: int, part_code: str, quantity: int) -> dict:
    code, qty = _part_qty(part_code, quantity)
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT status FROM printed_parts WHERE id = ?",
            (row_id,),
        ).fetchone()
        if not row or row["status"] == "pushed":
            raise ValueError("Queue row was not found.")
        connection.execute(
            """
            UPDATE printed_parts
            SET part_code = ?, quantity = ?, status = 'queued'
            WHERE id = ?
            """,
            (code, qty, row_id),
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
            "DELETE FROM printed_parts WHERE id = ? AND status != 'pushed'",
            (row_id,),
        )
        if not cursor.rowcount:
            raise ValueError("Queue row was not found.")


def list_printed_parts() -> dict:
    with _queue_lock, closing(_connect()) as connection, connection:
        _ensure_schema(connection)
        rows = connection.execute(
            """
            SELECT * FROM printed_parts
            WHERE status IN ('queued', 'error')
            ORDER BY id DESC
            """
        ).fetchall()
    return {"items": [dict(row) for row in rows]}
