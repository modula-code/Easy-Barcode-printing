import io
import unittest

from reportlab.pdfgen import canvas

from odoo_client import (
    OdooLookupError,
    fetch_panel_label_pdf,
    lookup_part_codes,
)
from pdf_service import prepare_matching_pages


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
                if domain and domain[0][0] == "default_code":
                    code = domain[0][2].upper()
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


class VariantMatchingTest(unittest.TestCase):
    """Colour-variant components must only match same-colour finished products."""

    @staticmethod
    def _execute(component_ptav_ids, po_products, ptav_value_by_id):
        def execute(model, method, args, kwargs):
            domain = args[0]

            if model == "purchase.order":
                return [{"id": 10, "name": "P0001", "partner_ref": None}]

            if model == "product.product":
                if domain and domain[0][0] == "default_code":
                    return [
                        {
                            "id": 101,
                            "default_code": domain[0][2].upper(),
                            "product_template_attribute_value_ids": (
                                component_ptav_ids
                            ),
                        }
                    ]
                return po_products

            if model == "mrp.bom.line":
                return [
                    {"id": 301, "product_id": [101, "SM"], "bom_id": [401, "B"]}
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

    def test_wrong_colour_variant_does_not_match(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0079 - B3"],
            execute=self._execute([7], [self.B9_PRODUCT], self.PTAV_VALUES),
        )

        self.assertEqual(result["matches"], [])
        self.assertIn("colour", result["results"][0]["error"])

    def test_same_colour_variant_matches_only_that_variant(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0079 - B3"],
            execute=self._execute(
                [7], [self.B9_PRODUCT, self.B3_PRODUCT], self.PTAV_VALUES
            ),
        )

        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["part_code"], "M-001-C-B3")

    def test_colourless_component_matches_every_variant(self):
        result = lookup_part_codes(
            "P0001",
            ["SM-0078"],
            execute=self._execute(
                [], [self.B9_PRODUCT, self.B3_PRODUCT], self.PTAV_VALUES
            ),
        )

        self.assertEqual(
            sorted(match["part_code"] for match in result["matches"]),
            ["M-001-C-B3", "M-001-C-B9"],
        )


class PanelLabelFetchTest(unittest.TestCase):
    @staticmethod
    def _execute_with_pickings(pickings):
        def execute(model, method, args, kwargs):
            if model == "purchase.order":
                return [
                    {
                        "id": 10,
                        "name": "P0001",
                        "picking_ids": [picking["id"] for picking in pickings],
                    }
                ]
            if model == "stock.picking":
                allowed_states = args[0][1][2]
                return [
                    picking
                    for picking in pickings
                    if picking["state"] in allowed_states
                ]
            return []

        return execute

    def test_downloads_report_for_waiting_pickings_only(self):
        downloaded = []

        def download(picking_ids):
            downloaded.append(picking_ids)
            return b"%PDF-fake"

        result = fetch_panel_label_pdf(
            "P0001",
            execute=self._execute_with_pickings(
                [
                    {"id": 71, "name": "WH/IN/71", "state": "confirmed"},
                    {"id": 72, "name": "WH/IN/72", "state": "done"},
                ]
            ),
            download=download,
        )

        self.assertEqual(downloaded, [[71]])
        self.assertEqual(result["pdf_bytes"], b"%PDF-fake")
        self.assertEqual(result["picking_names"], ["WH/IN/71"])

    def test_errors_when_no_waiting_pickings(self):
        with self.assertRaises(OdooLookupError):
            fetch_panel_label_pdf(
                "P0001",
                execute=self._execute_with_pickings(
                    [{"id": 71, "name": "WH/IN/71", "state": "done"}]
                ),
                download=lambda picking_ids: b"%PDF-fake",
            )


if __name__ == "__main__":
    unittest.main()
