import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class PlannerSyncError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _planner_request(method: str, path: str, payload: dict | None = None) -> dict:
    base_url = os.getenv("PLANNER_API_URL", "").strip().rstrip("/")
    token = os.getenv("BARCODE_SYNC_TOKEN", "").strip()
    if not base_url or not token:
        raise PlannerSyncError(
            "Planner sync is not configured. Set PLANNER_API_URL and BARCODE_SYNC_TOKEN.",
            503,
        )

    request = Request(
        f"{base_url}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Barcode-Sync-Token": token,
        },
        method=method,
    )
    try:
        with urlopen(
            request,
            timeout=float(os.getenv("PLANNER_SYNC_TIMEOUT", "15")),
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            message = data.get("message") or data.get("error")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        raise PlannerSyncError(message or f"Planner rejected the request ({exc.code}).", exc.code) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise PlannerSyncError(f"Could not reach Planner: {exc}") from exc


def sync_production_event(payload: dict) -> dict:
    return _planner_request("POST", "/tracking/barcode-events", payload)


def planner_po_status(po_number: str) -> dict:
    """{known, open, plans[]} for a PO, from GET /tracking/barcode-po."""
    return _planner_request(
        "GET", f"/tracking/barcode-po?poNumber={quote(po_number, safe='')}"
    )
