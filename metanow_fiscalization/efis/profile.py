"""efis's country profile (GET /v1/profiles/{country}), read as data.

BUILD_SPEC 7.4: clients fetch the enumerations rather than hard-coding them. The module keeps
the last profile it fetched and checks every configured code against it before an invoice is
posted. No code list is written into this module.
"""

import json
from decimal import Decimal, InvalidOperation

PAYMENT_METHODS = "paymentMethods"
CURRENCIES = "payments.currency"
COUNTRY_CODES = "buyer.countryCode"
IDENTIFIER_TYPES = "buyer.identifierType"
EXEMPTIONS = "lines.exemptionReason"
TAX_RATES = "lines.taxRate"


class Profile:
    def __init__(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("enumerations"), list):
            raise ValueError("not a country profile")
        self.document = document
        self.country = document.get("country")
        self.version = document.get("version")

    @classmethod
    def from_json(cls, text):
        if not text:
            return None
        return cls(json.loads(text))

    def to_json(self):
        return json.dumps(self.document, sort_keys=True, separators=(",", ":"))

    def _enumeration(self, field):
        for enumeration in self.document["enumerations"]:
            names = [name.strip() for name in str(enumeration.get("field", "")).split(",")]
            if field in names:
                return enumeration
        return None

    def codes(self, field):
        enumeration = self._enumeration(field)
        if enumeration is None:
            return frozenset()
        return frozenset(str(value.get("code")) for value in enumeration.get("values", []))

    def flag(self, field, code, flag):
        enumeration = self._enumeration(field)
        for value in (enumeration or {}).get("values", []):
            if value.get("code") == code:
                return bool((value.get("flags") or {}).get(flag))
        return False

    def tax_rates(self):
        rates = set()
        for code in self.codes(TAX_RATES):
            try:
                rates.add(Decimal(code))
            except InvalidOperation:
                continue
        return frozenset(rates)
