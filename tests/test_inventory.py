import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from pcbgit.inventory import _digikey, _mouser, check_inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bom = Path(self.temp.name) / "bom.csv"

    def write(self, rows):
        self.bom.write_text("Reference,Qty,DigiKey,Mouser,DNP\n" + rows, encoding="utf-8")

    def test_sufficient_stock_from_either_distributor(self):
        self.write("R1 R2,2,dk1,m1,\n")
        with patch("pcbgit.inventory._digikey", return_value=(0, "match")), patch("pcbgit.inventory._mouser", return_value=(6, "match")):
            result = check_inventory(self.bom, 3)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["findings"][0]["quantity"], 6)

    def test_insufficient_and_unknown_are_not_passes(self):
        self.write("U1,2,dk1,m1,\n")
        with patch("pcbgit.inventory._digikey", return_value=(1, "match")), patch("pcbgit.inventory._mouser", return_value=(None, "unavailable")):
            result = check_inventory(self.bom)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["findings"][0]["status"], "unknown")
        with patch("pcbgit.inventory._digikey", return_value=(1, "match")), patch("pcbgit.inventory._mouser", return_value=(0, "match")):
            self.assertEqual(check_inventory(self.bom)["findings"][0]["status"], "out_of_stock")

    def test_shared_sku_total_and_lookup_cache(self):
        self.write("R1,2,dk1,,\nR2,3,dk1,,\n")
        with patch("pcbgit.inventory._digikey", return_value=(4, "match")) as lookup:
            result = check_inventory(self.bom)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["findings"][0]["options"][0]["required"], 5)
        lookup.assert_called_once()

    def test_missing_credentials_skus_and_invalid_bom_fail(self):
        self.write("R1,1,dk1,,\n")
        with patch.dict("os.environ", {}, clear=True):
            result = check_inventory(self.bom)
        self.assertEqual(result["findings"][0]["status"], "unknown")
        self.write("R1,1,,,\n")
        self.assertEqual(check_inventory(self.bom)["status"], "fail")
        self.write("R1,abc,dk1,,\n")
        self.assertEqual(check_inventory(self.bom)["status"], "unknown")
        self.assertEqual(check_inventory(self.bom, skip=True)["status"], "skipped")

    def test_excludes_dnp_and_rejects_empty_bom(self):
        self.write("R1,1,,,yes\n")
        self.assertEqual(check_inventory(self.bom)["status"], "unknown")

    def test_invalid_configuration_and_missing_references_are_unknown(self):
        self.write("R1,1,dk1,,\n")
        self.assertEqual(check_inventory(self.bom, 0)["status"], "unknown")
        self.assertEqual(check_inventory(self.bom, True, skip=True)["status"], "unknown")
        self.bom.write_text("Qty,DigiKey\n1,dk1\n", encoding="utf-8")
        self.assertEqual(check_inventory(self.bom)["status"], "unknown")
        self.write(",1,dk1,,\n")
        self.assertEqual(check_inventory(self.bom)["status"], "unknown")

    def test_digikey_requires_exact_package_match(self):
        response = {"Product": {"ProductVariations": [{"DigiKeyProductNumber": "SKU-A", "QuantityAvailableforPackageType": 7}]}}
        with patch.dict("os.environ", {"DIGIKEY_CLIENT_ID": "id", "DIGIKEY_ACCESS_TOKEN": "token"}, clear=True), patch("pcbgit.inventory._json_request", return_value=response):
            self.assertEqual(_digikey("SKU-A")[0], 7)
            self.assertIsNone(_digikey("SKU-B")[0])

    def test_digikey_two_legged_oauth_and_details_request(self):
        response = {"Product": {"ProductVariations": [{"DigiKeyProductNumber": "SKU/A", "QuantityAvailableforPackageType": 2}]}}
        with patch.dict("os.environ", {"DIGIKEY_CLIENT_ID": "id", "DIGIKEY_CLIENT_SECRET": "secret", "DIGIKEY_ACCOUNT_ID": "account"}, clear=True), patch("pcbgit.inventory._json_request", side_effect=[{"access_token": "bearer"}, response]) as request:
            self.assertEqual(_digikey("SKU/A")[0], 2)
        token_request, detail_request = request.call_args_list
        self.assertEqual(token_request.args[0], "https://api.digikey.com/v1/oauth2/token")
        self.assertEqual(urllib.parse.parse_qs(token_request.kwargs["data"].decode())["grant_type"], ["client_credentials"])
        self.assertIn("/SKU%2FA/productdetails", detail_request.args[0])
        self.assertEqual(detail_request.kwargs["headers"]["X-DIGIKEY-Account-Id"], "account")
        self.assertEqual(detail_request.kwargs["headers"]["Authorization"], "Bearer bearer")

    def test_mouser_ambiguous_and_malformed_responses_unknown(self):
        response = {"SearchResults": {"Parts": [{"MouserPartNumber": "SKU-A", "Availability": "1,234 In Stock"}]}}
        with patch.dict("os.environ", {"MOUSER_API_KEY": "secret"}, clear=True), patch("pcbgit.inventory._json_request", return_value=response):
            self.assertEqual(_mouser("SKU-A")[0], 1234)
            self.assertIsNone(_mouser("SKU-B")[0])
            response["SearchResults"]["Parts"].append(response["SearchResults"]["Parts"][0])
            self.assertIsNone(_mouser("SKU-A")[0])

    def test_mouser_exact_search_and_numeric_stock_field(self):
        response = {"SearchResults": {"Parts": [{"MouserPartNumber": "SKU-A", "Availability": "Call for details", "AvailabilityInStock": "1,234"}]}}
        with patch.dict("os.environ", {"MOUSER_API_KEY": "secret"}, clear=True), patch("pcbgit.inventory._json_request", return_value=response) as request:
            self.assertEqual(_mouser("SKU-A")[0], 1234)
            payload = json.loads(request.call_args.kwargs["data"])
            self.assertEqual(payload["SearchByPartRequest"]["partSearchOptions"], "Exact")
            self.assertIsNone(_mouser("SKU-A|OTHER")[0])
            response["SearchResults"]["Parts"][0]["AvailabilityInStock"] = "1,,234"
            self.assertIsNone(_mouser("SKU-A")[0])

    def test_malformed_distributor_items_return_unknown(self):
        with patch.dict("os.environ", {"MOUSER_API_KEY": "secret", "DIGIKEY_CLIENT_ID": "id", "DIGIKEY_ACCESS_TOKEN": "token"}, clear=True):
            with patch("pcbgit.inventory._json_request", return_value={"Product": {"ProductVariations": [42]}}):
                self.assertIsNone(_digikey("SKU-A")[0])
            with patch("pcbgit.inventory._json_request", return_value={"SearchResults": {"Parts": [42]}}):
                self.assertIsNone(_mouser("SKU-A")[0])

    def test_api_error_is_sanitized(self):
        self.write("R1,1,,sku,\n")
        with patch.dict("os.environ", {"MOUSER_API_KEY": "very-secret"}, clear=True), patch("pcbgit.inventory._json_request", side_effect=OSError("very-secret")):
            result = check_inventory(self.bom)
        self.assertNotIn("very-secret", json.dumps(result))
        self.assertEqual(result["status"], "fail")


if __name__ == "__main__":
    unittest.main()
