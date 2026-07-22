import copy
import http.client
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import xmlrpc.client
from collections import Counter
from collections.abc import Callable
from http.cookiejar import CookieJar
from typing import Any

from dotenv import load_dotenv

load_dotenv()

Execute = Callable[[str, str, list[Any], dict[str, Any]], Any]

PANEL_LABEL_REPORT = os.getenv(
    "ODOO_PANEL_LABEL_REPORT", "stock.report_delivery_label_3x8"
)


class OdooConfigurationError(RuntimeError):
    pass


class OdooLookupError(RuntimeError):
    pass


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def _settings() -> tuple[str, str, str, str, float]:
    required = ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise OdooConfigurationError(
            f"Missing Odoo configuration: {', '.join(missing)}"
        )

    try:
        timeout = float(os.getenv("ODOO_TIMEOUT", "20"))
    except ValueError as exc:
        raise OdooConfigurationError("ODOO_TIMEOUT must be a number") from exc

    return (
        os.environ["ODOO_URL"].rstrip("/"),
        os.environ["ODOO_DB"],
        os.environ["ODOO_USERNAME"],
        os.environ["ODOO_PASSWORD"],
        timeout,
    )


logger = logging.getLogger(__name__)

_auth_lock = threading.Lock()
_uid: int | None = None
_local = threading.local()  # .models = per-thread /object proxy (ServerProxy is not thread-safe)
_RETRYABLE = (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError)

_CACHE_TTL = float(os.getenv("ODOO_CACHE_TTL", "300"))  # seconds; 0 disables
_CACHEABLE_MODELS = {
    "purchase.order",
    "product.product",
    "product.template",
    "mrp.bom",
    "mrp.bom.line",
    "product.template.attribute.value",
}
_MISS = object()
_cache: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)
_cache_lock = threading.Lock()


def _transport() -> xmlrpc.client.Transport:
    url, _, _, _, timeout = _settings()
    transport_class = (
        _TimeoutSafeTransport if url.lower().startswith("https://") else _TimeoutTransport
    )
    return transport_class(timeout)


def _get_uid() -> int:
    global _uid
    with _auth_lock:
        if _uid is None:
            url, db, username, password, _ = _settings()
            common = xmlrpc.client.ServerProxy(
                f"{url}/xmlrpc/2/common",
                transport=_transport(),
                allow_none=True,
            )
            uid = common.authenticate(db, username, password, {})
            if not uid:
                raise OdooConfigurationError(
                    "Odoo authentication failed. Check ODOO_DB, ODOO_USERNAME, and ODOO_PASSWORD."
                )
            _uid = uid
        return _uid


def _models_proxy() -> xmlrpc.client.ServerProxy:
    models = getattr(_local, "models", None)
    if models is None:
        url = _settings()[0]
        models = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/object",
            transport=_transport(),
            allow_none=True,
        )
        _local.models = models
    return models


def _reset_connection() -> None:
    global _uid
    _local.models = None
    with _auth_lock:
        _uid = None


def _rpc(model: str, method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    _, db, _, password, _ = _settings()
    uid = _get_uid()
    return _models_proxy().execute_kw(db, uid, password, model, method, args, kwargs)


def _cache_get(key: str) -> Any:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return copy.deepcopy(entry[1])
        _cache.pop(key, None)
        return _MISS


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        if len(_cache) > 512:  # ponytail: crude bound, LRU if key space ever grows
            _cache.clear()
        _cache[key] = (time.monotonic() + _CACHE_TTL, copy.deepcopy(value))


def _shared_execute(
    model: str,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> Any:
    key = None
    if method == "search_read" and model in _CACHEABLE_MODELS and _CACHE_TTL > 0:
        key = repr((model, args, kwargs))
        cached = _cache_get(key)
        if cached is not _MISS:
            return cached
    start = time.monotonic()
    try:
        result = _rpc(model, method, args, kwargs)
    except _RETRYABLE:
        _reset_connection()
        result = _rpc(model, method, args, kwargs)
    logger.debug(
        "odoo %s.%s took %.0f ms", model, method, (time.monotonic() - start) * 1000
    )
    if key is not None:
        _cache_set(key, result)
    return result


def _connect() -> tuple[Execute, int]:
    return _shared_execute, _get_uid()


def _search_read(
    execute: Execute,
    model: str,
    domain: list[Any],
    fields: list[str],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    options = {"fields": fields, **kwargs}
    return execute(model, "search_read", [domain], options)


def _many2one_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    return int(value) if isinstance(value, int) else None


def _many2one_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return ""


PO_FALLBACK_COMPANY_ID = int(os.getenv("ODOO_PO_COMPANY_ID", "1"))


def _po_search_terms(po_number: str) -> list[str]:
    value = po_number.strip().upper()
    terms = [value]
    if value and "/" not in value and not value.startswith("PO"):
        terms.append(f"PO{value}")
    return list(dict.fromkeys(terms))


def _find_purchase_order(
    execute: Execute, po_number: str, fields: list[str]
) -> dict[str, Any]:
    """Find a PO by full name, floor short code, or number only.

    Company 1 PO names look like '06/26-27/PO09862' but the floor only knows
    the trailing 'PO09862' or '09862', so fall back to a suffix search there.
    """
    terms = _po_search_terms(po_number)
    orders = _search_read(
        execute,
        "purchase.order",
        [("name", "in", terms)],
        fields,
        limit=10,
    )
    for term in terms:
        for order in orders:
            if order.get("name") == term:
                return order

    suffix_terms = terms[1:] + terms[:1] if len(terms) > 1 else terms
    domain: list[Any] = ["|"] * (len(suffix_terms) - 1)
    domain += [("name", "=ilike", f"%{term}") for term in suffix_terms]
    domain.append(("company_id", "=", PO_FALLBACK_COMPANY_ID))
    orders = _search_read(
        execute,
        "purchase.order",
        domain,
        fields,
        limit=10,
        order="id desc",
    )
    for term in suffix_terms:
        matches = [
            order
            for order in orders
            if str(order.get("name") or "").upper().endswith(term)
        ]
        if len(matches) > 1:
            names = ", ".join(str(order.get("name")) for order in matches)
            raise OdooLookupError(
                f"Purchase order '{po_number}' is ambiguous: {names}. "
                "Enter the full PO number."
            )
        if matches:
            return matches[0]

    raise OdooLookupError(f"Purchase order '{po_number}' was not found.")


def suggest_purchase_orders(
    po_number: str,
    *,
    execute: Execute | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    query = po_number.strip()
    if not query:
        return []

    if execute is None:
        execute, _ = _connect()

    terms = _po_search_terms(query)
    search_terms = terms[1:] + terms[:1] if len(terms) > 1 else terms
    name_domain: list[Any] = [("name", "ilike", search_terms[0])]
    if len(search_terms) > 1:
        name_domain = [
            "|",
            ("name", "ilike", search_terms[0]),
            ("name", "ilike", search_terms[1]),
        ]
    orders = _search_read(
        execute,
        "purchase.order",
        name_domain,
        ["id", "name", "partner_ref"],
        limit=limit,
        order="id desc",
    )
    return [
        {
            "po_number": order.get("name"),
            "purchase_order_id": order.get("id"),
            "partner_ref": order.get("partner_ref"),
        }
        for order in orders
        if order.get("name")
    ]


def get_partner_ref(
    po_number: str,
    *,
    execute: Execute | None = None,
) -> dict[str, Any]:
    """Look up purchase.order.partner_ref for a given purchase.order.name."""
    normalized_po = po_number.strip()
    if not normalized_po:
        raise ValueError("PO number is required.")

    if execute is None:
        execute, _ = _connect()

    order = _find_purchase_order(
        execute, normalized_po, ["id", "name", "partner_ref"]
    )
    return {
        "po_number": order.get("name") or normalized_po,
        "purchase_order_id": order["id"],
        "partner_ref": order.get("partner_ref"),
    }


def _download_report_pdf(
    picking_ids: list[int], company_ids: list[int] | None = None
) -> bytes:
    """Download the Panel Label report through Odoo's web session.

    Odoo 17 does not expose report rendering over XML-RPC, so this logs in via
    /web/session/authenticate (needs the real password, an API key is not
    enough) and downloads /report/pdf/<report>/<ids>.
    """
    url, db, username, password, timeout = _settings()

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )
    auth_request = urllib.request.Request(
        f"{url}/web/session/authenticate",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "params": {"db": db, "login": username, "password": password},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(auth_request, timeout=timeout) as response:
        session = json.load(response)
    if not (session.get("result") or {}).get("uid"):
        raise OdooConfigurationError(
            "Odoo web login failed. Report download requires the real "
            "ODOO_PASSWORD (an API key is not enough)."
        )

    ids = ",".join(str(picking_id) for picking_id in picking_ids)
    report_url = f"{url}/report/pdf/{PANEL_LABEL_REPORT}/{ids}"
    if company_ids:
        # pickings from another company 403 unless it is in allowed_company_ids
        report_url += "?context=" + urllib.parse.quote(
            json.dumps({"allowed_company_ids": list(company_ids)})
        )
    try:
        report_timeout = float(os.getenv("ODOO_REPORT_TIMEOUT", "300"))
    except ValueError as exc:
        raise OdooConfigurationError(
            "ODOO_REPORT_TIMEOUT must be a number"
        ) from exc
    # ponytail: Odoo renders large label batches synchronously; keep this tunable
    with opener.open(report_url, timeout=max(timeout, report_timeout)) as response:
        pdf_bytes = response.read()
    if not pdf_bytes.startswith(b"%PDF-"):
        raise OdooLookupError(
            f"Odoo did not return a PDF for report '{PANEL_LABEL_REPORT}'."
        )
    return pdf_bytes


def fetch_panel_label_pdf(
    po_number: str,
    *,
    execute: Execute | None = None,
    download: Callable[[list[int]], bytes] | None = None,
) -> dict[str, Any]:
    """Fetch the Panel Label PDF for the sale order linked to a purchase order.

    The PO's partner_ref holds the sale order name (e.g. 'S00334'); the label
    is printed from that SO's outgoing delivery orders until they are Done.
    """
    normalized_po = po_number.strip()
    if not normalized_po:
        raise ValueError("PO number is required.")

    if execute is None:
        execute, _ = _connect()

    order = _find_purchase_order(
        execute, normalized_po, ["id", "name", "partner_ref"]
    )
    so_number = str(order.get("partner_ref") or "").strip()
    if not so_number:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' has no partner_ref, so the "
            "sale order cannot be determined."
        )

    sale_orders = _search_read(
        execute,
        "sale.order",
        [("name", "=", so_number)],
        ["id", "name", "picking_ids"],
        limit=1,
    )
    if not sale_orders:
        raise OdooLookupError(
            f"Sale order '{so_number}' (partner_ref of '{normalized_po}') "
            "was not found."
        )
    sale_order = sale_orders[0]
    picking_ids = sale_order.get("picking_ids") or []
    if not picking_ids:
        raise OdooLookupError(
            f"Sale order '{so_number}' has no deliveries."
        )

    pickings = _search_read(
        execute,
        "stock.picking",
        [
            ("id", "in", picking_ids),
            ("state", "!=", "done"),
            ("picking_type_id.code", "=", "outgoing"),
        ],
        ["id", "state", "company_id"],
        order="id",
    )
    if not pickings:
        raise OdooLookupError(
            f"Sale order '{so_number}' has no delivery orders in "
            "a stage before Done."
        )

    if download is None:
        company_ids = sorted(
            {
                company_id
                for picking in pickings
                if (company_id := _many2one_id(picking.get("company_id")))
                is not None
            }
        )

        def download(ids: list[int]) -> bytes:
            return _download_report_pdf(ids, company_ids=company_ids)

    pdf_bytes = download([picking["id"] for picking in pickings])

    return {
        "po_number": order.get("name") or normalized_po,
        "so_number": so_number,
        "pdf_bytes": pdf_bytes,
    }


def normalize_sm_code(value: str) -> str:
    """Scanned SM code -> 'SM-<main>-<colour>'. Labels carry a bare 'B313-AD',
    so the 'SM-' prefix is always added. Idempotent (the UI also normalizes)."""
    parts = [part.strip().upper() for part in str(value or "").split("-")]
    if parts and parts[0] == "SM":
        parts = parts[1:]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) != 2:
        return "SM-" + "-".join(parts)

    def is_main_code(part: str) -> bool:
        return part.isdigit() or (
            len(part) > 1 and part[0].isalpha() and part[1:].isdigit()
        )

    first, second = parts
    if is_main_code(second) and len(first) == 2 and first.isalpha():
        first, second = second, first
    return f"SM-{first}-{second}"


def _sm_colour_suffix_parts(code: str) -> tuple[str, str] | None:
    """('SM-B313-AD') -> ('SM-B313', 'AD'); None when there is no colour suffix."""
    parts = code.split("-")
    if len(parts) < 3 or parts[0] != "SM":
        return None
    last = parts[-1]
    if len(last) != 2 or not last[0].isalpha() or not last.isalnum():
        return None
    return "-".join(parts[:-1]), last


def spaced_sm_colour_form(code: str) -> str:
    """'SM-0079-B3' -> 'SM-0079 - B3', the form Odoo stores colour variants in."""
    parts = _sm_colour_suffix_parts(code)
    return f"{parts[0]} - {parts[1]}" if parts else code


def strip_sm_colour_suffix(code: str) -> str:
    """'SM-B313-AD' -> 'SM-B313'; unchanged when there is no colour suffix.

    Odoo does not always hold the colour-suffixed variant, so a failed lookup
    retries the base code, whose search pulls the whole colour family."""
    parts = _sm_colour_suffix_parts(code)
    return parts[0] if parts else code


def lookup_part_codes(
    po_number: str,
    sm_codes: list[str],
    *,
    execute: Execute | None = None,
) -> dict[str, Any]:
    """
    Resolve each component product to finished products present on a purchase order.

    Relationship:
      mrp.bom.line.product_id (SM code)
        -> mrp.bom.line.bom_id
        -> mrp.bom.product_tmpl_id (finished product)
        -> purchase.order.line.product_id.product_tmpl_id
           (within the requested PO)
    """
    normalized_po = po_number.strip()
    normalized_codes = [
        normalize_sm_code(code) for code in sm_codes if code.strip()
    ]
    if not normalized_po:
        raise ValueError("PO number is required.")
    if not normalized_codes:
        raise ValueError("Enter at least one product code.")
    if len(normalized_codes) > 2:
        raise ValueError("A maximum of two product codes is allowed.")

    if execute is None:
        execute, _ = _connect()

    order = _find_purchase_order(
        execute, normalized_po, ["id", "name", "partner_ref"]
    )
    print(f"[lookup] PO {order.get('name')} partner_ref={order.get('partner_ref')!r}")

    result = _match_codes(execute, order, normalized_po, normalized_codes)
    if result["matches"]:
        return result
    # Retry only the codes Odoo could not resolve, so a code that did resolve
    # keeps its colour and still constrains the match.
    retry_codes = [
        item["sm_code"]
        if item["code_resolved"]
        else strip_sm_colour_suffix(item["sm_code"])
        for item in result["results"]
    ]
    if retry_codes == normalized_codes:
        return result
    print(f"[lookup] retrying without colour suffix: {retry_codes}")
    retry = _match_codes(execute, order, normalized_po, retry_codes)
    return retry if retry["matches"] else result


def _match_codes(
    execute: Execute,
    order: dict[str, Any],
    normalized_po: str,
    normalized_codes: list[str],
) -> dict[str, Any]:
    order_id = order["id"]
    required_code_counts = Counter(normalized_codes)
    unique_codes = list(dict.fromkeys(normalized_codes))

    product_code_by_id: dict[int, str] = {}
    product_ids_by_code: dict[str, set[int]] = {
        code: set() for code in unique_codes
    }
    ptav_ids_by_product: dict[int, list[int]] = {}
    for code in unique_codes:
        # active BOMs still reference archived components (e.g. SM-0082),
        # so include archived products in the lookup. A bare code without a
        # colour suffix (e.g. 'SM-0079') also pulls its colour variants
        # ('SM-0079 - B3', 'SM-0079-AA') so the user can pick a colour.
        # Labels print 'SM-0079-B3' while Odoo stores 'SM-0079 - B3', so the
        # spaced form is searched too - matching the exact variant keeps its
        # colour attributes, which is what narrows the finished product later.
        terms = [code, f"{code} - %", f"{code}-%"]
        spaced = spaced_sm_colour_form(code)
        if spaced != code:
            terms.append(spaced)
        products = _search_read(
            execute,
            "product.product",
            ["|"] * (len(terms) - 1)
            + [("default_code", "=ilike", term) for term in terms],
            ["id", "default_code", "product_template_attribute_value_ids"],
            limit=100,
            context={"active_test": False},
        )
        variant_prefixes = (f"{code} - ", f"{code}-")
        for product in products:
            actual_code = str(product.get("default_code") or "").strip().upper()
            if actual_code not in (code, spaced) and not actual_code.startswith(
                variant_prefixes
            ):
                continue
            product_id = product["id"]
            product_code_by_id[product_id] = code
            product_ids_by_code[code].add(product_id)
            ptav_ids_by_product[product_id] = list(
                product.get("product_template_attribute_value_ids") or []
            )

    product_ids = sorted(product_code_by_id)
    bom_lines = (
        _search_read(
            execute,
            "mrp.bom.line",
            [("product_id", "in", product_ids)],
            ["id", "product_id", "bom_id"],
        )
        if product_ids
        else []
    )

    bom_ids = sorted(
        {
            bom_id
            for line in bom_lines
            if (bom_id := _many2one_id(line.get("bom_id"))) is not None
        }
    )
    boms = (
        _search_read(
            execute,
            "mrp.bom",
            [("id", "in", bom_ids)],
            ["id", "product_tmpl_id"],
        )
        if bom_ids
        else []
    )
    template_id_by_bom = {
        bom["id"]: _many2one_id(bom.get("product_tmpl_id")) for bom in boms
    }

    template_ids_by_code: dict[str, set[int]] = {
        code: set() for code in unique_codes
    }
    template_code_counts_by_code: dict[str, dict[int, int]] = {
        code: {} for code in unique_codes
    }
    for line in bom_lines:
        product_id = _many2one_id(line.get("product_id"))
        bom_id = _many2one_id(line.get("bom_id"))
        code = product_code_by_id.get(product_id)
        template_id = template_id_by_bom.get(bom_id)
        if code in template_ids_by_code and template_id is not None:
            template_ids_by_code[code].add(template_id)
            counts = template_code_counts_by_code[code]
            counts[template_id] = counts.get(template_id, 0) + 1

    candidate_template_ids = sorted(
        {
            template_id
            for template_ids in template_ids_by_code.values()
            for template_id in template_ids
        }
    )
    purchase_lines = (
        _search_read(
            execute,
            "purchase.order.line",
            [
                ("order_id", "=", order_id),
                (
                    "product_id.product_tmpl_id",
                    "in",
                    candidate_template_ids,
                ),
            ],
            ["id", "product_id"],
        )
        if candidate_template_ids
        else []
    )

    purchase_product_ids = sorted(
        {
            product_id
            for line in purchase_lines
            if (
                product_id := _many2one_id(line.get("product_id"))
            )
            is not None
        }
    )
    purchase_products = (
        _search_read(
            execute,
            "product.product",
            [("id", "in", purchase_product_ids)],
            [
                "id",
                "product_tmpl_id",
                "name",
                "default_code",
                "product_template_attribute_value_ids",
            ],
            context={"active_test": False},
        )
        if purchase_product_ids
        else []
    )
    purchase_product_by_id = {
        product["id"]: product for product in purchase_products
    }

    matched_template_ids = sorted(
        {
            template_id
            for product in purchase_products
            if (
                template_id := _many2one_id(product.get("product_tmpl_id"))
            )
            is not None
        }
    )
    templates = (
        _search_read(
            execute,
            "product.template",
            [("id", "in", matched_template_ids)],
            ["id", "name", "default_code"],
            context={"active_test": False},
        )
        if matched_template_ids
        else []
    )
    template_by_id = {template["id"]: template for template in templates}

    # Components and finished products are colour variants linked only by a
    # shared attribute value (e.g. 'SM-0079 - B3' and 'M-001-C-B3' both carry
    # Colour: MATT PALACE DADO), so matching must compare attribute values.
    all_ptav_ids = sorted(
        {
            ptav_id
            for ptav_ids in ptav_ids_by_product.values()
            for ptav_id in ptav_ids
        }
        | {
            ptav_id
            for product in purchase_products
            for ptav_id in (
                product.get("product_template_attribute_value_ids") or []
            )
        }
    )
    ptavs = (
        _search_read(
            execute,
            "product.template.attribute.value",
            [("id", "in", all_ptav_ids)],
            ["id", "product_attribute_value_id"],
            context={"active_test": False},
        )
        if all_ptav_ids
        else []
    )
    value_id_by_ptav = {
        ptav["id"]: _many2one_id(ptav.get("product_attribute_value_id"))
        for ptav in ptavs
    }

    def attribute_values(ptav_ids: list[int]) -> frozenset[int]:
        return frozenset(
            value_id
            for ptav_id in ptav_ids
            if (value_id := value_id_by_ptav.get(ptav_id)) is not None
        )

    required_value_sets_by_code: dict[str, list[frozenset[int]]] = {
        code: [
            attribute_values(ptav_ids_by_product.get(product_id, []))
            for product_id in sorted(product_ids)
        ]
        for code, product_ids in product_ids_by_code.items()
    }
    finished_values_by_product = {
        product["id"]: attribute_values(
            product.get("product_template_attribute_value_ids") or []
        )
        for product in purchase_products
    }

    purchase_lines_by_product: dict[int, list[dict[str, Any]]] = {}
    for line in purchase_lines:
        product_id = _many2one_id(line.get("product_id"))
        if product_id is not None:
            purchase_lines_by_product.setdefault(product_id, []).append(line)

    results = []
    for code in normalized_codes:
        candidate_ids = template_ids_by_code.get(code, set())
        required_sets = required_value_sets_by_code.get(code) or [frozenset()]
        matches = []
        template_level_hit = False
        for product_id in sorted(purchase_lines_by_product):
            product = purchase_product_by_id.get(product_id, {})
            template_id = _many2one_id(product.get("product_tmpl_id"))
            if template_id not in candidate_ids:
                continue
            template_level_hit = True

            finished_values = finished_values_by_product.get(
                product_id, frozenset()
            )
            if not any(
                required <= finished_values for required in required_sets
            ):
                continue

            product_purchase_lines = sorted(
                purchase_lines_by_product[product_id],
                key=lambda line: int(line["id"]),
            )
            primary_line = product_purchase_lines[0]
            relation_name = _many2one_name(primary_line.get("product_id"))
            template = template_by_id.get(template_id, {})
            purchase_line_ids = [line["id"] for line in product_purchase_lines]
            matches.append(
                {
                    "part_code": product.get("default_code")
                    or template.get("default_code")
                    or relation_name,
                    "product_id": product_id,
                    "product_template_id": template_id,
                    "product_template_name": template.get("name")
                    or product.get("name")
                    or relation_name,
                    "purchase_order_line_id": primary_line["id"],
                    "purchase_order_line_ids": purchase_line_ids,
                    "purchase_order_line_count": len(purchase_line_ids),
                }
            )

        # code_resolved: the component itself exists and is used in a BOM. When
        # it is False the scanned code is wrong (or too specific) and is worth
        # retrying without its colour suffix; when it is True the code is right
        # and the PO/colour is the mismatch, so widening would only hide that.
        code_resolved = bool(product_ids_by_code.get(code) and candidate_ids)
        if not product_ids_by_code.get(code):
            error = f"Product '{code}' was not found in Odoo."
        elif not candidate_ids:
            error = f"No BOM line was found for component '{code}'."
        elif matches:
            error = None
        elif template_level_hit:
            error = (
                f"Component '{code}' matches finished products on purchase "
                f"order '{normalized_po}', but none share its colour/variant."
            )
        else:
            error = (
                f"Component '{code}' is used in BOMs, but none of their finished "
                f"products are present in purchase order '{normalized_po}'."
            )

        results.append(
            {
                "sm_code": code,
                "matches": matches,
                "error": error,
                "code_resolved": code_resolved,
            }
        )

    consolidated_by_product: dict[int, dict[str, Any]] = {}
    for item in results:
        for match in item["matches"]:
            key = match["product_id"]
            consolidated = consolidated_by_product.setdefault(
                key,
                {
                    **match,
                    "purchase_order_line_ids": list(
                        match.get(
                            "purchase_order_line_ids",
                            [match["purchase_order_line_id"]],
                        )
                    ),
                    "sm_codes": [],
                },
            )
            for line_id in match.get(
                "purchase_order_line_ids",
                [match["purchase_order_line_id"]],
            ):
                if line_id not in consolidated["purchase_order_line_ids"]:
                    consolidated["purchase_order_line_ids"].append(line_id)
            consolidated["purchase_order_line_count"] = len(
                consolidated["purchase_order_line_ids"]
            )
            if item["sm_code"] not in consolidated["sm_codes"]:
                consolidated["sm_codes"].append(item["sm_code"])

    matches = list(consolidated_by_product.values())
    if len(normalized_codes) > 1:
        matches = [
            match
            for match in matches
            if all(
                code in match["sm_codes"]
                and template_code_counts_by_code.get(code, {}).get(
                    match["product_template_id"], 0
                )
                >= count
                for code, count in required_code_counts.items()
            )
        ]
        for match in matches:
            match["sm_codes"] = list(normalized_codes)

    return {
        "po_number": order.get("name") or normalized_po,
        "purchase_order_id": order_id,
        "partner_ref": order.get("partner_ref"),
        "results": results,
        "matches": matches,
    }
