"""The model layer against a mocked ORM boundary and a mocked HTTP client."""

import unittest
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis.client import EfisClient
from metanow_fiscalization.models.account_move import AccountMove
from metanow_fiscalization.models.res_company import ResCompany
from metanow_fiscalization.models.res_config_settings import ResConfigSettings
from metanow_fiscalization.models.res_users import ResUsers

FISCAL_FIELDS = {
    outcome.DOCUMENT_ID, outcome.STATE, outcome.EFIS_STATE, outcome.FISCAL_NUMBER,
    outcome.SECURITY_CODE, outcome.VERIFICATION_URL, outcome.ERROR, outcome.ELECTRONIC,
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
        journal_id=SimpleNamespace(metanow_fiscal_payment_method="ACCOUNT", metanow_fiscal_device_code=False),
        commercial_partner_id=SimpleNamespace(
            name="Blerës sh.p.k.", vat="L98765432K", metanow_fiscal_identifier_type=False, is_company=True,
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
    # The person posting, with no operator code of their own unless a test gives one.
    env.user = ResUsers(env, id=2)
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
        self.assertEqual(sent["body"]["buyer"]["kind"], "business", "a company partner is a business")

    def test_a_person_partner_carries_no_kind(self):
        env = environment()
        opener = support.FakeOpener((202, support.document_response()))
        buyer = SimpleNamespace(
            name="Arben Hoxha", vat=False, metanow_fiscal_identifier_type=False, is_company=False,
            street="Rruga 3", street2=False, city="Tiranë", country_id=False)
        record = move(env, company(env, opener=opener), commercial_partner_id=buyer)
        record._post()
        record._metanow_fiscal_send()
        self.assertNotIn("kind", opener.requests[0]["body"]["buyer"])

    def test_the_poll_stores_efis_warnings_on_the_invoice(self):
        record, _ = self.queued(
            (202, support.document_response()),
            (200, support.document_response(warnings=["buyer_identified_by_passport"])))
        record._metanow_fiscal_send()
        record.writes.clear()
        record._metanow_fiscal_poll()
        self.assertEqual(len(record.writes), 1)
        self.assertEqual(record.metanow_fiscal_warnings, "buyer_identified_by_passport")

    def test_a_refusal_is_parked_once_and_not_resent(self):
        record, opener = self.queued((422, {"code": "buyer_tax_id_unknown", "retryable": False, "authorityCode": None}))
        record._metanow_fiscal_send()
        record._metanow_fiscal_send()
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(record.metanow_fiscal_state, outcome.REFUSED)
        self.assertEqual(len(record.writes), 1)

    def test_a_refusal_before_numbering_returns_to_draft_with_efis_message(self):
        refusals = {
            "buyer_personal_id_required": (
                "An export to an individual names buyer.identifier with buyer.identifierType personal_id, "
                "foreign_tax or passport (Law 87/2019 Art. 9(2))."),
            "cash_over_ceiling": "A cash invoice between two taxpayers is at most 100000.00 ALL.",
        }
        for code, detail in refusals.items():
            with self.subTest(code=code):
                record, opener = self.queued((422, {
                    "code": code, "detail": detail, "retryable": False, "authorityCode": None,
                    "correlationId": "c0ffee"}))
                record._metanow_fiscal_send()
                self.assertEqual(record.metanow_fiscal_state, outcome.REFUSED)
                self.assertIn("button_draft", record.base_calls, "the invoice goes back to draft to be corrected")
                self.assertIn(detail, record.metanow_fiscal_error, "efis's message is shown in full")
                self.assertIn(code, record.metanow_fiscal_error, "the code is shown for support")
                self.assertEqual(len(record.writes), 1, "the refusal is one write")
                record._metanow_fiscal_send()
                self.assertEqual(len(opener.requests), 1, "a refusal is a client fault: never retried")

    def test_odoo_refusing_the_draft_leaves_it_refused_with_both_messages(self):
        record, opener = self.queued((422, {
            "code": "cash_over_ceiling", "detail": "Over the ceiling.", "retryable": False, "authorityCode": None}))
        record.draft_refusal = "The lock date is after this invoice."
        record._metanow_fiscal_send()
        self.assertEqual(record.metanow_fiscal_state, outcome.REFUSED)
        self.assertNotIn("button_draft", record.base_calls)
        self.assertIn("Over the ceiling.", record.metanow_fiscal_error)
        self.assertIn("The lock date is after this invoice.", record.metanow_fiscal_error)
        self.assertIn(("ROLLBACK TO SAVEPOINT", None), record.env.cr.statements)
        self.assertEqual(len(opener.requests), 1)

    def test_a_refusal_that_is_not_before_numbering_stays_posted(self):
        record, _ = self.queued((409, {"code": "conflict", "retryable": False, "authorityCode": None}))
        record._metanow_fiscal_send()
        self.assertEqual(record.metanow_fiscal_state, outcome.REFUSED)
        self.assertNotIn("button_draft", record.base_calls)

    def test_a_corrected_invoice_is_posted_as_a_new_request(self):
        record, opener = self.queued(
            (422, {"code": "cash_over_ceiling", "detail": "Over.", "retryable": False, "authorityCode": None}),
            (202, support.document_response()))
        record._metanow_fiscal_send()
        record._post()
        self.assertEqual(record.metanow_fiscal_state, outcome.TO_SEND)
        self.assertIs(record.metanow_fiscal_error, False, "the old refusal is cleared on posting again")
        record._metanow_fiscal_send()
        first, second = opener.requests
        self.assertNotEqual(first["headers"]["idempotency-key"], second["headers"]["idempotency-key"])

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


class EinvoiceSendTests(unittest.TestCase):
    EIC = "5e1a0c3d-0000-4000-8000-00000000e1c0"

    def sent(self, *answers):
        env = environment()
        opener = support.FakeOpener((202, support.document_response()), *answers)
        record = move(env, company(env, opener=opener, metanow_fiscal_einvoice=True))
        record._post()
        record._metanow_fiscal_send()
        record.writes.clear()
        return record, opener

    def test_an_e_invoice_is_flagged_in_the_request_and_on_the_invoice(self):
        record, opener = self.sent()
        self.assertIs(opener.requests[0]["body"]["electronic"], True)
        self.assertIs(record.metanow_fiscal_electronic, True)

    def test_off_in_the_company_sends_a_plain_fiscal_invoice(self):
        env = environment()
        opener = support.FakeOpener((202, support.document_response()))
        record = move(env, company(env, opener=opener, metanow_fiscal_einvoice=False))
        record._post()
        record._metanow_fiscal_send()
        self.assertNotIn("electronic", opener.requests[0]["body"])
        self.assertIs(record.metanow_fiscal_electronic, False)

    def test_the_e_invoice_is_read_once_registered_in_the_same_write(self):
        registered = support.document_response(state="accepted", registrationCode="d9b0b4a1-0000-4000-8000-000000000001")
        answer = {"id": "a1", "documentId": registered["id"], "kind": "invoice", "state": "registered",
                  "eic": self.EIC, "faultCode": None, "faultText": None, "warnings": [], "reconciled": False,
                  "authorityStatus": None, "statusSeenAt": None, "createdAt": "2026-11-09T10:00:00+01:00",
                  "answeredAt": None}
        record, opener = self.sent((200, registered), (200, answer))
        record._metanow_fiscal_poll()
        self.assertTrue(opener.requests[2]["url"].endswith(f"/v1/documents/{registered['id']}/einvoice"))
        self.assertEqual(len(record.writes), 1, "the document and its e-invoice are one write")
        self.assertEqual(record.metanow_fiscal_state, outcome.REGISTERED)
        self.assertEqual(record.metanow_fiscal_einvoice_state, "registered")
        self.assertEqual(record.metanow_fiscal_einvoice_eic, self.EIC)

    def test_the_e_invoice_is_not_read_before_registration(self):
        record, opener = self.sent((200, support.document_response(state="submitted")))
        record._metanow_fiscal_poll()
        self.assertEqual(len(opener.requests), 2)

    def test_an_e_invoice_not_built_yet_keeps_the_document_answer(self):
        registered = support.document_response(state="accepted", registrationCode="d9b0b4a1-0000-4000-8000-000000000001")
        record, _ = self.sent((200, registered), (404, {"code": "not_found", "retryable": False, "authorityCode": None}))
        record._metanow_fiscal_poll()
        self.assertEqual(record.metanow_fiscal_state, outcome.REGISTERED)
        self.assertIs(record.metanow_fiscal_einvoice_state, False)
        self.assertEqual(record.env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_poll"].count > 0, True,
                         "it is read again until the e-invoice is exchanged")

    def test_the_poll_job_reads_pending_e_invoices_in_their_own_batch(self):
        env = environment()
        domains = []
        record = move(env, company(env))
        record.search = lambda domain, order=None, limit=None: domains.append(domain) or []
        record._cron_metanow_fiscal_poll()
        self.assertEqual(len(domains), 2)
        self.assertIn((outcome.ELECTRONIC, "=", True), domains[1])
        self.assertIn((outcome.EINVOICE_STATE, "not in", list(outcome.EINVOICE_FINAL)), domains[1])


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
    def test_pairing_stores_the_credential_in_one_write_and_fetches_the_profile_and_codes(self):
        env = environment()
        opener = support.FakeOpener(
            (200, {"credential": "c-9", "tenantId": "shembull", "channel": "odoo"}),
            (200, support.PROFILE_DOCUMENT),
            (200, support.REGISTRY_DOCUMENT))
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
        self.assertEqual(opener.requests[2]["url"], "https://efis.example.invalid/v1/registry")
        self.assertEqual(opener.requests[2]["headers"]["x-credential"], "c-9")
        self.assertEqual(record._metanow_fiscal_registry().codes, support.registry().codes)

    def test_pairing_holds_when_the_codes_cannot_be_read(self):
        # The pairing code is single-use and already spent, so a failed read of the codes must
        # not undo it. "Check the configuration" reads them again.
        env = environment()
        opener = support.FakeOpener(
            (200, {"credential": "c-9", "tenantId": "shembull", "channel": "odoo"}),
            (200, support.PROFILE_DOCUMENT),
            (503, None))
        record = company(env, opener=opener, metanow_fiscal_credential=False, metanow_fiscal_tenant=False)
        self.assertTrue(record._metanow_fiscal_pair("123-456"))
        self.assertEqual(record.metanow_fiscal_credential, "c-9")
        self.assertIsNone(record._metanow_fiscal_registry())

    def test_a_refused_code_stores_nothing(self):
        env = environment()
        opener = support.FakeOpener((422, {"code": "pairing_code_invalid", "retryable": False, "authorityCode": None}))
        record = company(env, opener=opener)
        with self.assertRaises(fake_odoo.UserError):
            record._metanow_fiscal_pair("000")
        self.assertEqual(record.writes, [])

    def test_saving_the_settings_exchanges_a_typed_code_after_the_rest_is_saved(self):
        # Odoo's settings page saves and reloads before any other button runs, so the code is
        # exchanged by the save itself.
        env = environment()
        opener = support.FakeOpener(
            (200, {"credential": "c-9", "tenantId": "shembull", "channel": "odoo"}),
            (200, support.PROFILE_DOCUMENT),
            (200, support.REGISTRY_DOCUMENT))
        record = company(env, opener=opener, metanow_fiscal_credential=False)
        settings = ResConfigSettings(env, company_id=record, metanow_fiscal_pairing_code=" 123-456 ")
        settings.set_values()
        self.assertEqual(settings.base_calls, ["set_values"])
        self.assertEqual(record.writes[0]["metanow_fiscal_credential"], "c-9")
        self.assertFalse(settings.metanow_fiscal_pairing_code, "the single-use code is not kept")

    def test_saving_the_settings_without_a_code_calls_nothing(self):
        env = environment()
        opener = support.FakeOpener()
        record = company(env, opener=opener)
        ResConfigSettings(env, company_id=record).set_values()
        self.assertEqual(opener.requests, [])
        self.assertEqual(record.writes, [])

    def test_the_pair_button_is_the_settings_save(self):
        view = (support.MODULE_DIR / "views" / "res_config_settings_views.xml").read_text(encoding="utf-8")
        self.assertIn('<button name="execute" type="object"', view)
        self.assertNotIn("action_metanow_fiscal_pair", view)


if __name__ == "__main__":
    unittest.main()
