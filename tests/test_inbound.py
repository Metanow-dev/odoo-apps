"""DEV-2433: e-invoices suppliers sent to the business, as draft vendor bills."""

import json
import unittest
from datetime import date

import fake_odoo
import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis import inbound
from metanow_fiscalization.models.account_move import AccountMove
from test_account_move import company, environment

INBOUND = "5b8f2c1e-0000-4000-8000-0000000000aa"


def view(**overrides):
    values = {
        "id": INBOUND, "eic": "a1b2c3d4-0000-4000-8000-000000000001", "sellerTaxId": "L98765432K",
        "buyerTaxId": "K12345678L", "docNumber": "15/2026", "amount": "1200.00", "currency": None,
        "dueAt": "2026-12-10T00:00:00+01:00", "answerBy": "2026-12-11", "authorityStatus": "DELIVERED",
        "receivedAt": "2026-11-10T09:30:00Z", "firstSeenAt": "2026-11-10T09:31:00Z", "lastSeenAt": "2026-11-11T08:00:00Z",
        "hasPdf": True, "registrationCode": "d9b0b4a1-0000-4000-8000-000000000001", "waiting": True,
        "latestDecision": None,
    }
    values.update(overrides)
    return values


def decision(choice="accept", state="queued"):
    return {"id": "d-1", "decision": choice, "state": state, "requestedBy": "odoo",
            "createdAt": "2026-11-11T10:00:00Z", "answeredAt": None, "faultCode": None}


class ValuesTests(unittest.TestCase):
    def test_a_received_e_invoice_is_read_as_efis_states_it(self):
        current = inbound.values(view())
        self.assertEqual(current["seller"], "L98765432K")
        self.assertEqual(current["number"], "15/2026")
        self.assertEqual(current["amount"], "1200.00")
        self.assertEqual(current["due_on"], date(2026, 12, 10))
        self.assertEqual(current["answer_by"], date(2026, 12, 11))
        self.assertTrue(current["waiting"] and current["has_pdf"] and current["fiscalised"])
        self.assertIsNone(current["decision"])

    def test_a_foreign_currency_amount_names_its_currency(self):
        self.assertEqual(inbound.values(view(currency="EUR"))["amount"], "1200.00 EUR")

    def test_the_latest_answer_is_shown(self):
        self.assertEqual(inbound.values(view(latestDecision=decision("refuse", "sent")))["decision"], "refuse: sent")

    def test_tax_numbers_compare_without_spaces_case_or_country_prefix(self):
        for text in ("L98765432K", "l98765432k", " L98765432K ", "AL L98765432K", "AL L98765432K"):
            self.assertEqual(inbound.tax_id(text), "L98765432K", text)
        self.assertEqual(inbound.tax_id("ALB12345"), "ALB12345", "a prefix is taken off a tax number only")

    def test_the_view_is_efis_published_shape(self):
        properties = set(support.openapi()["components"]["schemas"]["EinvoiceInboundView"]["properties"])
        self.assertLessEqual(set(view()), properties)


class ClientTests(unittest.TestCase):
    def client(self, *answers):
        opener = support.FakeOpener(*answers)
        return efis_client.EfisClient("https://efis.example.invalid", credential="c-1", tenant="t-1", opener=opener), opener

    def test_the_answer_carries_only_the_decision(self):
        client, opener = self.client((202, decision()))
        client.decide_inbound(INBOUND, "accept")
        request = opener.requests[0]
        self.assertEqual((request["method"], request["url"]),
                         ("POST", f"https://efis.example.invalid/v1/einvoices/inbound/{INBOUND}/decision"))
        self.assertEqual(request["body"], {"decision": "accept"})

    def test_an_answer_is_accept_or_refuse(self):
        client, opener = self.client()
        with self.assertRaises(ValueError):
            client.decide_inbound(INBOUND, "maybe")
        self.assertEqual(opener.requests, [])

    def test_the_pdf_is_read_as_bytes(self):
        seen = []

        def opener(request, data=None, timeout=None):
            seen.append({name.lower(): value for name, value in request.header_items()})
            return _PdfAnswer(b"%PDF-1.7 ...")

        client = efis_client.EfisClient("https://efis.example.invalid", credential="c-1", tenant="t-1", opener=opener)
        response = client.get_inbound_pdf(INBOUND)
        self.assertEqual(response.body, b"%PDF-1.7 ...")
        self.assertEqual(seen[0]["accept"], "application/pdf")


class _PdfAnswer:
    status = 200
    headers = {}

    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DecisionTests(unittest.TestCase):
    def bill(self, env, *answers, **overrides):
        owner = company(env)
        opener = support.FakeOpener(*answers)
        owner._metanow_fiscal_client = lambda authenticated=True: efis_client.EfisClient(
            owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        values = dict(id=77, name="BILL/2026/00001", move_type="in_invoice", company_id=owner,
                      metanow_fiscal_inbound_id=INBOUND, metanow_fiscal_inbound_waiting=True)
        values.update(overrides)
        return AccountMove(env, **values), opener

    def test_accepting_queues_the_answer_and_reads_it_back(self):
        env = environment()
        bill, opener = self.bill(env, (202, decision()),
                                 (200, {"einvoice": view(waiting=False, latestDecision=decision()), "decisions": [decision()]}))
        bill.action_metanow_fiscal_accept()
        self.assertEqual([request["method"] for request in opener.requests], ["POST", "GET"])
        self.assertFalse(bill.metanow_fiscal_inbound_waiting)
        self.assertEqual(bill.metanow_fiscal_inbound_decision, "accept: queued")

    def test_an_answer_given_before_is_read_back_not_refused(self):
        # efis refuses a second answer (decision_exists), so pressing again is safe.
        env = environment()
        bill, _ = self.bill(env, (409, {"code": "decision_exists"}),
                            (200, {"einvoice": view(waiting=False, latestDecision=decision("refuse", "sent"))}))
        bill.action_metanow_fiscal_accept()
        self.assertEqual(bill.metanow_fiscal_inbound_decision, "refuse: sent")

    def test_an_unfiscalised_e_invoice_cannot_be_accepted(self):
        env = environment()
        bill, _ = self.bill(env, (422, {"code": "einvoice_not_fiscalised"}))
        with self.assertRaises(fake_odoo.UserError) as raised:
            bill.action_metanow_fiscal_accept()
        self.assertIn("you may refuse it", str(raised.exception))
        self.assertTrue(bill.metanow_fiscal_inbound_waiting)

    def test_no_answer_from_efis_says_try_again(self):
        env = environment()
        bill, _ = self.bill(env, OSError("timed out"))
        with self.assertRaises(fake_odoo.UserError) as raised:
            bill.action_metanow_fiscal_refuse()
        self.assertIn("try again", str(raised.exception))

    def test_a_bill_not_made_from_an_e_invoice_sends_nothing(self):
        env = environment()
        bill, opener = self.bill(env, metanow_fiscal_inbound_id=False)
        bill.action_metanow_fiscal_refuse()
        self.assertEqual(opener.requests, [])

    def test_the_buttons_ask_before_an_answer_that_cannot_be_withdrawn(self):
        view_xml = (support.MODULE_DIR / "views" / "account_move_views.xml").read_text(encoding="utf-8")
        for action in ("action_metanow_fiscal_accept", "action_metanow_fiscal_refuse"):
            start = view_xml.index(f'name="{action}"')
            self.assertIn("can never be withdrawn", view_xml[start:view_xml.index("/>", start)])


class RefreshTests(unittest.TestCase):
    def test_only_what_changed_is_written(self):
        env = environment()
        bill = AccountMove(env, metanow_fiscal_inbound_id=INBOUND, metanow_fiscal_inbound_status="DELIVERED",
                           metanow_fiscal_inbound_answer_by=date(2026, 12, 11), metanow_fiscal_inbound_waiting=True,
                           metanow_fiscal_inbound_fiscalised=True)
        bill._metanow_fiscal_inbound_refresh(inbound.values(view()))
        self.assertEqual(bill.writes, [])
        bill._metanow_fiscal_inbound_refresh(inbound.values(view(authorityStatus="ACCEPTED", waiting=False)))
        self.assertEqual(bill.writes, [{"metanow_fiscal_inbound_status": "ACCEPTED", "metanow_fiscal_inbound_waiting": False}])


class CompanyTests(unittest.TestCase):
    def test_turning_it_on_starts_from_today(self):
        env = environment()
        owner = company(env)
        owner.write({"metanow_fiscal_inbound_enabled": True})
        self.assertEqual(owner.metanow_fiscal_inbound_since, date.today())

    def test_a_day_already_set_or_given_is_kept(self):
        env = environment()
        owner = company(env, metanow_fiscal_inbound_since=date(2026, 11, 1))
        owner.write({"metanow_fiscal_inbound_enabled": True, "metanow_fiscal_inbound_since": False})
        self.assertEqual(owner.metanow_fiscal_inbound_since, date(2026, 11, 1))
        other = company(env)
        other.write({"metanow_fiscal_inbound_enabled": True, "metanow_fiscal_inbound_since": date(2026, 10, 1)})
        self.assertEqual(other.metanow_fiscal_inbound_since, date(2026, 10, 1))

    def test_a_deleted_draft_is_not_made_again(self):
        env = environment()
        owner = company(env)
        bill = AccountMove(env, company_id=owner, move_type="in_invoice", metanow_fiscal_inbound_id=INBOUND)
        bill.unlink()
        self.assertEqual(owner._metanow_fiscal_inbound_ignored(), {INBOUND})
        self.assertEqual(json.loads(owner.metanow_fiscal_inbound_ignored), [INBOUND])


if __name__ == "__main__":
    unittest.main()
