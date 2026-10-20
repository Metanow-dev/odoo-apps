"""Shared fixtures for the module's tests. Nothing here calls a network."""

import io
import json
import sys
import urllib.error
from pathlib import Path

ODOO_DIR = Path(__file__).resolve().parents[1]
REPO = ODOO_DIR.parent
MODULE_DIR = ODOO_DIR / "metanow_fiscalization"
OPENAPI = REPO / "spec" / "openapi" / "v1.json"

if str(ODOO_DIR) not in sys.path:
    sys.path.insert(0, str(ODOO_DIR))

from fake_odoo import install  # noqa: E402

install()

from metanow_fiscalization.efis.profile import Profile  # noqa: E402


def openapi():
    return json.loads(OPENAPI.read_text(encoding="utf-8"))


# A small profile in efis's published shape (CountryProfile). The codes are test data, chosen to
# look like the archived contract's; the module reads whatever efis serves.
PROFILE_DOCUMENT = {
    "country": "AL",
    "version": "test",
    "contract": "test",
    "limits": {},
    "rules": [],
    "enumerations": [
        {"field": "paymentMethods", "source": "test", "values": [
            {"code": "ACCOUNT", "description": None, "flags": {"cash": False}},
            {"code": "BANKNOTE", "description": None, "flags": {"cash": True}},
        ]},
        {"field": "payments.currency", "source": "test", "values": [
            # As the real profile publishes it: the XSD's CurrencyCodeSType, which has no ALL.
            {"code": "EUR", "description": None, "flags": {}},
        ]},
        {"field": "seller.countryCode, buyer.countryCode", "source": "test", "values": [
            {"code": "ALB", "description": None, "flags": {}},
        ]},
        {"field": "seller.identifierType, buyer.identifierType", "source": "test", "values": [
            {"code": "tax", "description": None, "flags": {"domestic": True}},
            {"code": "passport", "description": None, "flags": {"domestic": False}},
        ]},
        {"field": "lines.exemptionReason", "source": "test", "values": [
            {"code": "type_1", "description": None, "flags": {"requiresZeroRate": True}},
        ]},
        {"field": "lines.taxRate", "source": "test", "values": [
            {"code": "0", "description": None, "flags": {"zero": True}},
            {"code": "6", "description": None, "flags": {"zero": False}},
            {"code": "10", "description": None, "flags": {"zero": False}},
            {"code": "20", "description": None, "flags": {"zero": False}},
        ]},
    ],
}


def profile():
    return Profile(json.loads(json.dumps(PROFILE_DOCUMENT)))


def config(**overrides):
    values = {
        "client_system": "odoo",
        "tax_id": "K12345678L",
        "issuer_in_vat": True,
        "seller_name": "Shembull sh.p.k.",
        "seller_address": "Rruga e Durrësit 1",
        "seller_town": "Tiranë",
        "seller_country_code": None,
        "business_unit_code": "bb123bb123",
        "operator_code": "oo123oo123",
        "software_code": "ss123ss123",
        "device_code": None,
    }
    values.update(overrides)
    return values


def line(**overrides):
    values = {
        "description": "Karrige",
        "unit": "copë",
        "quantity": 2.0,
        "price_subtotal": 200.0,
        "price_total": 240.0,
        "item_code": "K-1",
        "taxes": [{"amount_type": "percent", "amount": 20.0, "exemption": False}],
    }
    values.update(overrides)
    return values


def invoice(**overrides):
    values = {
        "reference": "INV/2026/00001",
        "currency": "ALL",
        "company_currency": "ALL",
        "amount_total": 240.0,
        "amount_untaxed": 200.0,
        "amount_tax": 40.0,
        "payment_method": "ACCOUNT",
        "buyer": {
            "name": "Blerës sh.p.k.",
            "identifier": "L98765432K",
            "identifier_type": False,
            "address": "Rruga 2",
            "town": "Durrës",
            "country_code": None,
        },
        "lines": [line()],
    }
    values.update(overrides)
    return values


SECURITY_CODE = "0123456789abcdef0123456789abcdef"


def document_response(**overrides):
    body = {
        "id": "7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88",
        "state": "numbered",
        "fiscalNumber": "1/2026/bb123bb123",
        "securityCode": SECURITY_CODE,
        "registrationCode": None,
        "verificationUrl": "https://verify.example.invalid/check?code=abc&tin=K12345678L",
    }
    body.update(overrides)
    return body


class FakeAnswer:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._raw = b"" if body is None else json.dumps(body).encode("utf-8")
        self.headers = _Headers(headers or {"Content-Type": "application/json"})

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Headers(dict):
    pass


class FakeOpener:
    """Stands for urllib.request.urlopen. Records each request; answers from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request, data=None, timeout=None):
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "headers": {name.lower(): value for name, value in request.header_items()},
            "body": None if data is None else json.loads(data.decode("utf-8")),
            "timeout": timeout,
        })
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "error", _Headers({"Content-Type": "application/json"}),
                io.BytesIO(b"" if body is None else json.dumps(body).encode("utf-8")))
        return FakeAnswer(status, body)
