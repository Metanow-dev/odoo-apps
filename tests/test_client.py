import re
import unittest
import urllib.error

import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis.client import EfisClient, EfisUnavailable, check_base_url

BASE = "https://efis.example.invalid"


class RouteTests(unittest.TestCase):
    def test_every_route_the_module_calls_is_in_the_openapi_document_with_its_method(self):
        paths = support.openapi()["paths"]
        for method, path in efis_client.ROUTES:
            self.assertIn(path, paths, path)
            self.assertIn(method.lower(), paths[path], f"{method} {path}")

    def test_the_module_calls_no_certificate_route(self):
        for _, path in efis_client.ROUTES:
            self.assertNotRegex(path, re.compile("certificate|pkcs12|key", re.I))


class BaseUrlTests(unittest.TestCase):
    def test_https_is_required_for_a_public_host(self):
        self.assertEqual(check_base_url("https://efis.example.invalid/"), BASE)
        with self.assertRaises(ValueError):
            check_base_url("http://efis.example.invalid")

    def test_plain_http_is_allowed_for_loopback_and_a_compose_service(self):
        self.assertEqual(check_base_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(check_base_url("http://api:8080"), "http://api:8080")

    def test_the_authority_is_never_a_base_url(self):
        host = ".".join(("efiskalizimi-test", "tatime", "gov", "al"))
        with self.assertRaises(ValueError):
            check_base_url(f"https://{host}/FiscalizationService")

    def test_credentials_in_the_url_are_refused(self):
        with self.assertRaises(ValueError):
            check_base_url("https://user:secret@efis.example.invalid")


class ClientTests(unittest.TestCase):
    def test_pairing_sends_only_the_code(self):
        opener = support.FakeOpener((200, {"credential": "c-1", "tenantId": "shembull", "channel": "odoo"}))
        answer = EfisClient(BASE, opener=opener).pair("123-456")
        self.assertEqual(answer.status, 200)
        request = opener.requests[0]
        self.assertEqual((request["method"], request["url"]), ("POST", BASE + "/v1/pair"))
        self.assertEqual(request["body"], {"code": "123-456"})
        self.assertNotIn("x-credential", request["headers"])

    def test_a_document_carries_the_credential_the_tenant_and_the_idempotency_key(self):
        opener = support.FakeOpener((202, support.document_response()))
        client = EfisClient(BASE, credential="c-1", tenant="shembull", opener=opener)
        answer = client.submit_document({"clientSystem": "odoo"}, "odoo-db-1-1")
        self.assertEqual(answer.status, 202)
        headers = opener.requests[0]["headers"]
        self.assertEqual(headers["x-credential"], "c-1")
        self.assertEqual(headers["x-tenant-id"], "shembull")
        self.assertEqual(headers["idempotency-key"], "odoo-db-1-1")
        self.assertEqual(headers["content-type"], "application/json")

    def test_a_document_is_never_sent_without_a_key(self):
        client = EfisClient(BASE, credential="c-1", tenant="shembull", opener=support.FakeOpener())
        with self.assertRaises(ValueError):
            client.submit_document({}, "")

    def test_an_unpaired_client_sends_nothing(self):
        opener = support.FakeOpener()
        with self.assertRaises(ValueError):
            EfisClient(BASE, opener=opener).get_document("x")
        self.assertEqual(opener.requests, [])

    def test_an_error_answer_is_returned_not_raised(self):
        opener = support.FakeOpener((422, {"code": "lines_required", "retryable": False, "authorityCode": None}))
        client = EfisClient(BASE, credential="c-1", tenant="shembull", opener=opener)
        answer = client.submit_document({}, "k")
        self.assertEqual(answer.status, 422)
        self.assertEqual(answer.body["code"], "lines_required")

    def test_no_answer_is_efis_unavailable(self):
        opener = support.FakeOpener(urllib.error.URLError("timed out"))
        client = EfisClient(BASE, credential="c-1", tenant="shembull", opener=opener)
        with self.assertRaises(EfisUnavailable):
            client.get_document("x")

    def test_path_parameters_are_escaped(self):
        opener = support.FakeOpener((200, {}))
        EfisClient(BASE, credential="c", tenant="t", opener=opener).get_document("../admin")
        self.assertEqual(opener.requests[0]["url"], BASE + "/v1/documents/..%2Fadmin")


if __name__ == "__main__":
    unittest.main()
