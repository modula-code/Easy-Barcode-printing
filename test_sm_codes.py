import unittest

from odoo_client import (
    lookup_part_codes,
    normalize_sm_code,
    spaced_sm_colour_form,
    strip_sm_colour_suffix,
)


class SmCodeTest(unittest.TestCase):
    def test_normalizes_main_and_colour_code_order(self):
        cases = {
            "B313-AD": "SM-B313-AD",
            "AD-B313": "SM-B313-AD",
            "B313": "SM-B313",
            "T009-AM": "SM-T009-AM",
            "AM-T009": "SM-T009-AM",
            "A090-AM": "SM-A090-AM",
            "AM-A090": "SM-A090-AM",
            "009-AM": "SM-009-AM",
            "SM-T009-AM": "SM-T009-AM",
            " SM - AM - T009 ": "SM-T009-AM",
            "CUSTOM-CODE": "SM-CUSTOM-CODE",
            "SM-CUSTOM-CODE": "SM-CUSTOM-CODE",
            "": "",
            "  ": "",
        }
        for scanned, expected in cases.items():
            with self.subTest(scanned=scanned):
                self.assertEqual(normalize_sm_code(scanned), expected)
                # The UI normalizes on scan, the server again on lookup.
                self.assertEqual(normalize_sm_code(expected), expected)

    def test_strips_only_two_char_colour_suffix(self):
        cases = {
            "SM-B313-AD": "SM-B313",
            "SM-0079-AA": "SM-0079",
            "SM-0079-B3": "SM-0079",
            "SM-B313": "SM-B313",
            "SM-CUSTOM-CODE": "SM-CUSTOM-CODE",
            "B313-AD": "B313-AD",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(strip_sm_colour_suffix(code), expected)

    def test_spaced_form_matches_how_odoo_stores_colour_variants(self):
        cases = {
            "SM-0079-B3": "SM-0079 - B3",
            "SM-B313-AD": "SM-B313 - AD",
            "SM-B313": "SM-B313",
            "SM-CUSTOM-CODE": "SM-CUSTOM-CODE",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(spaced_sm_colour_form(code), expected)


def _fake_execute(products_by_code, component_colour=(), finished_colour=()):
    """Minimal Odoo stub: PO 'PO1' holds finished product 42, whose BOM uses
    whichever component codes appear in products_by_code. The colour args are
    product.template.attribute.value ids, used to exercise colour narrowing."""
    calls = []

    def execute(model, method, args, kwargs=None):
        calls.append((model, method, args[0] if args else []))
        domain = args[0] if args else []
        if model == "purchase.order":
            return [{"id": 1, "name": "PO1", "partner_ref": "SO1"}]
        if model == "product.product":
            ids = [term[2] for term in domain if term[0] == "id"]
            if ids:  # finished products on the PO
                return [
                    {
                        "id": 42,
                        "product_tmpl_id": [7, "Panel"],
                        "name": "Panel",
                        "default_code": "PANEL-1",
                        "product_template_attribute_value_ids": list(finished_colour),
                    }
                ]
            wanted = {
                str(term[2]).rstrip("- %").strip()
                for term in domain
                if isinstance(term, (list, tuple)) and term[0] == "default_code"
            }
            return [
                {
                    "id": product_id,
                    "default_code": code,
                    "product_template_attribute_value_ids": list(component_colour),
                }
                for code, product_id in products_by_code.items()
                if code in wanted
            ]
        if model == "mrp.bom.line":
            return [
                {"id": 1, "product_id": [product_id, code], "bom_id": [5, "BOM"]}
                for code, product_id in products_by_code.items()
            ]
        if model == "mrp.bom":
            return [{"id": 5, "product_tmpl_id": [7, "Panel"]}]
        if model == "purchase.order.line":
            return [{"id": 9, "product_id": [42, "Panel"]}]
        if model == "product.template":
            return [{"id": 7, "name": "Panel", "default_code": "PANEL-1"}]
        if model == "product.template.attribute.value":
            return [
                {"id": ptav_id, "product_attribute_value_id": [ptav_id, "colour"]}
                for term in domain
                for ptav_id in term[2]
                if term[0] == "id"
            ]
        return []

    return execute, calls


class LookupRetryTest(unittest.TestCase):
    def test_retries_without_colour_suffix_when_exact_code_misses(self):
        execute, _ = _fake_execute({"SM-B313": 11})
        result = lookup_part_codes("PO1", ["B313-AD"], execute=execute)
        self.assertEqual([m["part_code"] for m in result["matches"]], ["PANEL-1"])
        self.assertEqual(result["results"][0]["sm_code"], "SM-B313")

    def test_exact_hit_does_not_retry(self):
        execute, calls = _fake_execute({"SM-B313-AD": 11})
        result = lookup_part_codes("PO1", ["B313-AD"], execute=execute)
        self.assertEqual(result["results"][0]["sm_code"], "SM-B313-AD")
        self.assertEqual(
            sum(1 for model, _, _ in calls if model == "mrp.bom.line"), 1
        )

    def test_searches_the_spaced_form_odoo_stores(self):
        # Odoo holds colour variants as 'SM-B313 - AD', not 'SM-B313-AD'.
        execute, calls = _fake_execute({"SM-B313 - AD": 11})
        result = lookup_part_codes("PO1", ["B313-AD"], execute=execute)
        self.assertEqual([m["part_code"] for m in result["matches"]], ["PANEL-1"])
        self.assertEqual(result["results"][0]["sm_code"], "SM-B313-AD")
        self.assertEqual(
            sum(1 for model, _, _ in calls if model == "mrp.bom.line"), 1
        )

    def test_colour_mismatch_does_not_widen_to_the_base_code(self):
        # The component resolves, the PO's panel is another colour: retrying
        # the base code would drop the colour check and match it anyway.
        execute, calls = _fake_execute(
            {"SM-B313-AD": 11}, component_colour=(101,), finished_colour=(202,)
        )
        result = lookup_part_codes("PO1", ["B313-AD"], execute=execute)
        self.assertEqual(result["matches"], [])
        self.assertIn("colour/variant", result["results"][0]["error"])
        self.assertEqual(
            sum(1 for model, _, _ in calls if model == "mrp.bom.line"), 1
        )

    def test_reports_original_failure_when_retry_also_misses(self):
        execute, _ = _fake_execute({})
        result = lookup_part_codes("PO1", ["B313-AD"], execute=execute)
        self.assertEqual(result["matches"], [])
        self.assertIn("SM-B313-AD", result["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
