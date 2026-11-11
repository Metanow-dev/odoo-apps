"""DEV-2434: Instruction 24 records for the cash payments on an e-invoice."""

import json
import unittest
from datetime import date
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis import payment_records
from test_account_move import environment
from test_payments import DOCUMENT, invoice, partial, payment_entry

REGISTRATION = "d9b0b4a1-0000-4000-8000-000000000001"


def cash(name="Arka"):
    return SimpleNamespace(name=name, type="cash", metanow_fiscal_payment_method="BANKNOTE")


def bank(name="Banka"):
    return SimpleNamespace(name=name, type="bank", metanow_fiscal_payment_method="ACCOUNT")


def record(record_id, amount="120.00", paid_on="2026-11-10", due_by="2026-12-10", filed=None, cause="bank_not_reported",
           document=DOCUMENT):
    return {"id": record_id, "documentId": document, "buyerTaxId": "L98765432K", "providerTaxId": None,
            "registrationCode": REGISTRATION, "securityCode": support.SECURITY_CODE, "amount": amount,
            "paidOn": paid_on, "cause": cause, "dueBy": due_by, "recordedBy": "odoo",
            "recordedAt": "2026-11-10T10:00:00Z", "filedAt": filed, "filedReference": "PL-1" if filed else None}


def einvoice(env, partials=(), **overrides):
    values = dict(
        metanow_fiscal_electronic=True,
        metanow_fiscal_registration_code=REGISTRATION,
        metanow_fiscal_security_code=support.SECURITY_CODE,
        metanow_fiscal_payment_records_pending=True,
        metanow_fiscal_payments_pending=False,
    )
    values.update(overrides)
    return invoice(env, partials=partials, **values)


class PlanTests(unittest.TestCase):
    def test_a_new_cash_payment_is_recorded(self):
        create, extra = payment_records.plan([{"amount": "120.00", "paidOn": "2026-11-10"}], [])
        self.assertEqual((create, extra), ([{"amount": "120.00", "paidOn": "2026-11-10"}], []))

    def test_a_record_efis_holds_is_not_made_again(self):
        self.assertEqual(payment_records.plan([{"amount": "120.00", "paidOn": "2026-11-10"}], [record("r-1")]), ([], []))

    def test_two_equal_payments_need_two_records(self):
        wanted = [{"amount": "50.00", "paidOn": "2026-11-10"}] * 2
        create, extra = payment_records.plan(wanted, [record("r-1", amount="50.00")])
        self.assertEqual((len(create), extra), (1, []))

    def test_a_record_odoo_no_longer_calls_for_is_named_not_taken_back(self):
        create, extra = payment_records.plan([], [record("r-1")])
        self.assertEqual(create, [])
        self.assertEqual([held["id"] for held in extra], ["r-1"])

    def test_the_body_is_efis_published_shape(self):
        schema = support.openapi()["components"]["schemas"]["PaymentRecordRequest"]
        self.assertEqual(set(schema["properties"]), {
            "documentId", "buyerTaxId", "providerTaxId", "registrationCode", "securityCode", "amount", "paidOn", "cause"})


class SyncTests(unittest.TestCase):
    def run_pass(self, move, *answers):
        opener = support.FakeOpener(*answers)
        owner = move.company_id
        owner._metanow_fiscal_client = lambda authenticated=True: efis_client.EfisClient(
            owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        move._metanow_fiscal_sync_payment_records()
        return opener

    def paid_in_cash(self, env, **overrides):
        return einvoice(env, partials=[partial(7, 120.0, payment_entry(bank=cash()))], **overrides)

    def test_a_cash_payment_on_an_e_invoice_makes_its_record(self):
        env = environment()
        move = self.paid_in_cash(env)
        opener = self.run_pass(move, (200, {"records": []}), (201, record("r-1")))
        listing, created = opener.requests
        self.assertEqual(listing["method"], "GET")
        self.assertEqual(listing["url"],
                         "https://efis.example.invalid/v1/einvoices/payment-records?dueFrom=2026-11-10&dueTo=2026-12-25")
        self.assertEqual(created["body"], {
            "documentId": DOCUMENT, "buyerTaxId": "L98765432K", "providerTaxId": None,
            "registrationCode": REGISTRATION, "securityCode": support.SECURITY_CODE,
            "amount": "120.00", "paidOn": "2026-11-10", "cause": "bank_not_reported"})
        self.assertEqual(move.metanow_fiscal_payment_record_due, "2026-12-10")
        self.assertFalse(move.metanow_fiscal_payment_records_pending)
        self.assertIn("not filed", _summary(move))

    def test_a_lost_answer_never_makes_a_second_record(self):
        env = environment()
        move = self.paid_in_cash(env)
        self.run_pass(move, (200, {"records": []}), OSError("timed out"))
        self.assertTrue(move.metanow_fiscal_payment_records_pending, "tried again on the next pass")
        opener = self.run_pass(move, (200, {"records": [record("r-1")]}))
        self.assertEqual(len(opener.requests), 1, "efis had it; nothing is made again")

    def test_a_payment_through_a_bank_needs_no_record(self):
        env = environment()
        move = einvoice(env, partials=[partial(7, 120.0, payment_entry(bank=bank()))])
        self.assertEqual(self.run_pass(move).requests, [])
        self.assertFalse(move.metanow_fiscal_payment_records_pending)

    def test_a_plain_fiscal_invoice_needs_no_record(self):
        env = environment()
        move = self.paid_in_cash(env, metanow_fiscal_electronic=False)
        self.assertEqual(self.run_pass(move).requests, [])
        self.assertFalse(move.metanow_fiscal_payment_records_pending)

    def test_an_e_invoice_not_registered_yet_waits(self):
        env = environment()
        move = self.paid_in_cash(env, metanow_fiscal_document_id=False, metanow_fiscal_registration_code=False,
                                 metanow_fiscal_state=outcome.TO_SEND)
        self.assertEqual(self.run_pass(move).requests, [])
        self.assertTrue(move.metanow_fiscal_payment_records_pending)

    def test_only_this_invoices_records_with_this_cause_count(self):
        env = environment()
        move = self.paid_in_cash(env)
        others = [record("r-9", document="another"), record("r-8", cause="compensation")]
        opener = self.run_pass(move, (200, {"records": others}), (201, record("r-1")))
        self.assertEqual(opener.requests[1]["method"], "POST")

    def test_a_filed_record_is_no_longer_due(self):
        env = environment()
        move = self.paid_in_cash(env, metanow_fiscal_payment_records_pending=False,
                                 metanow_fiscal_payment_record_due="2026-12-10",
                                 metanow_fiscal_payment_records=json.dumps([record("r-1")]))
        self.run_pass(move, (200, {"records": [record("r-1", filed="2026-12-01T09:00:00Z")]}))
        self.assertFalse(move.metanow_fiscal_payment_record_due)
        self.assertIn("filed PL-1", _summary(move))

    def test_a_cancelled_cash_payment_leaves_a_record_not_to_file(self):
        env = environment()
        move = einvoice(env, metanow_fiscal_payment_records=json.dumps([record("r-1")]))
        opener = self.run_pass(move, (200, {"records": [record("r-1")]}))
        self.assertEqual(len(opener.requests), 1, "efis has no way to take it back; nothing else is sent")
        self.assertIn("do not file", move.metanow_fiscal_payment_records_note)
        self.assertFalse(move.metanow_fiscal_payment_record_due, "it is not counted as one to file")

    def test_a_filed_record_whose_payment_was_cancelled_must_be_put_right(self):
        env = environment()
        filed = record("r-1", filed="2026-12-01T09:00:00Z")
        move = einvoice(env, metanow_fiscal_payment_records=json.dumps([filed]))
        self.run_pass(move, (200, {"records": [filed]}))
        self.assertIn("put them right on the platform", move.metanow_fiscal_payment_records_note)
        self.assertIn("PL-1", move.metanow_fiscal_payment_records_note)
        self.assertNotIn("do not file", move.metanow_fiscal_payment_records_note)

    def test_a_refusal_is_never_retried(self):
        env = environment()
        move = self.paid_in_cash(env)
        self.run_pass(move, (200, {"records": []}), (422, {"code": "invalid_payment_record"}))
        self.assertFalse(move.metanow_fiscal_payment_records_pending)
        self.assertEqual(move.metanow_fiscal_payment_records_note, "efis: invalid_payment_record")

    def test_efis_trouble_is_retried(self):
        env = environment()
        move = self.paid_in_cash(env)
        self.run_pass(move, (503, None))
        self.assertTrue(move.metanow_fiscal_payment_records_pending)

    def test_a_buyer_without_a_tax_number_stops_before_efis(self):
        env = environment()
        move = self.paid_in_cash(env)
        move.commercial_partner_id = SimpleNamespace(vat=False)
        self.assertEqual(self.run_pass(move).requests, [])
        self.assertIn("no tax number", move.metanow_fiscal_payment_records_note)

    def test_nothing_is_read_for_an_invoice_with_no_cash_payment_and_no_record(self):
        env = environment()
        move = einvoice(env)
        self.assertEqual(self.run_pass(move).requests, [])
        self.assertFalse(move.metanow_fiscal_payment_records_pending)

    def test_a_record_is_kept_apart_from_the_receivables(self):
        # A refused receivable never stops the record the law asks for.
        env = environment()
        move = self.paid_in_cash(env, metanow_fiscal_payments_pending=False,
                                 metanow_fiscal_payments_error="efis: payment_exceeds_outstanding")
        opener = self.run_pass(move, (200, {"records": []}), (201, record("r-1")))
        self.assertEqual(len(opener.requests), 2)


def _summary(move):
    move._compute_metanow_fiscal_payment_records_summary()
    return move.metanow_fiscal_payment_records_summary or ""


class MarkTests(unittest.TestCase):
    def test_marking_and_sending_again_cover_the_records(self):
        env = environment()
        move = einvoice(env, metanow_fiscal_payment_records_pending=False)
        move.action_metanow_fiscal_send_payments()
        self.assertTrue(move.metanow_fiscal_payment_records_pending)

    def test_the_filter_lists_records_to_file(self):
        view = (support.MODULE_DIR / "views" / "account_move_views.xml").read_text(encoding="utf-8")
        self.assertIn("('metanow_fiscal_payment_record_due', '!=', False)", view)


if __name__ == "__main__":
    unittest.main()
