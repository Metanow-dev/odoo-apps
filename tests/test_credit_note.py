"""DEV-2431: an Odoo credit note goes to efis as a correction of the invoice it reverses."""

import unittest
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis.payload import PayloadInvalid, build
from metanow_fiscalization.models.account_move import AccountMove
from metanow_fiscalization.models.account_move_reversal import AccountMoveReversal
from test_account_move import company, environment, move

ISSUED_AT = "2026-10-20T10:15:00+02:00"


def original(env, owner, **overrides):
    values = dict(
        id=41,
        name="INV/2026/00001",
        metanow_fiscal_state=outcome.REGISTERED,
        metanow_fiscal_security_code=support.SECURITY_CODE,
        metanow_fiscal_issued_at=ISSUED_AT,
        metanow_fiscal_electronic=False,
    )
    values.update(overrides)
    return AccountMove(env, **values)


def credit_note(env, owner=None, kind="credit", reversed_from=None, **overrides):
    owner = owner or company(env)
    values = dict(
        name="RINV/2026/00001",
        move_type="out_refund",
        reversed_entry_id=reversed_from if reversed_from is not None else original(env, owner),
        metanow_fiscal_correction_kind=kind,
    )
    values.update(overrides)
    return move(env, owner, **values)


def corrects(**overrides):
    values = {"security_code": support.SECURITY_CODE, "issued_at": ISSUED_AT, "kind": "credit", "electronic": False}
    values.update(overrides)
    return values


class PayloadTests(unittest.TestCase):
    def test_a_credit_note_is_negative_through_the_quantity_never_the_price(self):
        body = build(support.invoice(corrects=corrects()), support.config(), support.profile())
        line = body["lines"][0]
        self.assertEqual(line["quantity"], "-2")
        self.assertEqual((line["unitPriceBeforeTax"], line["unitPriceAfterTax"]), ("100", "120"))
        self.assertEqual(body["total"], "-240.00")
        self.assertEqual(body["payments"], [{"method": "ACCOUNT", "amount": "-240.00"}])

    def test_it_references_the_original_with_the_chosen_kind(self):
        for kind in ("corrective", "credit"):
            body = build(support.invoice(corrects=corrects(kind=kind)), support.config(), support.profile())
            self.assertEqual(body["corrects"], {"securityCode": support.SECURITY_CODE, "issuedAt": ISSUED_AT, "kind": kind})

    def test_a_kind_must_be_chosen(self):
        for kind in (None, False, "", "debit"):
            with self.assertRaises(PayloadInvalid, msg=repr(kind)):
                build(support.invoice(corrects=corrects(kind=kind)), support.config(), support.profile())

    def test_an_original_without_codes_is_refused(self):
        with self.assertRaises(PayloadInvalid):
            build(support.invoice(corrects=corrects(security_code=None)), support.config(), support.profile())

    def test_a_partial_credit_note_carries_only_its_own_lines(self):
        invoice = support.invoice(
            corrects=corrects(), amount_total=120.0, amount_untaxed=100.0, amount_tax=20.0,
            lines=[support.line(quantity=1.0, price_subtotal=100.0, price_total=120.0)])
        body = build(invoice, support.config(), support.profile())
        self.assertEqual(body["lines"][0]["quantity"], "-1")
        self.assertEqual(body["total"], "-120.00")

    def test_odoo_negative_lines_are_still_refused(self):
        invoice = support.invoice(corrects=corrects(), lines=[support.line(quantity=-2.0)])
        with self.assertRaises(PayloadInvalid):
            build(invoice, support.config(), support.profile())

    def test_an_e_invoice_is_credited_as_an_e_invoice(self):
        body = build(support.invoice(corrects=corrects(electronic=True)), support.config(einvoice=True), support.profile())
        self.assertTrue(body["electronic"])

    def test_a_plain_invoice_is_credited_as_a_plain_document_whatever_the_buyer(self):
        # The buyer has a tax number and the company has e-invoices on, so an invoice would be an
        # e-invoice; this credit note follows its original instead.
        body = build(support.invoice(corrects=corrects(electronic=False)), support.config(einvoice=True), support.profile())
        self.assertNotIn("electronic", body)

    def test_an_e_invoice_cannot_be_credited_in_cash(self):
        invoice = support.invoice(corrects=corrects(electronic=True), payment_method="BANKNOTE")
        with self.assertRaises(PayloadInvalid) as raised:
            build(invoice, support.config(einvoice=True, device_code="dd123dd123"), support.profile())
        self.assertIn("non-cash journal", str(raised.exception))

    def test_an_invoice_carries_no_reference(self):
        self.assertNotIn("corrects", build(support.invoice(), support.config(), support.profile()))

    def test_the_reference_is_in_efis_published_shape(self):
        schemas = support.openapi()["components"]["schemas"]
        self.assertIn("corrects", schemas["SubmitDocumentRequest"]["properties"])
        self.assertEqual(set(schemas["CorrectionInput"]["properties"]), {"securityCode", "issuedAt", "kind"})


class PostingTests(unittest.TestCase):
    def send(self, record, *answers):
        opener = support.FakeOpener(*answers or ((202, support.document_response()),))
        owner = record.company_id
        owner._metanow_fiscal_client = lambda authenticated=True: efis_client.EfisClient(
            owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        record._metanow_fiscal_send()
        return opener

    def test_a_credit_note_is_queued_and_sent_as_a_correction(self):
        env = environment()
        record = credit_note(env, kind="corrective")
        record._post()
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)
        request = self.send(record).requests[0]
        self.assertEqual((request["method"], request["url"]), ("POST", "https://efis.example.invalid/v1/documents"))
        self.assertEqual(request["body"]["corrects"],
                         {"securityCode": support.SECURITY_CODE, "issuedAt": ISSUED_AT, "kind": "corrective"})
        self.assertEqual(request["body"]["clientReference"], "RINV/2026/00001")
        self.assertEqual(record.metanow_fiscal_security_code, support.SECURITY_CODE, "its own codes are stored")

    def test_no_kind_is_refused_before_posting(self):
        env = environment()
        record = credit_note(env, kind=False)
        with self.assertRaises(fake_odoo.UserError) as raised:
            record._post()
        self.assertIn("return or discount", str(raised.exception))
        self.assertNotIn("_post", record.base_calls)

    def test_an_original_efis_has_not_registered_is_refused_before_posting(self):
        for state in (False, outcome.TO_SEND, outcome.SENT, outcome.DEFERRED, outcome.REJECTED, outcome.REFUSED):
            env = environment()
            owner = company(env)
            record = credit_note(env, owner, reversed_from=original(env, owner, metanow_fiscal_state=state))
            with self.assertRaises(fake_odoo.UserError, msg=state):
                record._post()
            self.assertNotIn("_post", record.base_calls)

    def test_a_superseded_original_may_be_credited_again(self):
        env = environment()
        owner = company(env)
        record = credit_note(env, owner, reversed_from=original(env, owner, metanow_fiscal_state=outcome.SUPERSEDED))
        record._post()
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)

    def test_the_customers_bank_account_is_never_sent_as_an_account_to_pay_into(self):
        env = environment()
        owner = company(env, metanow_fiscal_einvoice=True)
        record = credit_note(env, owner, reversed_from=original(env, owner, metanow_fiscal_electronic=True),
                             partner_bank_id=SimpleNamespace(acc_type="iban", acc_number="AL35202111090000000001234567",
                                                             bank_id=False, acc_holder_name="Blerës"))
        record._post()
        body = self.send(record).requests[0]["body"]
        self.assertTrue(body["electronic"])
        self.assertNotIn("payment", body.get("einvoice") or {})

    def test_nothing_happens_when_fiscalisation_is_off(self):
        env = environment()
        record = credit_note(env, company(env, metanow_fiscal_enabled=False), reversed_from=False)
        record._post()
        self.assertEqual(record.writes, [])

    def test_the_kind_is_never_changed_once_sent(self):
        env = environment()
        record = credit_note(env)
        record._post()
        self.send(record)
        with self.assertRaises(fake_odoo.UserError):
            record.write({"metanow_fiscal_correction_kind": "corrective"})
        record.write({"metanow_fiscal_correction_kind": "credit"})

    def test_the_kind_may_change_while_in_draft(self):
        env = environment()
        record = credit_note(env)
        record.write({"metanow_fiscal_correction_kind": "corrective"})
        self.assertEqual(record.metanow_fiscal_correction_kind, "corrective")


class ReversalWizardTests(unittest.TestCase):
    def test_the_chosen_kind_reaches_a_credit_note_of_an_invoice(self):
        env = environment()
        wizard = AccountMoveReversal(env, metanow_fiscal_correction_kind="corrective")
        values = wizard._prepare_default_reversal(SimpleNamespace(move_type="out_invoice"))
        self.assertEqual(values["metanow_fiscal_correction_kind"], "corrective")

    def test_nothing_is_set_for_other_entries_or_without_a_choice(self):
        env = environment()
        wizard = AccountMoveReversal(env, metanow_fiscal_correction_kind="credit")
        self.assertNotIn("metanow_fiscal_correction_kind",
                         wizard._prepare_default_reversal(SimpleNamespace(move_type="in_invoice")))
        self.assertNotIn("metanow_fiscal_correction_kind",
                         AccountMoveReversal(env)._prepare_default_reversal(SimpleNamespace(move_type="out_invoice")))


if __name__ == "__main__":
    unittest.main()
