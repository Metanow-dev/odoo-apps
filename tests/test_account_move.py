"""The model layer against a mocked ORM boundary and a mocked HTTP client."""

import unittest
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis.client import EfisClient
from metanow_fiscalization.models.account_move import AccountMove
from metanow_fiscalization.models.res_company import ResCompany

FISCAL_FIELDS = {
    outcome.DOCUMENT_ID, outcome.STATE, outcome.EFIS_STATE, outcome.FISCAL_NUMBER,
    outcome.SECURITY_CODE, outcome.VERIFICATION_URL, outcome.ERROR,
}


def company(env, opener=None, **overrides):
    values = dict(
        name="Shembull sh.p.k.",
        vat="K12345678L",
        street="Rruga e Durrësit 1",
        street2=False,
        city="Tiranë",
        country_id=SimpleNamespace(code="AL", metanow_fiscal_country_code=False),
        currency_id=SimpleNamespace(name="ALL"),
        metanow_fiscal_enabled=True,
        metanow_fiscal_api_url="https://efis.example.invalid",
        metanow_fiscal_credential="c-1",
        metanow_fiscal_tenant="shembull",
        metanow_fiscal_channel="odoo",
        metanow_fiscal_vat_status="in_vat",
        metanow_fiscal_business_unit_code="bb123bb123",
        metanow_fiscal_operator_code="oo123oo123",
        metanow_fiscal_software_code="ss123ss123",
        metanow_fiscal_profile=support.profile().to_json(),
    )
    values.update(overrides)
    record = ResCompany(env, **values)
    if opener is not None:
        record._metanow_fiscal_client = lambda authenticated=True: EfisClient(
            record.metanow_fiscal_api_url,
            credential=record.metanow_fiscal_credential if authenticated else None,
            tenant=record.metanow_fiscal_tenant if authenticated else None,
            opener=opener,
        )
    return record


def invoice_line(**overrides):
    values = dict(
        display_type="product",
        name="Karrige",
        product_id=SimpleNamespace(display_name="Karrige", default_code="K-1"),
        product_uom_id=SimpleNamespace(name="copë"),
        quantity=2.0,
        price_subtotal=200.0,
        price_total=240.0,
        tax_ids=[SimpleNamespace(amount_type="percent", amount=20.0, metanow_fiscal_exemption=False)],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def move(env, owner, **overrides):
    values = dict(
        id=42,
        name="INV/2026/00001",
        move_type="out_invoice",
        company_id=owner,
        currency_id=SimpleNamespace(name="ALL"),
        journal_id=SimpleNamespace(metanow_fiscal_payment_method="ACCOUNT"),
        commercial_partner_id=SimpleNamespace(
            name="Blerës sh.p.k.", vat="L98765432K", metanow_fiscal_identifier_type=False,
            street="Rruga 2", street2=False, city="Durrës", country_id=False),
        invoice_line_ids=[invoice_line(), invoice_line(display_type="line_note", name="Shënim")],
        amount_total=240.0,
        amount_untaxed=200.0,
        amount_tax=40.0,
    )
    values.update(overrides)
    return AccountMove(env, **values)


def environment():
    env = fake_odoo.Env()
    env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_send"] = fake_odoo.Trigger()
    env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_poll"] = fake_odoo.Trigger()
    return env


class PostingTests(unittest.TestCase):
    def test_posting_validates_then_queues_with_a_stable_key(self):
        env = environment()
        record = move(env, company(env))
        record._post()
        self.assertEqual(record.base_calls[0], "_post")
        self.assertEqual(len(record.writes), 1)
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)
        self.assertEqual(record.metanow_fiscal_idempotency_key, "odoo-db-0001-42-1")
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_send"].count, 1)

    def test_an_invoice_efis_would_refuse_is_not_posted(self):
        env = environment()
        record = move(env, company(env, metanow_fiscal_vat_status=False))
        with self.assertRaises(fake_odoo.UserError):
            record._post()
        self.assertEqual(record.writes, [])

    def test_a_credit_note_is_refused_while_fiscalisation_is_on(self):
        env = environment()
        record = move(env, company(env), move_type="out_refund")
        with self.assertRaises(fake_odoo.UserError):
            record._post()
        self.assertNotIn("_post", record.base_calls)

    def test_nothing_happens_when_fiscalisation_is_off(self):
        env = environment()
        record = move(env, company(env, metanow_fiscal_enabled=False))
        record._post()
        self.assertEqual(record.writes, [])

    def test_vendor_bills_are_left_alone(self):
        env = environment()
        record = move(env, company(env), move_type="in_invoice")
        record._post()
        self.assertEqual(record.writes, [])


class SendTests(unittest.TestCase):
    def queued(self, *answers):
        env = environment()
        opener = support.FakeOpener(*answers)
        record = move(env, company(env, opener=opener))
        record._post()
        record.writes.clear()
        return record, opener

    def test_one_write_stores_every_fiscal_field(self):
        record, opener = self.queued((202, support.document_response()))
        record._metanow_fiscal_send()
        self.assertEqual(len(record.writes), 1, record.writes)
        self.assertEqual(set(record.writes[0]), FISCAL_FIELDS)
        self.assertEqual(record.metanow_fiscal_security_code, support.SECURITY_CODE)
        self.assertEqual(record.metanow_fiscal_state, outcome.SENT)
        sent = opener.requests[0]
        self.assertEqual(sent["headers"]["idempotency-key"], "odoo-db-0001-42-1")
        self.assertEqual(sent["body"]["clientReference"], "INV/2026/00001")
        self.assertEqual(len(sent["body"]["lines"]), 1, "a note line is not an invoice line")

    def test_a_refusal_is_parked_once_and_not_resent(self):
        record, opener = self.queued((422, {"code": "buyer_tax_id_unknown", "retryable": False, "authorityCode": None}))
        record._metanow_fiscal_send()
        record._metanow_fiscal_send()
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(record.metanow_fiscal_state, outcome.REFUSED)
        self.assertEqual(len(record.writes), 1)

    def test_no_answer_keeps_it_queued_with_the_same_key_and_body(self):
        import urllib.error
        record, opener = self.queued(urllib.error.URLError("down"), (202, support.document_response()))
        record._metanow_fiscal_send()
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)
        record._metanow_fiscal_send()
        first, second = opener.requests
        self.assertEqual(first["headers"]["idempotency-key"], second["headers"]["idempotency-key"])
        self.assertEqual(first["body"], second["body"])
        self.assertEqual(record.metanow_fiscal_state, outcome.SENT)

    def test_a_locked_row_is_skipped(self):
        record, opener = self.queued()
        record.env.cr.locked.add(record.id)
        record._metanow_fiscal_send()
        self.assertEqual(opener.requests, [])
        self.assertEqual(record.writes, [])

    def test_a_conflicting_security_code_is_never_written(self):
        record, _ = self.queued((200, support.document_response(securityCode="f" * 32)))
        record.metanow_fiscal_state = outcome.SENT
        record.metanow_fiscal_document_id = support.document_response()["id"]
        record.metanow_fiscal_security_code = support.SECURITY_CODE
        record._metanow_fiscal_poll()
        self.assertEqual(record.metanow_fiscal_security_code, support.SECURITY_CODE)
        self.assertEqual(record.metanow_fiscal_state, outcome.PARKED)
        self.assertEqual(len(record.writes), 1)

    def test_the_poll_writes_the_registration_in_one_call(self):
        record, _ = self.queued(
            (202, support.document_response()),
            (200, support.document_response(state="accepted", registrationCode="d9b0b4a1-0000-4000-8000-000000000001",
                                            issuedAt="2026-10-20T10:15:00+02:00")))
        record._metanow_fiscal_send()
        record.writes.clear()
        record._metanow_fiscal_poll()
        self.assertEqual(len(record.writes), 1)
        self.assertEqual(record.metanow_fiscal_state, outcome.REGISTERED)
        self.assertEqual(record.metanow_fiscal_registration_code, "d9b0b4a1-0000-4000-8000-000000000001")
        self.assertIn("barLevel=M", _qr(record))


def _qr(record):
    record._compute_metanow_fiscal_qr_src()
    return record.metanow_fiscal_qr_src


class GuardTests(unittest.TestCase):
    def sent(self):
        env = environment()
        record = move(env, company(env), metanow_fiscal_state=outcome.SENT,
                      metanow_fiscal_document_id="7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88",
                      metanow_fiscal_security_code=support.SECURITY_CODE)
        return record

    def test_a_sent_invoice_never_goes_back_to_draft(self):
        record = self.sent()
        for action in (record.button_draft, record.button_cancel, record.unlink):
            with self.assertRaises(fake_odoo.UserError):
                action()
        self.assertEqual(record.base_calls, [])

    def test_a_queued_invoice_never_goes_back_to_draft(self):
        env = environment()
        record = move(env, company(env), metanow_fiscal_state=outcome.TO_SEND)
        with self.assertRaises(fake_odoo.UserError):
            record.button_draft()

    def test_a_refused_invoice_may_be_corrected_in_draft(self):
        env = environment()
        record = move(env, company(env), metanow_fiscal_state=outcome.REFUSED, metanow_fiscal_attempt=1)
        record.button_draft()
        self.assertEqual(record.base_calls, ["button_draft"])
        record._post()
        self.assertEqual(record.metanow_fiscal_idempotency_key, "odoo-db-0001-42-2",
                         "a corrected invoice is a new request, so it has a new key")

    def test_the_security_code_cannot_be_overwritten(self):
        record = self.sent()
        with self.assertRaises(fake_odoo.UserError):
            record.write({outcome.SECURITY_CODE: "f" * 32})
        record.write({outcome.SECURITY_CODE: support.SECURITY_CODE})

    def test_retry_resends_a_parked_invoice_under_its_original_key(self):
        env = environment()
        record = move(env, company(env), metanow_fiscal_state=outcome.PARKED,
                      metanow_fiscal_idempotency_key="odoo-db-0001-42-1")
        record.action_metanow_fiscal_retry()
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)
        self.assertEqual(record.metanow_fiscal_idempotency_key, "odoo-db-0001-42-1")
        record.metanow_fiscal_state = outcome.PARKED
        record.metanow_fiscal_document_id = "7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88"
        record.action_metanow_fiscal_retry()
        self.assertEqual(record.metanow_fiscal_state, outcome.SENT, "a numbered document is read, not resent")


class PairingTests(unittest.TestCase):
    def test_pairing_stores_the_credential_in_one_write_and_fetches_the_profile(self):
        env = environment()
        opener = support.FakeOpener(
            (200, {"credential": "c-9", "tenantId": "shembull", "channel": "odoo"}),
            (200, support.PROFILE_DOCUMENT))
        record = company(env, opener=opener, metanow_fiscal_credential=False, metanow_fiscal_tenant=False,
                         metanow_fiscal_channel=False, metanow_fiscal_profile=False)
        record._metanow_fiscal_pair(" 123-456 ")
        self.assertEqual(record.writes[0], {
            "metanow_fiscal_credential": "c-9",
            "metanow_fiscal_tenant": "shembull",
            "metanow_fiscal_channel": "odoo",
        })
        self.assertEqual(opener.requests[1]["url"], "https://efis.example.invalid/v1/profiles/AL")
        self.assertIsNotNone(record._metanow_fiscal_profile())

    def test_a_refused_code_stores_nothing(self):
        env = environment()
        opener = support.FakeOpener((422, {"code": "pairing_code_invalid", "retryable": False, "authorityCode": None}))
        record = company(env, opener=opener)
        with self.assertRaises(fake_odoo.UserError):
            record._metanow_fiscal_pair("000")
        self.assertEqual(record.writes, [])


if __name__ == "__main__":
    unittest.main()
