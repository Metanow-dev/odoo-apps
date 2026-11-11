import unittest
from decimal import Decimal

import support
from metanow_fiscalization.efis import money
from metanow_fiscalization.efis.payload import PayloadInvalid, build


def _schema_properties(document, name, seen=None):
    """Every property name reachable from a schema, keyed by schema name."""
    schemas = document["components"]["schemas"]
    seen = {} if seen is None else seen
    if name in seen:
        return seen
    schema = schemas[name]
    seen[name] = set(schema.get("properties", {}))
    for prop in schema.get("properties", {}).values():
        for ref in _refs(prop):
            _schema_properties(document, ref, seen)
    return seen


def _refs(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node["$ref"].rsplit("/", 1)[-1]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


class MoneyTests(unittest.TestCase):
    def test_money_always_has_two_decimals(self):
        self.assertEqual(money.format_money(Decimal("240")), "240.00")
        self.assertEqual(money.format_money(Decimal("99.1")), "99.10")
        self.assertEqual(money.format_money(money.cents(0.3)), "0.30")

    def test_money_that_is_not_whole_cents_is_refused_not_rounded(self):
        with self.assertRaises(money.NotExact):
            money.format_money(Decimal("1.005"))
        with self.assertRaises(money.NotExact):
            money.cents(1.005)

    def test_float_noise_is_read_as_the_cent_value(self):
        self.assertEqual(money.cents(0.1 + 0.2), Decimal("0.30"))

    def test_rounding_is_half_away_from_zero_like_efis(self):
        self.assertEqual(money.round_cents(Decimal("0.125")), Decimal("0.13"))
        self.assertEqual(money.round_cents(Decimal("-0.125")), Decimal("-0.13"))

    def test_unit_price_reproduces_the_line_total(self):
        price = money.unit_price(Decimal("118.00"), Decimal("3"))
        self.assertEqual(money.round_cents(Decimal(3) * price), Decimal("118.00"))
        self.assertEqual(money.format_plain(price), "39.3333333333")

    def test_plain_numbers_have_no_exponent(self):
        self.assertEqual(money.format_plain(Decimal("100")), "100")
        self.assertEqual(money.format_plain(Decimal("1E+2")), "100")
        self.assertEqual(money.format_plain(Decimal("0.50")), "0.5")


class ElectronicTests(unittest.TestCase):
    def build(self, invoice=None, **config):
        values = {"einvoice": True}
        values.update(config)
        return build(invoice or support.invoice(), support.config(**values), support.profile())

    def with_buyer(self, **overrides):
        buyer = dict(support.invoice()["buyer"])
        buyer.update(overrides)
        return support.invoice(buyer=buyer)

    def test_a_non_cash_invoice_to_a_taxpayer_is_an_e_invoice(self):
        self.assertIs(self.build()["electronic"], True)

    def test_the_company_can_turn_e_invoices_off(self):
        self.assertNotIn("electronic", self.build(einvoice=False))

    def test_a_cash_invoice_is_never_an_e_invoice(self):
        body = self.build(support.invoice(payment_method="BANKNOTE"), device_code="dd123dd123")
        self.assertNotIn("electronic", body)

    def test_a_buyer_without_a_tax_number_gets_a_fiscal_invoice(self):
        self.assertNotIn("electronic", self.build(self.with_buyer(identifier=None)))
        self.assertNotIn("electronic", self.build(support.invoice(buyer=None)))

    def test_a_buyer_named_by_another_identifier_gets_a_fiscal_invoice(self):
        self.assertNotIn("electronic", self.build(self.with_buyer(identifier="AB123456", identifier_type="passport")))

    def test_the_flag_is_in_efis_request_schema(self):
        properties = support.openapi()["components"]["schemas"]["SubmitDocumentRequest"]["properties"]
        self.assertIn("electronic", properties)


class EinvoiceFieldsTests(unittest.TestCase):
    """DEV-2358: what an e-invoice carries beyond the fiscal record (efis DEV-2354)."""

    IBAN = "AL35202111090000000001234567"

    def invoice(self, **overrides):
        values = {
            "due_date": "2026-12-10",
            "customer_reference": "PO-778",
            "delivery_date": "2026-11-09",
            "bank": {"iban": "al35 2021 1109 0000 0000 0123 4567", "bic": "sgsbalta", "holder": "Shembull sh.p.k."},
        }
        values.update(overrides)
        return support.invoice(**values)

    def build(self, invoice, **config):
        values = {"einvoice": True}
        values.update(config)
        return build(invoice, support.config(**values), support.profile())

    def test_an_e_invoice_carries_the_due_date_the_account_the_reference_and_the_delivery_date(self):
        body = self.build(self.invoice())
        self.assertEqual(body["paymentDueDate"], "2026-12-10")
        self.assertEqual(body["einvoice"], {
            "payment": {"accountId": self.IBAN, "serviceProvider": "SGSBALTA", "accountName": "Shembull sh.p.k."},
            "buyerReference": "PO-778",
            "delivery": {"date": "2026-11-09"},
        })

    def test_a_plain_fiscal_invoice_carries_none_of_them(self):
        body = self.build(self.invoice(), einvoice=False)
        self.assertNotIn("paymentDueDate", body)
        self.assertNotIn("einvoice", body)

    def test_what_odoo_does_not_have_is_left_out(self):
        body = self.build(self.invoice(due_date=None, customer_reference=None, delivery_date=None, bank=None))
        self.assertIs(body["electronic"], True)
        self.assertNotIn("paymentDueDate", body)
        self.assertNotIn("einvoice", body)

    def test_an_account_belongs_only_to_a_credit_transfer(self):
        from metanow_fiscalization.efis.payload import _einvoice_fields
        _, part, problems = _einvoice_fields(self.invoice(customer_reference=None, delivery_date=None), "COMPENSATION")
        self.assertEqual(problems, [])
        self.assertIsNone(part)

    def test_an_account_efis_would_refuse_is_refused_before_posting(self):
        with self.assertRaises(PayloadInvalid) as bad_iban:
            self.build(self.invoice(bank={"iban": "AL35202111090000000001234568"}))
        self.assertIn("IBAN", str(bad_iban.exception))
        with self.assertRaises(PayloadInvalid) as bad_bic:
            self.build(self.invoice(bank={"iban": self.IBAN, "bic": "SGS"}))
        self.assertIn("BIC", str(bad_bic.exception))

    def test_every_e_invoice_key_sent_is_a_property_efis_publishes(self):
        document = support.openapi()
        properties = _schema_properties(document, "SubmitDocumentRequest")
        body = self.build(self.invoice())
        self.assertLessEqual(set(body), properties["SubmitDocumentRequest"])
        self.assertLessEqual(set(body["einvoice"]), properties["EinvoicePart"])
        self.assertLessEqual(set(body["einvoice"]["payment"]), properties["EinvoicePaymentPart"])
        self.assertLessEqual(set(body["einvoice"]["delivery"]), properties["EinvoiceDeliveryPart"])


class BuyerKindTests(unittest.TestCase):
    def buyer(self, **overrides):
        buyer = dict(support.invoice()["buyer"])
        buyer.update(overrides)
        return support.invoice(buyer=buyer)

    def test_a_company_is_sent_as_a_business(self):
        body = build(self.buyer(kind="business", identifier=None), support.config(), support.profile())
        self.assertEqual(body["buyer"]["kind"], "business")

    def test_a_person_is_left_to_efis_default(self):
        body = build(self.buyer(kind=None), support.config(), support.profile())
        self.assertNotIn("kind", body["buyer"])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(self.buyer(kind="individual"), support.config(), support.profile())


class PayloadTests(unittest.TestCase):
    def test_a_posted_invoice_becomes_efis_request(self):
        body = build(support.invoice(), support.config(), support.profile())
        self.assertEqual(body["clientSystem"], "odoo")
        self.assertEqual(body["clientReference"], "INV/2026/00001")
        self.assertEqual(body["total"], "240.00")
        self.assertEqual(body["payments"], [{"method": "ACCOUNT", "amount": "240.00"}])
        self.assertEqual(body["lines"], [{
            "description": "Karrige",
            "unit": "copë",
            "quantity": "2",
            "unitPriceBeforeTax": "100",
            "unitPriceAfterTax": "120",
            "taxRate": "20",
            "itemCode": "K-1",
        }])
        self.assertEqual(body["buyer"]["identifier"], "L98765432K")
        self.assertTrue(body["issuerInVat"])
        self.assertNotIn("issuedAt", body, "efis stamps the issue time on arrival")
        self.assertNotIn("deviceCode", body, "only a cash invoice carries the device code")

    def test_every_key_sent_is_a_property_efis_publishes(self):
        document = support.openapi()
        properties = _schema_properties(document, "SubmitDocumentRequest")
        body = build(support.invoice(), support.config(), support.profile())
        self.assertLessEqual(set(body), properties["SubmitDocumentRequest"])
        self.assertLessEqual(set(body["seller"]), properties["PartyInput"])
        self.assertLessEqual(set(body["buyer"]), properties["PartyInput"])
        for item in body["lines"]:
            self.assertLessEqual(set(item), properties["LineInput"])
        for payment in body["payments"]:
            self.assertLessEqual(set(payment), properties["PaymentInput"])
        required = set(document["components"]["schemas"]["SubmitDocumentRequest"].get("required", []))
        self.assertLessEqual(required, set(body))

    def test_no_profile_means_no_posting(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(), support.config(), None)

    def test_vat_status_is_never_inferred(self):
        with self.assertRaises(PayloadInvalid) as raised:
            build(support.invoice(), support.config(issuer_in_vat=None), support.profile())
        self.assertIn("VAT", raised.exception.problems[0])

    def test_unknown_payment_method_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(payment_method="CHEQUE"), support.config(), support.profile())

    def test_missing_payment_method_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(payment_method=False), support.config(), support.profile())

    def test_cash_needs_the_device_code(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(payment_method="BANKNOTE"), support.config(), support.profile())
        body = build(support.invoice(payment_method="BANKNOTE"), support.config(device_code="cc123cc123"),
                     support.profile())
        self.assertEqual(body["deviceCode"], "cc123cc123")

    def test_foreign_currency_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(currency="EUR"), support.config(), support.profile())

    def test_a_lek_invoice_passes_though_the_profile_lists_no_lek(self):
        # Pipeline #5319: the real profile's currency list has no ALL, so the old check refused
        # every lek invoice. The payment carries no currency, which efis reads as the lek.
        payload = build(support.invoice(), support.config(), support.profile())
        self.assertNotIn("currency", payload["payments"][0])

    def test_a_company_keeping_books_in_another_currency_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(currency="EUR", company_currency="EUR"), support.config(), support.profile())

    def test_rate_not_in_the_profile_is_refused(self):
        taxed = support.line(price_total=230.0, taxes=[{"amount_type": "percent", "amount": 15.0, "exemption": False}])
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(lines=[taxed], amount_total=230.0, amount_tax=30.0), support.config(),
                  support.profile())

    def test_vat_on_an_issuer_outside_vat_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(), support.config(issuer_in_vat=False), support.profile())

    def test_totals_efis_would_compute_differently_are_refused(self):
        # Odoo rounding globally: three lines of 0.33 + 20% tax round to 1.19 per line in efis
        # (0.396 -> 0.40 each), while a global rounding books 1.19 for all three.
        item = support.line(quantity=1.0, price_subtotal=0.33, price_total=0.40)
        with self.assertRaises(PayloadInvalid) as raised:
            build(support.invoice(lines=[item, item, item], amount_untaxed=0.99, amount_tax=0.20, amount_total=1.19),
                  support.config(), support.profile())
        self.assertTrue(any("1.20" in problem for problem in raised.exception.problems))

    def test_line_untaxed_amount_must_match_efis_rule(self):
        item = support.line(quantity=1.0, price_subtotal=100.0, price_total=120.01)
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(lines=[item], amount_untaxed=100.0, amount_tax=20.01, amount_total=120.01),
                  support.config(), support.profile())

    def test_negative_quantity_is_refused(self):
        item = support.line(quantity=-1.0, price_subtotal=-100.0, price_total=-120.0)
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(lines=[item]), support.config(), support.profile())

    def test_exemption_must_be_in_the_profile(self):
        item = support.line(price_total=200.0, taxes=[{"amount_type": "percent", "amount": 0.0, "exemption": "type_9"}])
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(lines=[item], amount_total=200.0, amount_tax=0.0), support.config(), support.profile())
        item["taxes"][0]["exemption"] = "type_1"
        body = build(support.invoice(lines=[item], amount_total=200.0, amount_tax=0.0), support.config(),
                     support.profile())
        self.assertEqual(body["lines"][0]["exemptionReason"], "type_1")
        self.assertEqual(body["lines"][0]["taxRate"], "0")

    def test_identifier_type_and_country_come_from_the_profile(self):
        buyer = dict(support.invoice()["buyer"], identifier="AB1234567", identifier_type="passport",
                     country_code="ALB")
        body = build(support.invoice(buyer=buyer), support.config(), support.profile())
        self.assertEqual(body["buyer"]["identifierType"], "passport")
        self.assertEqual(body["buyer"]["countryCode"], "ALB")
        buyer["country_code"] = "AL"
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(buyer=buyer), support.config(), support.profile())

    def test_unpaired_company_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(), support.config(client_system=False), support.profile())


if __name__ == "__main__":
    unittest.main()
