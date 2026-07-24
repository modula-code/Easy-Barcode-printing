"""One-shot copy of the old SQLite queue into Postgres.

    DATABASE_URL=postgresql://... python migrate_sqlite_to_pg.py [printed_parts.sqlite3]

Idempotent: rows whose primary key is already in Postgres are left untouched,
so it is safe to re-run after a partial migration. IDs are preserved because
production_events.target_row_id points at printed_parts.id.
"""

import os
import sqlite3
import sys

from queue_store import _connect, _created_work_date

# column -> value used when the old SQLite file predates that column, or stored
# NULL in a column Postgres declares NOT NULL.
TABLES = {
    "printed_parts": {
        "key": "id",
        "order_by": "id",
        "columns": {
            "id": None,
            "po_number": "",
            "part_code": "",
            "quantity": 0,
            "status": "queued",
            "so_number": "",
            "planner_plan_id": "",
            "work_date": None,
            "created_at": "",
        },
    },
    "shift_plans": {
        "key": "id",
        "order_by": "id",
        "columns": {
            "id": None,
            "work_date": "",
            "po_number": "",
            "label_filename": "label.pdf",
            "label_pdf": b"",
            "created_at": "",
        },
    },
    "production_events": {
        "key": "event_id",
        "order_by": "rowid",
        "columns": {
            "event_id": "",
            "action": "produced",
            "po_number": "",
            "so_number": "",
            "part_code": "",
            "quantity": 0,
            "work_date": "",
            "target_row_id": None,
            "planner_plan_id": "",
            "status": "pending",
            "error": None,
            "planner_response": None,
            "created_at": "",
            "updated_at": "",
        },
    },
}


def _source_tables(source: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _copy(source: sqlite3.Connection, target, table: str, spec: dict) -> int:
    present = {row["name"] for row in source.execute(f"PRAGMA table_info({table})")}
    defaults = spec["columns"]
    selected = [name for name in defaults if name in present]
    rows = source.execute(
        f"SELECT {', '.join(selected)} FROM {table} ORDER BY {spec['order_by']}"
    ).fetchall()

    copied = 0
    names = list(defaults)
    placeholders = ", ".join(["%s"] * len(names))
    for row in rows:
        values = {
            name: (row[name] if name in present and row[name] is not None else fallback)
            for name, fallback in defaults.items()
        }
        if table == "printed_parts" and not values["work_date"]:
            values["work_date"] = _created_work_date(values["created_at"])
        cursor = target.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({spec['key']}) DO NOTHING",
            tuple(values[name] for name in names),
        )
        copied += cursor.rowcount
    return copied


def main(path: str) -> None:
    if not os.path.exists(path):
        sys.exit(f"SQLite file not found: {path}")

    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        with _connect() as target:
            available = _source_tables(source)
            for table, spec in TABLES.items():
                if table not in available:
                    print(f"{table}: not in source, skipped")
                    continue
                print(f"{table}: {_copy(source, target, table, spec)} rows copied")
            # Identity columns keep counting from 1 after explicit-ID inserts.
            for table, column in (
                ("printed_parts", "id"),
                ("shift_plans", "id"),
                ("production_events", "seq"),
            ):
                target.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'),"
                    f" COALESCE(MAX({column}), 0) + 1, false) FROM {table}"
                )
    finally:
        source.close()
    print("done")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "printed_parts.sqlite3")
