import io
import time
import unittest
from unittest import mock

from reportlab.pdfgen import canvas

import odoo_client
from app import app as flask_app
from odoo_client import (
    OdooLookupError,
    fetch_panel_label_pdf,
    lookup_part_codes,
    suggest_purchase_orders,
)
from pdf_service import prepare_matching_pages


def _domain_code(domain):
    """First default_code leaf in a domain that may contain '|' operators."""
    for term in domain:
        if isinstance(term, (list, tuple)) and term[0] == "default_code":
            return term[2]
    return None


class LookupBehaviorTest(unittest.TestCase):
    def test_same_product_template_is_returned_once(self):
        def execute(model, method, args, kwargs):
            self.assertEqual(method, "search_read")
            domain = args[0]

            if model == "purchase.order":
                return [
                    {"id": 10, "name": "P0001", "partner_ref": "Vendor Ref"}
                ]

            if model == "product.product":
                searched_code = _domain_code(domain)
                if searched_code is not None:
                    code = searched_code.upper()
                    component_ids = {"SM-1": 101, "SM-2": 102}
                    if code in component_ids:
                        return [
                            {
                                "id": component_ids[code],
                                "default_code": code,
                            }
                        ]
                    return []

                return [
                    {
                        "id": 201,
                        "product_tmpl_id": [501, "Finished Panel"],
                        "name": "Finished Panel",
                        "default_code": "PANEL-1",
                    }
                ]

            if model == "mrp.bom.line":
                return [
                    {
                        "id": 301,
                        "product_id": [101, "SM-1"],
                        "bom_id": [401, "BOM 1"],
                    },
                    {
                        "id": 302,
                        "product_id": [102, "SM-2"],
                        "bom_id": [401, "BOM 1"],
                    },
                ]

            if model == "mrp.bom":
                return [
                    {
                        "id": 401,
                        "product_tmpl_id": [501, "Finished Panel"],
                    }
                ]

            if model == "purchase.order.line":
                return [
                    {"id": 601, "product_id": [201, "PANEL-1"]},
                    {"id": 602, "product_id": [201, "PANEL-1"]},
                ]

            if model == "product.template":
                return [
                    {
                        "id": 501,
                        "name": "Finished Panel",
                        "default_code": "PANEL-T",
                    }
                ]

            return []

        result = lookup_part_codes("P0001", ["SM-1", "SM-2"], execute=execute)

        self.assertEqual(len(result["matches"]), 1)
        match = result["matches"][0]
        self.assertEqual(match["product_template_id"], 501)
        self.assertEqual(match["purchase_order_line_id"], 601)
        self.assertEqual(match["purchase_order_line_ids"], [601, 602])
        self.assertEqual(match["purchase_order_line_count"], 2)
        self.assertEqual(match["sm_codes"], ["SM-1", "SM-2"])
        self.assertEqual(
            [len(item["matches"]) for item in result["results"]],
            [1, 1],
        )

    def test_two_codes_show_only_panels_matching_both_codes(self):
        def execute(model, method, args, kwargs):
            domain = args[0]

            if model == "purchase.order":
                return [
                    {"id": 10, "name": "P0001", "partner_ref": "Vendor Ref"}
                ]

            if model == "product.product":
                searched_code = _domain_code(domain)
                if searched_code is not None:
                    code = searched_code.upper()
                    component_ids = {"SM-1": 101, "SM-2": 102}
                    return [
                        {
                            "id": component_ids[code],
                            "default_code": code,
                        }
                    ] if code in component_ids else []

                return [
                    {
                        "id": 201,
                        "product_tmpl_id": [501, "Panel Both"],
                        "name": "Panel Both",
                        "default_code": "PANEL-BOTH",
                    },
                    {
                        "id": 202,
                        "product_tmpl_id": [502, "Panel SM1 Only"],
                        "name": "Panel SM1 Only",
                        "default_code": "PANEL-SM1",
                    },
                    {
                        "id": 203,
                        "product_tmpl_id": [503, "Panel SM2 Only"],
                        "name": "Panel SM2 Only",
                        "default_code": "PANEL-SM2",
                    },
                ]

            if model == "mrp.bom.line":
                return [
                    {"id": 301, "product_id": [101, "SM-1"], "bom_id": [401, "BOTH"]},
                    {"id": 302, "product_id": [101, "SM-1"], "bom_id": [402, "SM1"]},
                    {"id": 303, "product_id": [102, "SM-2"], "bom_id": [401, "BOTH"]},
                    {"id": 304, "product_id": [102, "SM-2"], "bom_id": [403, "SM2"]},
                ]

            if model == "mrp.bom":
                return [
                    {"id": 401, "product_tmpl_id": [501, "Panel Both"]},
                    {"id": 402, "product_tmpl_id": [502, "Panel SM1 Only"]},
                    {"id": 403, "product_tmpl_id": [503, "Panel SM2 Only"]},
                ]

            if model == "purchase.order.line":
                return [
                    {"id": 601, "product_id": [201, "PANEL-BOTH"]},
                    {"id": 602, "product_id": [202, "PANEL-SM1"]},
                    {"id": 603, "product_id": [203, "PANEL-SM2"]},
                ]

            if model == "product.template":
                return [
                    {"id": 501, "name": "Panel Both", "default_code": "PANEL-BOTH"},
                    {"id": 502, "name": "Panel SM1 Only", "default_code": "PANEL-SM1"},
                    {"id": 503, "name": "Panel SM2 Only", "default_code": "PANEL-SM2"},
                ]

            return []

        result = lookup_part_codes("P0001", ["SM-1", "SM-2"], execute=execute)

        self.assertEqual(
            [match["part_code"] for match in result["matches"]],
            ["PANEL-BOTH"],
        )
        self.assertEqual(result["matches"][0]["sm_codes"], ["SM-1", "SM-2"])

    def test_pdf_matching_counts_label_pages_separately_from_text_hits(self):
        pdf_buffer = io.BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "P0222-AA P0222-AA P0222-AA")
        pdf.showPage()
        pdf.drawString(72, 720, "P0222-AA")
        pdf.showPage()
        pdf.save()

        result = prepare_matching_pages(
            pdf_buffer.getvalue(),
            ["P0222-AA"],
        )["P0222-AA"]

        self.assertEqual(result["occurrence_count"], 4)
        self.assertEqual(result["matching_page_count"], 2)
        self.assertEqual(result["page_numbers"], [1, 2])


class PrintRouteTest(unittest.TestCase):
    def test_default_print_link_opens_extracted_pdf(self):
        pdf_buffer = io.BytesIO()
        pdf = canvas.Canvas(pdf_buffer)
        pdf.drawString(72, 720, "P0222-AA")
        pdf.showPage()
        pdf.save()
        token = prepare_matching_pages(
            pdf_buffer.getvalue(),
            ["P0222-AA"],
        )["P0222-AA"]["first_token"]

        response = flask_app.test_client().get(f"/print/{token}")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"/api/print-pages/{token}.pdf",
        )


class VariantMatchingTest(unittest.TestCase):
    """Colour-variant components must only match same-colour finished products."""

    @staticmethod
    def _execute(component_products, po_products, ptav_value_by_id):
        def execute(model, method, args, kwargs):
            domain = args[0]

            if model == "purchase.order":
                return [{"id": 10, "name": "P0001", "partner_ref": None}]

            if model == "product.product":
                if _domain_code(domain) is not None:
                    return component_products
                return po_products

            if model == "mrp.bom.line":
                return [
                    {
                        "id": 300 + product["id"],
                        "product_id": [product["id"], "SM"],
                        "bom_id": [401, "B"],
                    }
                    for product in component_products
                ]

            if model == "mrp.bom":
                return [{"id": 401, "product_tmpl_id": [501, "L END HINGE"]}]

            if model == "purchase.order.line":
                return [
                    {
                        "id": 600 + product["id"],
                        "product_id": [product["id"], product["default_code"]],
                    }
                    for product in po_products
                ]

            if model == "product.template":
                return [
                    {"id": 501, "name": "L END HINGE", "default_code": False}
                ]

            if model == "product.template.attribute.value":
                return [
                    {
                        "id": ptav_id,
                        "product_attribute_value_id": [
                            ptav_value_by_id[ptav_id],
                            "Colour",
                        ],
                    }
                    for ptav_id in domain[0][2]
                    if ptav_id in ptav_value_by_id
                ]

            return []

        return execute

    B3_PRODUCT = {
        "id": 202,
        "default_code": "M-001-C-B3",
        "name": "L END HINGE (DADO)",
        "product_tmpl_id": [501, "L END HINGE"],
        "product_template_attribute_value_ids": [7],
    }
    B9_PRODUCT = {
        "id": 201,
        "default_code": "M-001-C-B9",
        "name": "L END HINGE (EARTH)",
        "product_tmpl_id": [501, "L END HINGE"],
        "product_template_attribute_value_ids": [8],
    }
    PTAV_VALUES = {7: 37, 8: 54}

    SM_B3_COMPONENT = {
        "id": 101,
        "default_code": "SM-0079 - B3",
        "product_template_attribute_value_ids": [7],
    }
    SM_B9_COMPONENT = {
        "id": 102,
        "default_code": "SM-0079 - B9",
        "product_template_attribute_value_ids": [8],
    }

    def test_wrong_colour_variant_does_not_match(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0079 - B3"],
            execute=self._execute(
                [self.SM_B3_COMPONENT], [self.B9_PRODUCT], self.PTAV_VALUES
            ),
        )

        self.assertEqual(result["matches"], [])
        self.assertIn("colour", result["results"][0]["error"])

    def test_same_colour_variant_matches_only_that_variant(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0079 - B3"],
            execute=self._execute(
                [self.SM_B3_COMPONENT],
                [self.B9_PRODUCT, self.B3_PRODUCT],
                self.PTAV_VALUES,
            ),
        )

        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["part_code"], "M-001-C-B3")

    def test_colourless_component_matches_every_variant(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0078"],
            execute=self._execute(
                [
                    {
                        "id": 101,
                        "default_code": "SM-0078",
                        "product_template_attribute_value_ids": [],
                    }
                ],
                [self.B9_PRODUCT, self.B3_PRODUCT],
                self.PTAV_VALUES,
            ),
        )

        self.assertEqual(
            sorted(match["part_code"] for match in result["matches"]),
            ["M-001-C-B3", "M-001-C-B9"],
        )

    def test_bare_code_offers_every_colour_variant_on_the_po(self):
        # scanning 'SM-0079' (no colour suffix) pulls 'SM-0079 - B3' and
        # 'SM-0079 - B9', so both PO colours come back for the user to choose
        result = lookup_part_codes(
            "P0001",
            ["SM-0079"],
            execute=self._execute(
                [self.SM_B3_COMPONENT, self.SM_B9_COMPONENT],
                [self.B9_PRODUCT, self.B3_PRODUCT],
                self.PTAV_VALUES,
            ),
        )

        self.assertEqual(
            sorted(match["part_code"] for match in result["matches"]),
            ["M-001-C-B3", "M-001-C-B9"],
        )


class PanelLabelFetchTest(unittest.TestCase):
    """PO.partner_ref names the SO; labels come from non-Done SO deliveries."""

    @staticmethod
    def _execute_with_pickings(
        pickings, partner_ref="S00333", po_name="P0001"
    ):
        def execute(model, method, args, kwargs):
            if model == "purchase.order":
                for leaf in args[0]:
                    if not isinstance(leaf, (list, tuple)) or leaf[0] != "name":
                        continue
                    _, op, value = leaf
                    matched = (
                        po_name in value
                        if op == "in"
                        else po_name.endswith(value.lstrip("%"))
                    )
                    if matched:
                        return [
                            {
                                "id": 10,
                                "name": po_name,
                                "partner_ref": partner_ref,
                            }
                        ]
                return []
            if model == "sale.order":
                if args[0][0][2] != partner_ref:
                    return []
                return [
                    {
                        "id": 20,
                        "name": partner_ref,
                        "picking_ids": [picking["id"] for picking in pickings],
                    }
                ]
            if model == "stock.picking":
                state_leaf = next(
                    leaf
                    for leaf in args[0]
                    if isinstance(leaf, (list, tuple)) and leaf[0] == "state"
                )
                _, op, value = state_leaf
                return [
                    picking
                    for picking in pickings
                    if (
                        picking["state"] != value
                        if op == "!="
                        else picking["state"] == value
                    )
                ]
            return []

        return execute

    def test_downloads_report_for_non_done_deliveries_of_the_so(self):
        downloaded = []

        def download(picking_ids):
            downloaded.append(picking_ids)
            return b"%PDF-fake"

        result = fetch_panel_label_pdf(
            "P0001",
            execute=self._execute_with_pickings(
                [
                    {"id": 71, "name": "WH/OUT/71", "state": "assigned"},
                    {"id": 72, "name": "WH/OUT/72", "state": "confirmed"},
                    {"id": 73, "name": "WH/OUT/73", "state": "done"},
                ]
            ),
            download=download,
        )

        self.assertEqual(downloaded, [[71, 72]])
        self.assertEqual(result["pdf_bytes"], b"%PDF-fake")
        self.assertEqual(result["so_number"], "S00333")
        self.assertEqual(result["picking_names"], ["WH/OUT/71", "WH/OUT/72"])

    def test_errors_when_only_done_deliveries(self):
        with self.assertRaises(OdooLookupError):
            fetch_panel_label_pdf(
                "P0001",
                execute=self._execute_with_pickings(
                    [{"id": 71, "name": "WH/OUT/71", "state": "done"}]
                ),
                download=lambda picking_ids: b"%PDF-fake",
            )

    def test_errors_when_po_has_no_partner_ref(self):
        with self.assertRaises(OdooLookupError):
            fetch_panel_label_pdf(
                "P0001",
                execute=self._execute_with_pickings([], partner_ref=None),
                download=lambda picking_ids: b"%PDF-fake",
            )

    def test_short_floor_code_resolves_to_full_po_name(self):
        # the floor only knows 'PO09862'; company 1 names it 06/26-27/PO09862
        result = fetch_panel_label_pdf(
            "PO09862",
            execute=self._execute_with_pickings(
                [{"id": 71, "name": "WH/OUT/71", "state": "assigned"}],
                po_name="06/26-27/PO09862",
            ),
            download=lambda picking_ids: b"%PDF-fake",
        )

        self.assertEqual(result["po_number"], "06/26-27/PO09862")
        self.assertEqual(result["so_number"], "S00333")

    def test_number_only_floor_code_resolves_to_full_po_name(self):
        result = fetch_panel_label_pdf(
            "09862",
            execute=self._execute_with_pickings(
                [{"id": 71, "name": "WH/OUT/71", "state": "assigned"}],
                po_name="06/26-27/PO09862",
            ),
            download=lambda picking_ids: b"%PDF-fake",
        )

        self.assertEqual(result["po_number"], "06/26-27/PO09862")

    def test_ambiguous_short_code_raises(self):
        def execute(model, method, args, kwargs):
            if model == "purchase.order":
                field, op, value = args[0][0]
                if op == "=ilike":
                    return [
                        {"id": 10, "name": "06/26-27/PO0001"},
                        {"id": 11, "name": "05/25-26/PO0001"},
                    ]
            return []

        with self.assertRaises(OdooLookupError) as ctx:
            fetch_panel_label_pdf(
                "PO0001",
                execute=execute,
                download=lambda picking_ids: b"%PDF-fake",
            )
        self.assertIn("ambiguous", str(ctx.exception))


class PurchaseOrderSuggestionTest(unittest.TestCase):
    def test_number_only_query_suggests_full_po_names(self):
        seen_domains = []

        def execute(model, method, args, kwargs):
            self.assertEqual(model, "purchase.order")
            seen_domains.append(args[0])
            return [
                {
                    "id": 10,
                    "name": "06/26-27/PO09862",
                    "partner_ref": "S00333",
                }
            ]

        result = suggest_purchase_orders("098", execute=execute)

        self.assertIn("PO098", str(seen_domains[0]))
        self.assertEqual(
            result,
            [
                {
                    "po_number": "06/26-27/PO09862",
                    "purchase_order_id": 10,
                    "partner_ref": "S00333",
                }
            ],
        )


class OdooConnectionTest(unittest.TestCase):
    def test_cached_uid_is_reused_without_reauthenticating(self):
        original = odoo_client._uid
        odoo_client._uid = 42
        try:
            self.assertEqual(odoo_client._get_uid(), 42)
        finally:
            odoo_client._uid = original

    def test_connection_error_resets_and_retries_once(self):
        with mock.patch.object(
            odoo_client,
            "_rpc",
            side_effect=[ConnectionResetError("dropped"), [{"id": 1}]],
        ) as rpc, mock.patch.object(odoo_client, "_reset_connection") as reset:
            result = odoo_client._shared_execute(
                "stock.picking", "search_read", [[]], {}
            )

        self.assertEqual(result, [{"id": 1}])
        self.assertEqual(rpc.call_count, 2)
        reset.assert_called_once()

    def test_second_failure_propagates(self):
        with mock.patch.object(
            odoo_client,
            "_rpc",
            side_effect=ConnectionResetError("still down"),
        ), mock.patch.object(odoo_client, "_reset_connection"):
            with self.assertRaises(ConnectionResetError):
                odoo_client._shared_execute(
                    "stock.picking", "search_read", [[]], {}
                )


class OdooCacheTest(unittest.TestCase):
    def setUp(self):
        odoo_client._cache.clear()

    def tearDown(self):
        odoo_client._cache.clear()

    def test_identical_search_read_served_from_cache(self):
        with mock.patch.object(
            odoo_client, "_rpc", return_value=[{"id": 7}]
        ) as rpc:
            first = odoo_client._shared_execute(
                "product.product", "search_read", [[]], {"fields": ["id"]}
            )
            second = odoo_client._shared_execute(
                "product.product", "search_read", [[]], {"fields": ["id"]}
            )

        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(first, second)
        self.assertIsNot(first[0], second[0])  # deepcopy protects the cache

    def test_expired_entry_is_refetched(self):
        with mock.patch.object(
            odoo_client, "_rpc", return_value=[{"id": 7}]
        ) as rpc:
            odoo_client._shared_execute(
                "product.product", "search_read", [[]], {"fields": ["id"]}
            )
            key, (_, value) = next(iter(odoo_client._cache.items()))
            odoo_client._cache[key] = (time.monotonic() - 1, value)
            odoo_client._shared_execute(
                "product.product", "search_read", [[]], {"fields": ["id"]}
            )

        self.assertEqual(rpc.call_count, 2)


if __name__ == "__main__":
    unittest.main()
