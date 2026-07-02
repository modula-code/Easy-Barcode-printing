import json
import os
import urllib.request
import xmlrpc.client
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


def _connect() -> tuple[Execute, int]:
    url, db, username, password, timeout = _settings()
    transport_class = (
        _TimeoutSafeTransport if url.lower().startswith("https://") else _TimeoutTransport
    )
    transport = transport_class(timeout)

    common = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/common",
        transport=transport,
        allow_none=True,
    )
    uid = common.authenticate(db, username, password, {})
    if not uid:
        raise OdooConfigurationError(
            "Odoo authentication failed. Check ODOO_DB, ODOO_USERNAME, and ODOO_PASSWORD."
        )

    models = xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object",
        transport=transport_class(timeout),
        allow_none=True,
    )

    def execute(
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        return models.execute_kw(db, uid, password, model, method, args, kwargs)

    return execute, uid


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

    orders = _search_read(
        execute,
        "purchase.order",
        [("name", "=", normalized_po)],
        ["id", "name", "partner_ref"],
        limit=1,
    )
    if not orders:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' was not found."
        )

    order = orders[0]
    return {
        "po_number": order.get("name") or normalized_po,
        "purchase_order_id": order["id"],
        "partner_ref": order.get("partner_ref"),
    }


def _download_report_pdf(picking_ids: list[int]) -> bytes:
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
    # ponytail: report rendering is slow on SaaS, floor the timeout at 120s
    with opener.open(report_url, timeout=max(timeout, 120)) as response:
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
    """Fetch the Panel Label PDF for a purchase order's waiting receipts."""
    normalized_po = po_number.strip()
    if not normalized_po:
        raise ValueError("PO number is required.")

    if execute is None:
        execute, _ = _connect()

    orders = _search_read(
        execute,
        "purchase.order",
        [("name", "=", normalized_po)],
        ["id", "name", "picking_ids"],
        limit=1,
    )
    if not orders:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' was not found."
        )
    order = orders[0]
    picking_ids = order.get("picking_ids") or []
    if not picking_ids:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' has no receipts."
        )

    # ponytail: 'waiting' and 'confirmed' both display as Waiting in Odoo
    pickings = _search_read(
        execute,
        "stock.picking",
        [("id", "in", picking_ids), ("state", "in", ["waiting", "confirmed"])],
        ["id", "name", "state"],
        order="id",
    )
    if not pickings:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' has no receipts in "
            "waiting state."
        )

    if download is None:
        download = _download_report_pdf
    pdf_bytes = download([picking["id"] for picking in pickings])

    return {
        "po_number": order.get("name") or normalized_po,
        "picking_names": [picking["name"] for picking in pickings],
        "pdf_bytes": pdf_bytes,
    }


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
        code.strip().upper() for code in sm_codes if code.strip()
    ]
    if not normalized_po:
        raise ValueError("PO number is required.")
    if not normalized_codes:
        raise ValueError("Enter at least one product code.")
    if len(normalized_codes) > 2:
        raise ValueError("A maximum of two product codes is allowed.")

    if execute is None:
        execute, _ = _connect()

    orders = _search_read(
        execute,
        "purchase.order",
        [("name", "=", normalized_po)],
        ["id", "name", "partner_ref"],
        limit=1,
    )
    if not orders:
        raise OdooLookupError(
            f"Purchase order '{normalized_po}' was not found."
        )

    order = orders[0]
    order_id = order["id"]
    print(f"[lookup] PO {order.get('name')} partner_ref={order.get('partner_ref')!r}")
    unique_codes = list(dict.fromkeys(normalized_codes))

    product_code_by_id: dict[int, str] = {}
    product_ids_by_code: dict[str, set[int]] = {
        code: set() for code in unique_codes
    }
    ptav_ids_by_product: dict[int, list[int]] = {}
    for code in unique_codes:
        # active BOMs still reference archived components (e.g. SM-0082),
        # so include archived products in the lookup
        products = _search_read(
            execute,
            "product.product",
            [("default_code", "=ilike", code)],
            ["id", "default_code", "product_template_attribute_value_ids"],
            limit=100,
            context={"active_test": False},
        )
        for product in products:
            actual_code = str(product.get("default_code") or "").strip().upper()
            if actual_code != code:
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
    for line in bom_lines:
        product_id = _many2one_id(line.get("product_id"))
        bom_id = _many2one_id(line.get("bom_id"))
        code = product_code_by_id.get(product_id)
        template_id = template_id_by_bom.get(bom_id)
        if code in template_ids_by_code and template_id is not None:
            template_ids_by_code[code].add(template_id)

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

        results.append({"sm_code": code, "matches": matches, "error": error})

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

    return {
        "po_number": order.get("name") or normalized_po,
        "purchase_order_id": order_id,
        "partner_ref": order.get("partner_ref"),
        "results": results,
        "matches": list(consolidated_by_product.values()),
    }
