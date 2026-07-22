import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PlannerSyncError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def sync_production_event(payload: dict) -> dict:
    base_url = os.getenv("PLANNER_API_URL", "").strip().rstrip("/")
    token = os.getenv("BARCODE_SYNC_TOKEN", "").strip()
    if not base_url or not token:
        raise PlannerSyncError(
            "Planner sync is not configured. Set PLANNER_API_URL and BARCODE_SYNC_TOKEN.",
            503,
        )

    request = Request(
        f"{base_url}/tracking/barcode-events",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Barcode-Sync-Token": token,
        },
        method="POST",
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
        raise PlannerSyncError(message or f"Planner rejected the event ({exc.code}).", exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise PlannerSyncError(f"Could not reach Planner: {exc}") from exc
