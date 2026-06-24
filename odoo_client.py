import os
import xmlrpc.client
from collections.abc import Callable
from typing import Any

from dotenv import load_dotenv

load_dotenv()

Execute = Callable[[str, str, list[Any], dict[str, Any]], Any]


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


def lookup_part_codes(
    so_number: str,
    sm_codes: list[str],
    *,
    execute: Execute | None = None,
) -> dict[str, Any]:
    """
    Resolve each component product to finished products present on a sale order.

    Relationship:
      mrp.bom.line.product_id (SM code)
        -> mrp.bom.line.bom_id
        -> mrp.bom.product_tmpl_id (finished product)
        -> sale.order.line.product_template_id (within the requested SO)
    """
    normalized_so = so_number.strip()
    normalized_codes = [code.strip().upper() for code in sm_codes]
    if not normalized_so:
        raise ValueError("SO number is required.")
    if len(normalized_codes) != 2 or any(not code for code in normalized_codes):
        raise ValueError("Exactly two product codes are required.")

    if execute is None:
        execute, _ = _connect()

    orders = _search_read(
        execute,
        "sale.order",
        [("name", "=", normalized_so)],
        ["id", "name"],
        limit=1,
    )
    if not orders:
        raise OdooLookupError(f"Sale order '{normalized_so}' was not found.")

    order = orders[0]
    order_id = order["id"]
    unique_codes = list(dict.fromkeys(normalized_codes))

    product_code_by_id: dict[int, str] = {}
    product_ids_by_code: dict[str, set[int]] = {
        code: set() for code in unique_codes
    }
    for code in unique_codes:
        products = _search_read(
            execute,
            "product.product",
            [("default_code", "=ilike", code)],
            ["id", "default_code"],
            limit=100,
        )
        for product in products:
            actual_code = str(product.get("default_code") or "").strip().upper()
            if actual_code != code:
                continue
            product_id = product["id"]
            product_code_by_id[product_id] = code
            product_ids_by_code[code].add(product_id)

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
    sale_lines = (
        _search_read(
            execute,
            "sale.order.line",
            [
                ("order_id", "=", order_id),
                ("product_template_id", "in", candidate_template_ids),
            ],
            ["id", "product_template_id"],
        )
        if candidate_template_ids
        else []
    )

    matched_template_ids = sorted(
        {
            template_id
            for line in sale_lines
            if (
                template_id := _many2one_id(line.get("product_template_id"))
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
        )
        if matched_template_ids
        else []
    )
    template_by_id = {template["id"]: template for template in templates}

    sale_lines_by_template: dict[int, list[dict[str, Any]]] = {}
    for line in sale_lines:
        template_id = _many2one_id(line.get("product_template_id"))
        if template_id is not None:
            sale_lines_by_template.setdefault(template_id, []).append(line)

    results = []
    for code in normalized_codes:
        candidate_ids = template_ids_by_code.get(code, set())
        matches = []
        for template_id in sorted(candidate_ids):
            for sale_line in sale_lines_by_template.get(template_id, []):
                relation_name = _many2one_name(
                    sale_line.get("product_template_id")
                )
                template = template_by_id.get(template_id, {})
                matches.append(
                    {
                        "part_code": template.get("default_code") or relation_name,
                        "product_template_id": template_id,
                        "product_template_name": template.get("name")
                        or relation_name,
                        "sale_order_line_id": sale_line["id"],
                    }
                )

        if not product_ids_by_code.get(code):
            error = f"Product '{code}' was not found in Odoo."
        elif not candidate_ids:
            error = f"No BOM line was found for component '{code}'."
        elif not matches:
            error = (
                f"Component '{code}' is used in BOMs, but none of their finished "
                f"products are present in sale order '{normalized_so}'."
            )
        else:
            error = None

        results.append({"sm_code": code, "matches": matches, "error": error})

    consolidated_by_line: dict[tuple[int, int], dict[str, Any]] = {}
    for item in results:
        for match in item["matches"]:
            key = (
                match["product_template_id"],
                match["sale_order_line_id"],
            )
            consolidated = consolidated_by_line.setdefault(
                key,
                {**match, "sm_codes": []},
            )
            if item["sm_code"] not in consolidated["sm_codes"]:
                consolidated["sm_codes"].append(item["sm_code"])

    return {
        "so_number": order.get("name") or normalized_so,
        "sale_order_id": order_id,
        "results": results,
        "matches": list(consolidated_by_line.values()),
    }
