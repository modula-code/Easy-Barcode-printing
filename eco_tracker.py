"""
Drawing/ECO revision check.

Engineering keeps a Google Sheet ("Drawing_ECO Tracker", tab `Drawing`) listing every
drawing release: item number, release date, revision number and drawing code. When a
drawing is revised after a PO was approved, the floor must not print that PO's label
without pulling the new drawing first.

Sheet layout (header on row 2, data from row 3):
    A Item Number   B Drawing/DXF status   C Date   D Change Description
    E PDF/DXF       F REVISION NO          G BOM Update Required
    H DRW CODE/NAME I STATUS

Only `STATUS == APPROVED` rows are considered. An item carries a full revision history
(up to 11 rows) and the revision strings are NOT monotonic - the same item runs R12 then
R10 because those belong to different drawing families - so the latest row is picked by
DATE, never by revision string.
"""

import base64
import binascii
import datetime
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

SHEET_RANGE_DEFAULT = "Drawing!A:I"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Column positions within the fetched range, 0-based.
COL_ITEM = 0
COL_DATE = 2
COL_DESCRIPTION = 3
COL_REVISION = 5
COL_DRAWING = 7
COL_STATUS = 8

_EPOCH = datetime.date(1899, 12, 30)  # Sheets/Excel serial date origin
_SERIAL_MIN = 40000  # 2009-07-06; anything older is a typo, not a drawing date
_SERIAL_MAX = 60000  # 2064-04-08; guards against the year-20000 serials in the sheet

_REVISION_SUFFIX = re.compile(r"[-_]R\d+$")
_PANEL_TYPO = re.compile(r"^PO(\d{3}-[A-Z]{2})$")

_index_lock = threading.Lock()
_index_cache: tuple[float, dict[str, dict]] | None = None  # (expires_at, index)


class EcoTrackerError(RuntimeError):
    pass


def is_enabled() -> bool:
    return os.getenv("ECO_REVISION_CHECK_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def normalize_item_code(value) -> str:
    """
    Fold a sheet item number or a scanned/Odoo code to a common key.

    The sheet lists the same SM part as both 'SM-C303' and 'SM-C303-R02', and five
    panel codes are typed with a letter O ('PO234-AA' for 'P0234-AA').
    """
    code = str(value or "").strip().upper()
    if not code:
        return ""
    code = _REVISION_SUFFIX.sub("", code)
    # ponytail: guarded by the P####-XX shape, so a real 'PO...' code cannot be hit.
    return _PANEL_TYPO.sub(r"P0\1", code)


def _parse_release_date(value) -> datetime.date | None:
    """Serial number (UNFORMATTED_VALUE) or an ISO/`DD-MM-YYYY` string; None if unusable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        if not _SERIAL_MIN < serial < _SERIAL_MAX:
            return None
        return _EPOCH + datetime.timedelta(days=int(serial))

    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _parse_release_date(float(text))
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_po_date(value) -> datetime.date | None:
    """Odoo returns 'YYYY-MM-DD HH:MM:SS' or False."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    if not text or text.lower() == "false":
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return None


def build_index(rows) -> dict[str, dict]:
    """
    Latest APPROVED release per normalized item code.

    Rows are the raw sheet values including the two header rows; anything without an
    item code, an APPROVED status or a parseable date is skipped.
    """
    index: dict[str, dict] = {}
    for row in rows:
        def cell(position):
            return row[position] if len(row) > position else ""

        if str(cell(COL_STATUS)).strip().upper() != "APPROVED":
            continue
        code = normalize_item_code(cell(COL_ITEM))
        if not code:
            continue
        released_on = _parse_release_date(cell(COL_DATE))
        if released_on is None:
            continue

        # 10 codes carry two revisions on the same date; sheet order is the only
        # signal left there, so the row entered later wins.
        current = index.get(code)
        if current is not None and current["released_on"] > released_on:
            continue
        index[code] = {
            "code": code,
            "item_number": str(cell(COL_ITEM)).strip(),
            "released_on": released_on,
            "revision": str(cell(COL_REVISION)).strip(),
            "drawing_code": str(cell(COL_DRAWING)).strip(),
            "description": str(cell(COL_DESCRIPTION)).strip(),
        }
    return index


def sheet_id_from(value) -> str:
    """Accept a bare sheet id or a pasted /spreadsheets/d/<id>/edit URL."""
    text = str(value or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else text


def _credentials(scopes):
    """
    GOOGLE_SERVICE_ACCOUNT_JSON is a path, the raw JSON, or base64 of the JSON.

    Inline forms exist because container deployments carry env vars far more easily
    than mounted key files.
    """
    from google.oauth2.service_account import Credentials

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise EcoTrackerError(
            "Revision check is not configured. Set GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    if raw.startswith("{"):
        info = raw
    elif os.path.isfile(raw):
        return Credentials.from_service_account_file(raw, scopes=scopes)
    else:
        try:
            info = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise EcoTrackerError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not a readable file, inline JSON, "
                "or base64-encoded JSON."
            ) from exc

    try:
        return Credentials.from_service_account_info(json.loads(info), scopes=scopes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise EcoTrackerError(f"Service account credentials are invalid: {exc}") from exc


def _fetch_rows() -> list[list]:
    sheet_id = sheet_id_from(os.getenv("ECO_SHEET_ID"))
    if not sheet_id:
        raise EcoTrackerError("Revision check is not configured. Set ECO_SHEET_ID.")

    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - deployment problem, not logic
        raise EcoTrackerError(
            "Google Sheets libraries are missing. Install google-auth and "
            "google-api-python-client."
        ) from exc

    try:
        credentials = _credentials(SCOPES)
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=sheet_id,
                range=os.getenv("ECO_SHEET_RANGE", SHEET_RANGE_DEFAULT),
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="SERIAL_NUMBER",
            )
            .execute()
        )
    except EcoTrackerError:
        raise
    except Exception as exc:
        raise EcoTrackerError(f"Could not read the ECO tracker sheet: {exc}") from exc

    return response.get("values", [])


def get_index(*, force_refresh: bool = False) -> dict[str, dict]:
    """Cached revision index. ~1.9k APPROVED rows collapse to one dict, so lookups are free."""
    global _index_cache

    ttl = float(os.getenv("ECO_SHEET_CACHE_TTL", "600"))
    now = time.monotonic()
    with _index_lock:
        if not force_refresh and _index_cache and _index_cache[0] > now:
            return _index_cache[1]

    index = build_index(_fetch_rows())
    logger.info("ECO tracker index built: %d items", len(index))
    with _index_lock:
        _index_cache = (now + ttl, index) if ttl > 0 else None
    return index


def check_codes(codes, po_date) -> list[dict]:
    """
    Codes whose drawing was released after the PO was approved.

    `codes` are panel part codes and/or scanned SM codes; `po_date` is the PO's
    date_approve. Returns [] when disabled or when the PO date is unknown - without a
    PO date there is nothing to compare and every item would look revised.
    """
    if not is_enabled():
        return []
    po_day = parse_po_date(po_date)
    if po_day is None:
        return []

    index = get_index()
    alerts = []
    for code in dict.fromkeys(codes):
        entry = index.get(normalize_item_code(code))
        if entry is None or entry["released_on"] <= po_day:
            continue
        alerts.append(
            {
                "code": str(code).strip().upper(),
                "item_number": entry["item_number"],
                "revision": entry["revision"],
                "drawing_code": entry["drawing_code"],
                "description": entry["description"],
                "released_on": entry["released_on"].isoformat(),
                "po_date": po_day.isoformat(),
            }
        )
    return alerts
