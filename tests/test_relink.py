"""DEV-2462: received e-invoices linked to their vendor bills again after a reinstall."""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

import fake_odoo
import support
from metanow_fiscalization.efis import relink
from metanow_fiscalization.models import account_move as account_move_module
from metanow_fiscalization.models.account_move import AccountMove
from test_account_move import company, environment
from test_inbound import INBOUND, view

EIC = view()["eic"]
OTHER_INBOUND = "5b8f2c1e-0000-4000-8000-0000000000bb"
OTHER_EIC = "a1b2c3d4-0000-4000-8000-000000000002"


def wanted(**overrides):
    """A vendor bill as relink.match reads it: the test bill 15/2026, 1200.00 ALL from L98765432K."""
    values = {"key": 77, "eic": None, "seller": "L98765432K", "number": "15/2026",
              "amount_total": 1200.0, "has_lines": True, "currency": "ALL"}
    values.update(overrides)
    return values


class MatchTests(unittest.TestCase):
    def test_the_eic_the_chatter_names_is_certain_by_itself(self):
        matches, skipped = relink.match([wanted(eic=EIC, number=False, seller=None, amount_total=0.0)], [view()])
        self.assertEqual(matches, [(77, view())])
        self.assertEqual(skipped, [])

    def test_a_chatter_eic_efis_no_longer_lists_is_no_match_and_is_said_so(self):
        self.assertEqual(relink.match([wanted(eic=OTHER_EIC)], [view()]),
                         ([], [(77, relink.NO_MATCH)]))

    def test_a_chatter_eic_is_never_traded_for_another_e_invoice_that_would_match(self):
        # The seller, the number and the amount all agree, but the chatter names another e-invoice.
        self.assertEqual(relink.match([wanted(eic=OTHER_EIC)], [view()])[0], [])

    def test_without_an_eic_the_seller_number_amount_and_currency_match(self):
        matches, skipped = relink.match([wanted()], [view()])
        self.assertEqual(matches, [(77, view())])
        self.assertEqual(skipped, [])

    def test_a_tax_number_is_compared_without_spaces_or_the_country_prefix(self):
        self.assertEqual(relink.match([wanted(seller="AL L98765432K")], [view()])[0], [(77, view())])

    def test_a_reused_document_number_leaves_the_bill_alone(self):
        # The same supplier, the same number in another year, the same amount: neither is certainly it.
        again = view(id=OTHER_INBOUND, eic=OTHER_EIC)
        self.assertEqual(relink.match([wanted()], [view(), again]), ([], [(77, relink.SEVERAL)]))

    def test_a_reused_number_is_still_certain_where_the_chatter_names_the_eic(self):
        again = view(id=OTHER_INBOUND, eic=OTHER_EIC)
        self.assertEqual(relink.match([wanted(eic=EIC)], [view(), again])[0], [(77, view())])

    def test_the_same_amount_from_one_supplier_under_another_number_is_not_matched(self):
        self.assertEqual(relink.match([wanted(number="16/2026")], [view()]), ([], []))

    def test_a_different_amount_is_never_matched(self):
        self.assertEqual(relink.match([wanted(amount_total=1200.01)], [view()])[1],
                         [(77, relink.TOTAL_DIFFERS)])

    def test_a_bill_with_no_lines_is_matched_without_its_amount(self):
        # A bill the module made carries no lines until a person enters them from the PDF.
        self.assertEqual(relink.match([wanted(has_lines=False, amount_total=0.0)], [view()])[0], [(77, view())])

    def test_a_different_currency_is_never_matched(self):
        self.assertEqual(relink.match([wanted(currency="EUR")], [view()])[1], [(77, relink.CURRENCY_DIFFERS)])
        self.assertEqual(relink.match([wanted(currency="EUR")], [view(currency="EUR")])[0], [(77, view(currency="EUR"))])

    def test_an_e_invoice_another_bill_already_holds_is_never_linked_again(self):
        self.assertEqual(relink.match([wanted()], [view()], linked=[INBOUND]), ([], [(77, relink.TAKEN)]))

    def test_two_bills_claiming_one_e_invoice_are_both_left_alone(self):
        bills = [wanted(), wanted(key=78)]
        matches, skipped = relink.match(bills, [view()])
        self.assertEqual(matches, [])
        self.assertEqual(sorted(skipped), [(77, relink.SEVERAL), (78, relink.SEVERAL)])

    def test_a_bill_efis_holds_nothing_like_is_not_listed_at_all(self):
        # Most vendor bills are ordinary purchases that never were an e-invoice.
        self.assertEqual(relink.match([wanted(seller="M11111111N")], [view()]), ([], []))

    def test_an_e_invoice_without_an_id_is_ignored(self):
        self.assertEqual(relink.match([wanted()], [view(id=None)]), ([], []))

    def test_every_reason_has_a_sentence_in_both_languages(self):
        for reason, text in relink.REASONS.items():
            self.assertIn(" / ", text, reason)
        self.assertEqual(set(relink.REASONS), {relink.NO_MATCH, relink.SEVERAL, relink.TOTAL_DIFFERS,
                                               relink.CURRENCY_DIFFERS, relink.TAKEN})


class NoteTests(unittest.TestCase):
    def test_the_note_carries_the_eic_and_what_identifies_the_e_invoice(self):
        from metanow_fiscalization.efis import inbound
        text = relink.note("Heading.", inbound.values(view()))
        self.assertEqual(text.splitlines()[0], "Heading.")
        self.assertIn(f"EIC: {EIC}", text)
        self.assertIn("Numri i dokumentit / Document number: 15/2026", text)
        self.assertIn(f"Received e-invoice in efis: {INBOUND}", text)

    def test_the_eic_is_read_back_from_the_note(self):
        from metanow_fiscalization.efis import inbound
        self.assertEqual(relink.eic_in(relink.note("Heading.", inbound.values(view()))), EIC)

    def test_the_eic_is_read_back_from_the_html_odoo_stored_the_note_as(self):
        body = f"<p>Heading.<br>EIC: {EIC}<br>Numri i dokumentit / Document number: 15/2026</p>"
        self.assertEqual(relink.eic_in(body), EIC)

    def test_a_chatter_note_that_names_no_eic_reads_as_none(self):
        self.assertIsNone(relink.eic_in("<p>Regjistruar nga efis.</p>"))
        self.assertIsNone(relink.eic_in(""))
        self.assertIsNone(relink.eic_in(None))


class Messages:
    """mail.message, reduced to the bodies a bill's chatter holds."""

    def __init__(self, bodies=None):
        self.bodies = bodies or {}
        self.domains = []

    def sudo(self):
        return self

    def search(self, domain, order=None, limit=None):
        self.domains.append(domain)
        ids = next(value for field, _operator, value in domain if field == "res_id")
        return [SimpleNamespace(res_id=key, body=body) for key in ids for body in self.bodies.get(key, [])]


class Attachments:
    """ir.attachment, reduced to which records already have a PDF."""

    def __init__(self, with_pdf=()):
        self.with_pdf = set(with_pdf)

    def sudo(self):
        return self

    def search_count(self, domain):
        key = next(value for field, _operator, value in domain if field == "res_id")
        return 1 if key in self.with_pdf else 0


def relink_env(bodies=None, with_pdf=()):
    env = environment()
    env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_restore"] = fake_odoo.Trigger()
    env.models["mail.message"] = Messages(bodies)
    env.models["ir.attachment"] = Attachments(with_pdf)
    return env


def bill(env, owner, **overrides):
    """A vendor bill an uninstall emptied: the e-invoice's fields are gone, the bill is not."""
    values = dict(id=77, name="BILL/2026/00001", move_type="in_invoice", state="posted",
                  company_id=owner, ref="15/2026", amount_total=1200.0,
                  currency_id=SimpleNamespace(name="ALL"),
                  commercial_partner_id=SimpleNamespace(vat="L98765432K"),
                  invoice_line_ids=[SimpleNamespace(display_type="product")])
    values.update(overrides)
    return AccountMove(env, **values)


class RelinkRunTests(unittest.TestCase):
    def run_with(self, *bills, views=None, linked=()):
        opener = support.FakeOpener((200, {"einvoices": list(views if views is not None else [view()])}))
        owner = bills[0].company_id
        owner._metanow_fiscal_client = company(
            owner.env, opener=opener, metanow_fiscal_inbound_enabled=True)._metanow_fiscal_client
        Move = account_move_module.AccountMove
        with mock.patch.object(Move, "_metanow_fiscal_relink_candidates", lambda self, owner: list(bills)), \
                mock.patch.object(Move, "_metanow_fiscal_inbound_linked", lambda self, ids: set(linked)):
            return bills[0]._metanow_fiscal_relink_inbound(owner), opener

    def test_a_matched_bill_is_linked_in_one_write_and_says_so_in_the_chatter(self):
        env = relink_env()
        record = bill(env, company(env))
        done, opener = self.run_with(record)
        self.assertTrue(done)
        self.assertEqual(len(record.writes), 1)
        written = record.writes[0]
        self.assertEqual(written["metanow_fiscal_inbound_id"], INBOUND)
        self.assertEqual(written["metanow_fiscal_inbound_eic"], EIC)
        self.assertEqual(written["metanow_fiscal_inbound_seller"], "L98765432K")
        self.assertEqual(written["metanow_fiscal_inbound_amount"], "1200.00")
        self.assertEqual(written["metanow_fiscal_inbound_status"], "DELIVERED")
        self.assertEqual(written["metanow_fiscal_inbound_answer_by"], date(2026, 12, 11))
        self.assertIs(written["metanow_fiscal_inbound_waiting"], True)
        self.assertIs(written["metanow_fiscal_inbound_fiscalised"], True)
        self.assertIs(written["metanow_fiscal_restored"], True)
        self.assertIs(written["metanow_fiscal_restore_note"], False)
        # The chatter keeps the e-invoice's identity for the next uninstall.
        self.assertEqual(len(record.messages), 1)
        self.assertIn("Linked again", record.messages[0])
        self.assertEqual(relink.eic_in(record.messages[0]), EIC)
        # Only reads: the list, and never a decision.
        self.assertEqual([request["method"] for request in opener.requests], ["GET"])
        self.assertIn("/v1/einvoices/inbound", opener.requests[0]["url"])

    def test_the_bills_decision_is_not_answered_by_the_re_link(self):
        env = relink_env()
        record = bill(env, company(env))
        self.run_with(record)
        self.assertNotIn("metanow_fiscal_inbound_decision", [key for key in record.writes[0] if record.writes[0][key]])
        self.assertIs(record.metanow_fiscal_inbound_waiting, True)

    def test_a_bill_whose_chatter_names_the_eic_is_linked_though_nothing_else_agrees(self):
        env = relink_env(bodies={77: [f"<p>EIC: {EIC}</p>"]})
        record = bill(env, company(env), ref=False, amount_total=0.0,
                      commercial_partner_id=SimpleNamespace(vat=False))
        self.run_with(record)
        self.assertEqual(record.metanow_fiscal_inbound_id, INBOUND)

    def test_the_chatter_is_read_in_one_search_over_every_bill(self):
        env = relink_env(bodies={77: [f"<p>EIC: {EIC}</p>"]})
        record = bill(env, company(env))
        self.run_with(record, bill(env, company(env), id=78, ref="16/2026"))
        self.assertEqual(len(env.models["mail.message"].domains), 1)
        self.assertIn(("res_id", "in", [77, 78]), env.models["mail.message"].domains[0])

    def test_a_bill_left_alone_is_told_why_and_never_written_to_otherwise(self):
        env = relink_env()
        record = bill(env, company(env))
        done, _opener = self.run_with(record, views=[view(), view(id=OTHER_INBOUND, eic=OTHER_EIC)])
        self.assertTrue(done)
        self.assertEqual(record.writes, [{"metanow_fiscal_restore_note": relink.REASONS[relink.SEVERAL]}])
        self.assertFalse(record.metanow_fiscal_inbound_id)
        self.assertEqual(record.messages if hasattr(record, "messages") else [], [])

    def test_a_bill_efis_holds_nothing_like_is_left_untouched_and_unmarked(self):
        env = relink_env()
        record = bill(env, company(env), ref="99/2026")
        self.run_with(record)
        self.assertEqual(record.writes, [])

    def test_an_e_invoice_a_person_deleted_the_bill_of_is_left_alone(self):
        env = relink_env()
        owner = company(env, metanow_fiscal_inbound_ignored=f'["{INBOUND}"]')
        record = bill(env, owner)
        self.assertTrue(self.run_with(record)[0])
        self.assertEqual(record.writes, [])

    def test_running_it_again_changes_nothing(self):
        env = relink_env()
        record = bill(env, company(env))
        self.run_with(record)
        writes = len(record.writes)
        # The second run finds the bill linked, so it is no longer a candidate; a bill that was
        # linked between the list and the write is left as it is.
        self.run_with(record)
        self.assertEqual(len(record.writes), writes)
        self.assertEqual(len(record.messages), 1)

    def test_the_pdf_is_left_pending_only_where_the_bill_has_none(self):
        env = relink_env()
        record = bill(env, company(env))
        self.run_with(record)
        self.assertIs(record.writes[0]["metanow_fiscal_inbound_pdf_pending"], True)

        kept = relink_env(with_pdf=[77])
        record = bill(kept, company(kept))
        self.run_with(record)
        self.assertIs(record.writes[0]["metanow_fiscal_inbound_pdf_pending"], False)

    def test_a_refused_list_says_so_and_links_nothing(self):
        env = relink_env()
        record = bill(env, company(env))
        opener = support.FakeOpener((503, None))
        owner = record.company_id
        owner._metanow_fiscal_client = company(env, opener=opener)._metanow_fiscal_client
        Move = account_move_module.AccountMove
        with mock.patch.object(Move, "_metanow_fiscal_relink_candidates", lambda self, company: [record]), \
                mock.patch.object(Move, "_metanow_fiscal_inbound_linked", lambda self, ids: set()):
            with self.assertRaises(fake_odoo.UserError):
                record._metanow_fiscal_relink_inbound(owner)
        self.assertEqual(record.writes, [])

    def test_a_long_history_is_linked_over_several_runs(self):
        env = relink_env()
        owner = company(env)
        bills = [bill(env, owner, id=100 + number, ref=f"{number}/2026") for number in range(3)]
        views = [view(id=f"id-{number}", eic=f"eic-{number}", docNumber=f"{number}/2026") for number in range(3)]
        with mock.patch.object(account_move_module, "RESTORE_BATCH", 2):
            done, _opener = self.run_with(*bills, views=views)
        self.assertFalse(done, "a run that filled its batch asks for another")
        self.assertEqual([bool(record.metanow_fiscal_inbound_id) for record in bills], [True, True, False])


class RestoreActionTests(unittest.TestCase):
    """The re-link runs from the same action as DEV-2443's restore, never on its own."""

    def ran(self, **overrides):
        env = relink_env()
        owner = company(env, **overrides)
        env.models["res.company"] = SimpleNamespace(sudo=lambda: SimpleNamespace(search=lambda domain: [owner]))
        env.models["account.move"] = account_move_module.AccountMove(env)
        env.counts = [0, 0, 0]
        calls = []
        Move = account_move_module.AccountMove
        record = AccountMove(env)
        with mock.patch.object(Move, "_metanow_fiscal_restore", lambda self, company: calls.append("restore") is None), \
                mock.patch.object(Move, "_metanow_fiscal_relink_inbound",
                                  lambda self, company: calls.append("relink") is None):
            record._cron_metanow_fiscal_restore()
        return calls, owner

    def test_the_restore_action_also_links_the_received_bills(self):
        calls, owner = self.ran(metanow_fiscal_inbound_enabled=True)
        self.assertEqual(calls, ["restore", "relink"])
        self.assertIs(owner.metanow_fiscal_restore_pending, False)

    def test_a_company_that_does_not_import_received_e_invoices_links_nothing(self):
        self.assertEqual(self.ran(metanow_fiscal_inbound_enabled=False)[0], ["restore"])


class InboundNoteTests(unittest.TestCase):
    """DEV-2433's bill creation writes the EIC to the chatter, so a re-link can be certain."""

    def test_a_new_bill_from_a_received_e_invoice_names_its_eic_in_the_chatter(self):
        from metanow_fiscalization.efis import inbound
        env = relink_env()
        owner = company(env, metanow_fiscal_inbound_enabled=True)
        created = []

        class Bills(AccountMove):
            def create(self, values):
                record = bill(env, owner, **{key: value for key, value in values.items()
                                             if key not in ("move_type", "company_id", "partner_id")})
                record.metanow_fiscal_inbound_pdf_pending = False
                created.append(record)
                return record

        Move = Bills(env)
        Move._metanow_fiscal_inbound_partner = lambda company, seller: None
        Move._metanow_fiscal_inbound_create(owner, inbound.values(view()), client=None)
        self.assertEqual(len(created), 1)
        self.assertEqual(relink.eic_in(created[0].messages[0]), EIC)
        self.assertIn("made from an e-invoice", created[0].messages[0])
