"""DEV-2443: fiscal values read back from efis after the module was uninstalled and reinstalled."""

import unittest
from types import SimpleNamespace
from datetime import date
from unittest import mock

import fake_odoo
import support
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis import restore
from metanow_fiscalization.models import account_move as account_move_module
from metanow_fiscalization.models.base_module_uninstall import BaseModuleUninstall
from test_account_move import company, environment, move

DOCUMENT = "7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88"
OTHER = "0b8f1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d"


def item(**overrides):
    """A DocumentListItem as efis lists it, for the test invoice INV/2026/00001 (240.00 to L98765432K)."""
    body = {
        "id": DOCUMENT,
        "state": "accepted",
        "fiscalNumber": "1/2026/bb123bb123",
        "securityCode": support.SECURITY_CODE,
        "registrationCode": "11111111-2222-3333-4444-555555555555",
        "verificationUrl": "https://verify.example.invalid/check?code=abc",
        "clientReference": "INV/2026/00001",
        "tenantMustAct": False,
        "issuedAt": "2026-11-02T10:00:00+01:00",
        "buyer": {"identifier": "L98765432K", "name": "Blerës sh.p.k."},
        "total": "240.00",
        "currency": "ALL",
    }
    body.update(overrides)
    return body


def wanted(**overrides):
    values = {"key": 42, "reference": "INV/2026/00001", "amount_total": 240.0, "credit_note": False,
              "currency": "ALL", "buyer_identifier": "L98765432K"}
    values.update(overrides)
    return values


class MatchTests(unittest.TestCase):
    def test_the_one_document_that_agrees_on_everything_is_matched(self):
        matches, skipped = restore.match([wanted()], [item()])
        self.assertEqual(matches, [(42, item())])
        self.assertEqual(skipped, [])

    def test_no_document_with_the_number_is_no_match(self):
        self.assertEqual(restore.match([wanted()], [item(clientReference="INV/2026/00002")]),
                         ([], [(42, restore.NO_MATCH)]))

    def test_a_different_total_is_never_matched(self):
        self.assertEqual(restore.match([wanted()], [item(total="240.01")])[1], [(42, restore.TOTAL_DIFFERS)])

    def test_a_different_buyer_is_never_matched(self):
        self.assertEqual(restore.match([wanted()], [item(buyer={"identifier": "K11111111A", "name": "x"})])[1],
                         [(42, restore.BUYER_DIFFERS)])

    def test_a_buyer_is_compared_without_spaces_as_it_was_sent(self):
        self.assertEqual(len(restore.match([wanted(buyer_identifier="L98 765 432K")], [item()])[0]), 1)

    def test_no_buyer_matches_no_buyer(self):
        self.assertEqual(len(restore.match([wanted(buyer_identifier=False)], [item(buyer=None)])[0]), 1)
        self.assertEqual(restore.match([wanted(buyer_identifier=False)], [item()])[1], [(42, restore.BUYER_DIFFERS)])

    def test_a_different_currency_is_never_matched(self):
        self.assertEqual(restore.match([wanted()], [item(currency="EUR")])[1], [(42, restore.CURRENCY_DIFFERS)])

    def test_a_document_without_a_total_cannot_be_compared(self):
        self.assertEqual(restore.match([wanted()], [item(total=None, currency=None)])[1], [(42, restore.NO_TOTAL)])

    def test_two_documents_that_both_agree_leave_the_invoice_alone(self):
        self.assertEqual(restore.match([wanted()], [item(), item(id=OTHER)]), ([], [(42, restore.SEVERAL)]))

    def test_of_two_documents_with_the_number_the_one_that_agrees_is_matched(self):
        matches, skipped = restore.match([wanted()], [item(id=OTHER, total="99.00"), item()])
        self.assertEqual([document["id"] for _, document in matches], [DOCUMENT])
        self.assertEqual(skipped, [])

    def test_two_invoices_claiming_one_document_are_both_left_alone(self):
        matches, skipped = restore.match([wanted(), wanted(key=43)], [item()])
        self.assertEqual(matches, [])
        self.assertEqual(sorted(skipped), [(42, restore.SEVERAL), (43, restore.SEVERAL)])

    def test_a_document_another_invoice_already_holds_is_never_matched_again(self):
        self.assertEqual(restore.match([wanted()], [item()], linked={DOCUMENT}), ([], [(42, restore.TAKEN)]))

    def test_a_credit_note_is_compared_with_its_total_negative_as_it_was_sent(self):
        credit = wanted(credit_note=True, reference="RINV/2026/00001")
        self.assertEqual(len(restore.match([credit], [item(clientReference="RINV/2026/00001", total="-240.00")])[0]), 1)
        self.assertEqual(restore.match([credit], [item(clientReference="RINV/2026/00001")])[1],
                         [(42, restore.TOTAL_DIFFERS)])

    def test_every_reason_has_a_sentence_in_both_languages(self):
        for code, sentence in restore.REASONS.items():
            self.assertIn(" / ", sentence, code)


class PagesTests(unittest.TestCase):
    def test_every_page_is_read_by_its_cursor(self):
        answers = {None: {"documents": [item()], "next": "c1"}, "c1": {"documents": [item(id=OTHER)], "next": None}}
        asked = []

        def read(after, limit):
            asked.append((after, limit))
            return answers[after]

        self.assertEqual([document["id"] for document in restore.pages(read)], [DOCUMENT, OTHER])
        self.assertEqual(asked, [(None, 500), ("c1", 500)])

    def test_a_cursor_seen_before_ends_the_read(self):
        self.assertEqual(len(restore.pages(lambda after, limit: {"documents": [item()], "next": "same"})), 2)


class NoteTests(unittest.TestCase):
    def test_the_note_names_each_value_with_the_modules_labels(self):
        text = restore.note({outcome.FISCAL_NUMBER: "1/2026/bb", outcome.SECURITY_CODE: "abc",
                             outcome.REGISTRATION_CODE: None, outcome.DOCUMENT_ID: DOCUMENT})
        self.assertEqual(text.splitlines(), [
            "Numri fiskal / Fiscal number: 1/2026/bb",
            "Kodi i sigurisë së lëshuesit / Issuer security code: abc",
            f"Dokumenti në efis / efis document: {DOCUMENT}",
        ])


def restore_env():
    env = environment()
    for name in ("restore", "payments"):
        env.refs[f"metanow_fiscalization.ir_cron_metanow_fiscal_{name}"] = fake_odoo.Trigger()
    return env


def emptied(env, owner, **overrides):
    """A posted invoice an uninstall emptied: no fiscal value at all."""
    values = dict(state="posted", invoice_date=date(2026, 11, 2))
    values.update(overrides)
    return move(env, owner, **values)


class RestoreRunTests(unittest.TestCase):
    def test_a_matched_invoice_gets_every_value_back_in_one_write_and_is_locked_again(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item()], "next": None}), (404, None))
        record = emptied(env, company(env, opener=opener))
        self.assertTrue(self.run_with(record))
        self.assertEqual(len(record.writes), 1)
        written = record.writes[0]
        self.assertEqual(written[outcome.DOCUMENT_ID], DOCUMENT)
        self.assertEqual(written[outcome.SECURITY_CODE], support.SECURITY_CODE)
        self.assertEqual(written[outcome.STATE], outcome.REGISTERED)
        self.assertEqual(written[outcome.VERIFICATION_URL], item()["verificationUrl"])
        self.assertEqual(written[outcome.ISSUED_AT], item()["issuedAt"])
        self.assertIs(written[outcome.ELECTRONIC], False)
        self.assertTrue(written["metanow_fiscal_restored"])
        # A restore only reads: the payments wait for a person (decided 12 Nov 2026).
        self.assertTrue(written["metanow_fiscal_payments_review"])
        self.assertNotIn("metanow_fiscal_payments_pending", written)
        self.assertNotIn("metanow_fiscal_payment_records_pending", written)
        self.assertTrue(record._metanow_fiscal_locked())
        with self.assertRaises(fake_odoo.UserError):
            record.button_draft()
        self.assertEqual(len(record.messages), 1)
        self.assertIn("Restored from efis", record.messages[0])
        self.assertIn(support.SECURITY_CODE, record.messages[0])
        # Only reads: the list and the e-invoice, never a POST.
        self.assertEqual([request["method"] for request in opener.requests], ["GET", "GET"])
        self.assertIn("/v1/documents?limit=500", opener.requests[0]["url"])
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"].count, 0)

    def test_an_e_invoice_comes_back_with_its_state_and_code(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item()]}), (200, {"state": "registered", "eic": "EIC-1"}))
        record = emptied(env, company(env, opener=opener))
        self.run_with(record)
        self.assertIs(record.metanow_fiscal_electronic, True)
        self.assertEqual(record.metanow_fiscal_einvoice_eic, "EIC-1")
        self.assertEqual(record.metanow_fiscal_einvoice_state, "registered")

    def test_a_document_efis_has_not_registered_yet_goes_back_to_polling(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(state="submitted", registrationCode=None)]}), (404, None))
        record = emptied(env, company(env, opener=opener))
        self.run_with(record)
        self.assertEqual(record.metanow_fiscal_state, outcome.SENT)
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_poll"].count, 1)

    def test_an_unread_e_invoice_leaves_the_invoice_alone_with_the_reason(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item()]}), (503, {"code": "unavailable"}))
        record = emptied(env, company(env, opener=opener))
        self.run_with(record)
        self.assertFalse(record.metanow_fiscal_document_id)
        self.assertEqual(record.writes, [{"metanow_fiscal_restore_note": restore.REASONS[restore.EINVOICE_UNREAD]}])

    def test_an_invoice_with_a_different_total_is_left_alone_and_says_why(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(total="999.00")]}))
        record = emptied(env, company(env, opener=opener))
        self.run_with(record)
        self.assertEqual(record.writes, [{"metanow_fiscal_restore_note": restore.REASONS[restore.TOTAL_DIFFERS]}])
        self.assertFalse(record._metanow_fiscal_locked())

    def test_an_invoice_posted_after_efis_began_and_not_found_is_listed(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(clientReference="INV/2026/00009")]}))
        record = emptied(env, company(env, opener=opener), invoice_date=date(2026, 11, 3))
        self.run_with(record)
        self.assertEqual(record.metanow_fiscal_restore_note, restore.REASONS[restore.NO_MATCH])

    def test_an_invoice_from_before_efis_began_is_not_listed(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(clientReference="INV/2026/00009")]}))
        record = emptied(env, company(env, opener=opener), invoice_date=date(2026, 1, 15))
        self.run_with(record)
        self.assertEqual(record.writes, [])

    def test_with_nothing_in_efis_nothing_is_written(self):
        env = restore_env()
        record = emptied(env, company(env, opener=support.FakeOpener((200, {"documents": []}))))
        self.run_with(record)
        self.assertEqual(record.writes, [])

    def test_running_it_again_changes_nothing(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(total="999.00")]}), (200, {"documents": [item(total="999.00")]}))
        record = emptied(env, company(env, opener=opener))
        self.run_with(record)
        self.run_with(record)
        self.assertEqual(len(record.writes), 1)

    def test_an_invoice_queued_since_the_list_was_read_is_not_touched(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item()]}))
        record = emptied(env, company(env, opener=opener), metanow_fiscal_state=outcome.TO_SEND)
        self.run_with(record)
        self.assertEqual(record.writes, [])

    def test_a_long_history_is_restored_over_several_runs(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(), item(id=OTHER, clientReference="INV/2026/00002")]}),
                                    (404, None))
        owner = company(env, opener=opener)
        first, second = emptied(env, owner), emptied(env, owner, id=43, name="INV/2026/00002")
        with mock.patch.object(account_move_module, "RESTORE_BATCH", 1):
            self.assertFalse(self.run_with(first, second))
        self.assertTrue(first.metanow_fiscal_restored)
        self.assertFalse(second.metanow_fiscal_document_id)
        self.assertFalse(second.writes)

    def test_a_refused_list_says_so(self):
        env = restore_env()
        record = emptied(env, company(env, opener=support.FakeOpener((401, {"code": "unauthorised"}))))
        with self.assertRaises(fake_odoo.UserError):
            self.run_with(record)

    def run_with(self, *records):
        Move = account_move_module.AccountMove
        with mock.patch.object(Move, "_metanow_fiscal_restore_candidates", lambda self, company: list(records)), \
                mock.patch.object(Move, "_metanow_fiscal_linked", lambda self, ids: set()):
            return records[0]._metanow_fiscal_restore(records[0].company_id)


class PollAfterRestoreTests(unittest.TestCase):
    def test_a_restored_invoice_reads_its_e_invoice_once_when_efis_registers_it(self):
        env = restore_env()
        opener = support.FakeOpener(
            (200, support.document_response(state="accepted", registrationCode="r-1")),
            (200, {"state": "queued", "eic": None}))
        record = move(env, company(env, opener=opener), state="posted", metanow_fiscal_restored=True,
                      metanow_fiscal_noted=True, metanow_fiscal_document_id=support.document_response()["id"],
                      metanow_fiscal_state=outcome.SENT, metanow_fiscal_security_code=support.SECURITY_CODE,
                      metanow_fiscal_number="1/2026/bb123bb123")
        record._metanow_fiscal_poll()
        self.assertEqual(len(record.writes), 1)
        self.assertIs(record.writes[0][outcome.ELECTRONIC], True)
        self.assertEqual(record.writes[0][outcome.EINVOICE_STATE], "queued")

    def test_a_restored_invoice_without_an_e_invoice_stays_a_plain_invoice(self):
        env = restore_env()
        opener = support.FakeOpener((200, support.document_response(state="accepted", registrationCode="r-1")), (404, None))
        record = move(env, company(env, opener=opener), state="posted", metanow_fiscal_restored=True,
                      metanow_fiscal_noted=True, metanow_fiscal_document_id=support.document_response()["id"],
                      metanow_fiscal_state=outcome.SENT, metanow_fiscal_security_code=support.SECURITY_CODE,
                      metanow_fiscal_number="1/2026/bb123bb123")
        record._metanow_fiscal_poll()
        self.assertNotIn(outcome.ELECTRONIC, record.writes[0])

    def test_registration_posts_the_values_to_the_chatter_once(self):
        env = restore_env()
        answer = (200, support.document_response(state="accepted", registrationCode="r-1"))
        opener = support.FakeOpener(answer, (200, support.document_response(state="settled", registrationCode="r-1")))
        record = move(env, company(env, opener=opener), metanow_fiscal_document_id=support.document_response()["id"],
                      metanow_fiscal_state=outcome.SENT, metanow_fiscal_security_code=support.SECURITY_CODE,
                      metanow_fiscal_number="1/2026/bb123bb123")
        record._metanow_fiscal_poll()
        self.assertTrue(record.writes[0]["metanow_fiscal_noted"])
        self.assertEqual(len(record.messages), 1)
        self.assertIn("Registered by efis", record.messages[0])
        self.assertIn("r-1", record.messages[0])
        record._metanow_fiscal_poll()
        self.assertEqual(len(record.messages), 1)


class RestoreRequestTests(unittest.TestCase):
    def test_pairing_queues_a_restore(self):
        env = restore_env()
        opener = support.FakeOpener(
            (200, {"credential": "c-2", "tenantId": "shembull", "channel": "odoo"}),
            (200, support.PROFILE_DOCUMENT), (200, support.REGISTRY_DOCUMENT))
        owner = company(env, opener=opener)
        owner._metanow_fiscal_pair("ABC-123")
        self.assertTrue(owner.metanow_fiscal_restore_pending)
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_restore"].count, 1)

    def test_the_summary_is_counted_from_the_invoices(self):
        env = restore_env()
        env.models["account.move"] = account_move_module.AccountMove(env)
        env.counts = [3, 1, 4]
        owner = company(env, metanow_fiscal_restore_pending=True)
        owner._metanow_fiscal_restore_finished()
        self.assertFalse(owner.metanow_fiscal_restore_pending)
        self.assertEqual(owner.metanow_fiscal_restore_summary,
                         "Rikthyer: 3 · Lënë mënjanë: 3 · Në konflikt: 1 / Restored: 3 · Left alone: 3 · In conflict: 1")


class UninstallTests(unittest.TestCase):
    def wizard(self, modules, sent):
        env = environment()
        env.models["account.move"] = account_move_module.AccountMove(env)
        env.counts = [sent] * 4
        return BaseModuleUninstall(env, module_names=modules)

    def test_uninstalling_the_module_with_invoices_sent_to_efis_warns(self):
        warning = self.wizard(["metanow_fiscalization"], 7)._metanow_fiscal_warning()
        self.assertIn("7", warning)
        self.assertIn("Restore from efis", warning)

    def test_uninstalling_a_module_it_depends_on_warns_too(self):
        self.assertTrue(self.wizard(["account", "metanow_fiscalization"], 1)._metanow_fiscal_warning())

    def test_no_warning_without_invoices_sent_or_for_another_module(self):
        self.assertIsNone(self.wizard(["metanow_fiscalization"], 0)._metanow_fiscal_warning())
        self.assertIsNone(self.wizard(["sale"], 5)._metanow_fiscal_warning())

    def test_the_uninstall_waits_for_the_confirmation_and_is_never_blocked(self):
        wizard = self.wizard(["metanow_fiscalization"], 2)
        with self.assertRaises(fake_odoo.UserError):
            wizard.action_uninstall()
        self.assertEqual(wizard.base_calls, [])
        wizard.metanow_fiscal_confirmed = True
        self.assertTrue(wizard.action_uninstall())
        self.assertEqual(wizard.base_calls, ["action_uninstall"])

    def test_without_a_warning_the_uninstall_goes_ahead_as_odoo_does_it(self):
        wizard = self.wizard(["metanow_fiscalization"], 0)
        self.assertTrue(wizard.action_uninstall())


if __name__ == "__main__":
    unittest.main()


class PaymentReviewTests(unittest.TestCase):
    def test_a_restored_credit_note_has_no_payments_to_review(self):
        env = restore_env()
        opener = support.FakeOpener((200, {"documents": [item(clientReference="RINV/2026/00001", total="-240.00")]}),
                                    (404, None))
        record = emptied(env, company(env, opener=opener), name="RINV/2026/00001", move_type="out_refund")
        RestoreRunTests.run_with(None, record)
        self.assertIs(record.metanow_fiscal_payments_review, False)

    def test_sending_the_payments_ends_the_review_and_queues_both_syncs(self):
        env = restore_env()
        record = move(env, company(env), metanow_fiscal_payments_review=True, metanow_fiscal_restored=True)
        record.action_metanow_fiscal_send_payments()
        self.assertEqual(record.writes[-1], {"metanow_fiscal_payments_pending": True,
                                             "metanow_fiscal_payment_records_pending": True,
                                             "metanow_fiscal_payments_review": False})
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"].count, 1)

    def test_the_summary_says_how_many_invoices_wait_for_their_payments(self):
        env = restore_env()
        env.models["account.move"] = account_move_module.AccountMove(env)
        env.counts = [3, 0, 3, 2]
        owner = company(env, metanow_fiscal_restore_pending=True)
        owner._metanow_fiscal_restore_finished()
        self.assertIn("The payments of 2 invoices have not been sent", owner.metanow_fiscal_restore_summary)


class BackfillTests(unittest.TestCase):
    def registered(self, env, owner, **values):
        values = dict(dict(metanow_fiscal_document_id=DOCUMENT, metanow_fiscal_state=outcome.REGISTERED,
                           metanow_fiscal_number="1/2026/bb123bb123", metanow_fiscal_security_code=support.SECURITY_CODE,
                           metanow_fiscal_registration_code="r-1"), **values)
        return move(env, owner, **values)

    def test_each_registered_invoice_without_a_note_gets_one_and_nothing_is_sent(self):
        env = restore_env()
        opener = support.FakeOpener()
        owner = company(env, opener=opener)
        first, second = self.registered(env, owner), self.registered(env, owner, id=43)
        domains = []
        first.search = lambda domain, order=None, limit=None: domains.append(domain) or [first, second]
        self.assertEqual(first._metanow_fiscal_backfill_notes(), 2)
        for record in (first, second):
            self.assertEqual(record.writes, [{"metanow_fiscal_noted": True}])
            self.assertEqual(len(record.messages), 1)
            self.assertIn("Registered by efis earlier", record.messages[0])
            self.assertIn(support.SECURITY_CODE, record.messages[0])
        self.assertIn(("metanow_fiscal_noted", "=", False), domains[0])
        self.assertIn((outcome.STATE, "in", [outcome.REGISTERED, outcome.SUPERSEDED]), domains[0])
        self.assertEqual(opener.requests, [])

    def test_with_every_invoice_noted_nothing_is_posted(self):
        env = restore_env()
        record = self.registered(env, company(env), metanow_fiscal_noted=True)
        record.search = lambda domain, order=None, limit=None: []
        self.assertEqual(record._metanow_fiscal_backfill_notes(), 0)

    def test_the_upgrade_runs_the_backfill_and_a_new_install_does_not(self):
        import importlib.util
        path = support.MODULE_DIR / "migrations" / "20.0.1.1.0" / "post-migrate.py"
        spec = importlib.util.spec_from_file_location("post_migrate_2443", path)
        script = importlib.util.module_from_spec(spec)
        calls = []
        fake_api = type("api", (), {"Environment": staticmethod(lambda cr, uid, context: {"account.move": SimpleNamespace(
            _metanow_fiscal_backfill_notes=lambda: calls.append(uid))})})
        import odoo
        odoo.api_backup, odoo.SUPERUSER_ID_backup = odoo.api, getattr(odoo, "SUPERUSER_ID", None)
        odoo.api, odoo.SUPERUSER_ID = fake_api, 1
        try:
            spec.loader.exec_module(script)
            script.migrate(None, None)
            self.assertEqual(calls, [])
            script.migrate(None, "20.0.1.0.3")
            self.assertEqual(calls, [1])
        finally:
            odoo.api = odoo.api_backup
            if odoo.SUPERUSER_ID_backup is None:
                del odoo.SUPERUSER_ID
            else:
                odoo.SUPERUSER_ID = odoo.SUPERUSER_ID_backup
            del odoo.api_backup, odoo.SUPERUSER_ID_backup
