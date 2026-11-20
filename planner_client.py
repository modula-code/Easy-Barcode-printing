import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class PlannerSyncError(RuntimeError):
    def __init__(
        self, message: str, status_code: int = 502, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        # Seconds to wait before calling Planner again; None = not a backoff case.
        self.retry_after = retry_after


# Process-wide backoff: every caller (prints, retries, the outbox, PO checks)
# shares one pause, so a throttled Planner gets no traffic until it is over.
# Doubles per consecutive failure, honours Retry-After, resets on success.
# ponytail: module globals, fine for gunicorn's single worker (gunicorn.conf.py);
# move to the DB if workers > 1. Racy += between threads only loses a strike.
_BACKOFF_BASE = 30.0
_BACKOFF_MAX = 1800.0
_strikes = 0
_paused_until = 0.0


def planner_cooldown() -> float:
    """Seconds left before Planner may be called again; 0 when clear."""
    return max(0.0, _paused_until - time.monotonic())


def _back_off(retry_after: float = 0.0) -> float:
    global _strikes, _paused_until
    _strikes += 1
    wait = max(retry_after, min(_BACKOFF_BASE * 2 ** min(_strikes - 1, 10), _BACKOFF_MAX))
    _paused_until = time.monotonic() + wait
    return wait


def _retry_after_header(headers) -> float:
    # ponytail: delta-seconds only; an HTTP-date falls back to our own backoff.
    try:
        return max(0.0, float(headers.get("Retry-After") or 0))
    except (AttributeError, ValueError):
        return 0.0


def _planner_request(method: str, path: str, payload: dict | None = None) -> dict:
    global _strikes
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
    wait = planner_cooldown()
    if wait:
        raise PlannerSyncError(
            "Backing off: Planner is rate limiting or unreachable. Sync resumes automatically.", 429, wait
        )
    try:
        with urlopen(
            request,
            timeout=float(os.getenv("PLANNER_SYNC_TIMEOUT", "15")),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            message = data.get("message") or data.get("error")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            message = None
        retry_after = None
        if exc.code == 429 or exc.code >= 500:
            retry_after = _back_off(_retry_after_header(exc.headers))
        raise PlannerSyncError(
            message or f"Planner rejected the request ({exc.code}).",
            exc.code,
            retry_after,
        ) from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise PlannerSyncError(
            f"Could not reach Planner: {exc}", retry_after=_back_off()
        ) from exc
    _strikes = 0
    return data


def sync_production_event(payload: dict) -> dict:
    return _planner_request("POST", "/tracking/barcode-events", payload)


def planner_po_status(po_number: str) -> dict:
    """{known, open, plans[]} for a PO, from GET /tracking/barcode-po."""
    return _planner_request(
        "GET", f"/tracking/barcode-po?poNumber={quote(po_number, safe='')}"
    )
