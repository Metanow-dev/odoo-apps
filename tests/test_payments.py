"""DEV-2432: Odoo's payments on an invoice, kept in step with efis's receivables."""

import unittest
from datetime import date
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis import receivables
from metanow_fiscalization.models.account_move import AccountMove
from metanow_fiscalization.models.account_partial_reconcile import AccountPartialReconcile
from metanow_fiscalization.models.account_payment import AccountPayment
from test_account_move import company, environment, move

DATABASE = "db-0001"
TODAY = date(2026, 11, 11)
DOCUMENT = "7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88"


class Records(list):
    """A recordset, as far as the payment code reads one: filtered, and fields read across it."""

    def filtered(self, predicate):
        return Records(record for record in self if predicate(record))

    def __getattr__(self, name):
        return Records(value for record in self for value in getattr(record, name))


def journal(name="Banka", method="ACCOUNT"):
    return SimpleNamespace(name=name, metanow_fiscal_payment_method=method)


def payment_entry(paid_on=date(2026, 11, 10), bank=None, statement=False):
    """The journal entry on the other side of a reconciliation."""
    payment = SimpleNamespace(date=paid_on) if not statement else False
    return SimpleNamespace(origin_payment_id=payment, statement_line_id=bool(statement),
                           date=paid_on, journal_id=bank or journal(), move_type="entry")


def partial(partial_id, amount, counterpart):
    return SimpleNamespace(id=partial_id, amount=amount, credit_move_id=SimpleNamespace(move_id=counterpart))


def receivable_line(*partials):
    return SimpleNamespace(account_id=SimpleNamespace(account_type="asset_receivable"), matched_credit_ids=Records(partials))


def invoice(env, partials=(), payments=(), **overrides):
    owner = overrides.pop("owner", None) or company(env)
    values = dict(
        metanow_fiscal_state=outcome.REGISTERED,
        metanow_fiscal_document_id=DOCUMENT,
        metanow_fiscal_payments_pending=True,
        line_ids=Records([receivable_line(*partials),
                          SimpleNamespace(account_id=SimpleNamespace(account_type="income"), matched_credit_ids=Records())]),
        matched_payment_ids=Records(payments),
    )
    values.update(overrides)
    record = move(env, owner, **values)
    env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"] = fake_odoo.Trigger()
    return record


def held(payment_id, reference, amount="120.00", paid_on="2026-11-10", method="ACCOUNT", withdrawn=None):
    return {"id": payment_id, "documentId": DOCUMENT, "amount": amount, "paidOn": paid_on, "method": method,
            "reference": reference, "recordedAt": "2026-11-10T10:00:00Z", "recordedBy": "odoo",
            "withdrawnAt": withdrawn, "withdrawnBy": None}


def wanted(amount="120.00", paid_on="2026-11-10", method="ACCOUNT"):
    return {"amount": amount, "paidOn": paid_on, "method": method}


REF = "odoo-db-0001-rec-7"


class PlanTests(unittest.TestCase):
    def test_a_new_payment_is_recorded(self):
        withdraw, record, waiting = receivables.plan({REF: wanted()}, [], DATABASE, TODAY)
        self.assertEqual((withdraw, waiting), ([], False))
        self.assertEqual(record, [dict(wanted(), reference=REF)])

    def test_a_payment_efis_already_holds_is_not_sent_again(self):
        # What makes a lost answer safe: the next pass finds it under its reference.
        self.assertEqual(receivables.plan({REF: wanted()}, [held("p-1", REF)], DATABASE, TODAY), ([], [], False))

    def test_an_undone_payment_is_withdrawn(self):
        self.assertEqual(receivables.plan({}, [held("p-1", REF)], DATABASE, TODAY), (["p-1"], [], False))

    def test_a_changed_payment_is_withdrawn_and_recorded_again(self):
        withdraw, record, _ = receivables.plan({REF: wanted(amount="100.00")}, [held("p-1", REF)], DATABASE, TODAY)
        self.assertEqual(withdraw, ["p-1"])
        self.assertEqual(record[0]["amount"], "100.00")

    def test_a_payment_recorded_twice_keeps_one(self):
        withdraw, record, _ = receivables.plan({REF: wanted()}, [held("p-1", REF), held("p-2", REF)], DATABASE, TODAY)
        self.assertEqual((withdraw, record), (["p-2"], []))

    def test_payments_this_database_did_not_make_are_left_alone(self):
        listing = [held("p-1", "Cash at the counter"), held("p-2", None), held("p-3", "odoo-other-db-rec-7"),
                   held("p-4", REF, withdrawn="2026-11-10T11:00:00Z")]
        withdraw, record, _ = receivables.plan({REF: wanted()}, listing, DATABASE, TODAY)
        self.assertEqual(withdraw, [])
        self.assertEqual(len(record), 1, "a withdrawn one does not count as held")

    def test_a_payment_dated_after_tomorrow_waits(self):
        withdraw, record, waiting = receivables.plan(
            {REF: wanted(paid_on="2026-11-13"), "odoo-db-0001-rec-8": wanted(paid_on="2026-11-12")}, [], DATABASE, TODAY)
        self.assertTrue(waiting)
        self.assertEqual([payment["reference"] for payment in record], ["odoo-db-0001-rec-8"])

    def test_a_reference_fits_what_efis_keeps(self):
        self.assertEqual(receivables.reference(DATABASE, "rec", 7), REF)
        with self.assertRaises(ValueError):
            receivables.reference("x" * 100, "rec", 7)

    def test_the_body_is_efis_published_shape(self):
        schemas = support.openapi()["components"]["schemas"]
        _, record, _ = receivables.plan({REF: wanted()}, [], DATABASE, TODAY)
        self.assertEqual(set(record[0]), set(schemas["PaymentReceivedRequest"]["properties"]))


class WantedTests(unittest.TestCase):
    def test_a_payment_and_a_bank_statement_line_count(self):
        env = environment()
        record = invoice(env, partials=[
            partial(7, 100.0, payment_entry(paid_on=date(2026, 11, 9))),
            partial(8, 20.0, payment_entry(paid_on=date(2026, 11, 10), bank=journal("Arka", "BANKNOTE"), statement=True)),
        ])
        result, problems = record._metanow_fiscal_wanted_payments()
        self.assertEqual(problems, [])
        self.assertEqual(result, {
            "odoo-db-0001-rec-7": {"amount": "100.00", "paidOn": "2026-11-09", "method": "ACCOUNT"},
            "odoo-db-0001-rec-8": {"amount": "20.00", "paidOn": "2026-11-10", "method": "BANKNOTE"},
        })

    def test_a_credit_note_or_a_manual_entry_is_not_money_received(self):
        env = environment()
        other = SimpleNamespace(origin_payment_id=False, statement_line_id=False, date=TODAY, journal_id=journal())
        result, problems = invoice(env, partials=[partial(9, 50.0, other)])._metanow_fiscal_wanted_payments()
        self.assertEqual((result, problems), ({}, []))

    def test_a_journal_without_a_known_method_is_a_problem(self):
        env = environment()
        for bank in (journal(method=False), journal(method="GOLD")):
            result, problems = invoice(env, partials=[partial(7, 100.0, payment_entry(bank=bank))])._metanow_fiscal_wanted_payments()
            self.assertEqual(result, {})
            self.assertEqual(len(problems), 1)

    def test_a_payment_without_a_journal_entry_counts_for_its_one_invoice(self):
        env = environment()
        record = invoice(env)
        paid = SimpleNamespace(id=5, name="PBNK1/2026/00005", move_id=False, state="paid", amount=240.0,
                               date=date(2026, 11, 10), journal_id=journal(), invoice_ids=Records([record]))
        draft = SimpleNamespace(id=6, name="draft", move_id=False, state="draft", amount=1.0,
                                date=TODAY, journal_id=journal(), invoice_ids=Records([record]))
        record.matched_payment_ids = Records([paid, draft])
        result, problems = record._metanow_fiscal_wanted_payments()
        self.assertEqual(problems, [])
        self.assertEqual(result, {"odoo-db-0001-pay-5": {"amount": "240.00", "paidOn": "2026-11-10", "method": "ACCOUNT"}})

    def test_a_payment_without_a_journal_entry_for_several_invoices_cannot_be_split(self):
        env = environment()
        record = invoice(env)
        other = SimpleNamespace(move_type="out_invoice")
        record.matched_payment_ids = Records([SimpleNamespace(
            id=5, name="PBNK1/2026/00005", move_id=False, state="paid", amount=240.0, date=TODAY,
            journal_id=journal(), invoice_ids=Records([record, other]))])
        result, problems = record._metanow_fiscal_wanted_payments()
        self.assertEqual(result, {})
        self.assertIn("cannot be split", problems[0])


class SyncTests(unittest.TestCase):
    def run_pass(self, record, *answers):
        opener = support.FakeOpener(*answers)
        owner = record.company_id
        owner._metanow_fiscal_client = lambda authenticated=True: efis_client.EfisClient(
            owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        record._metanow_fiscal_sync_payments(today=TODAY)
        return opener

    def paid(self, env, **overrides):
        return invoice(env, partials=[partial(7, 120.0, payment_entry())], **overrides)

    def test_efis_is_read_first_then_the_payment_is_recorded(self):
        env = environment()
        record = self.paid(env)
        opener = self.run_pass(record, (200, {"payments": []}), (201, held("p-1", REF)))
        self.assertEqual([(r["method"], r["url"]) for r in opener.requests], [
            ("GET", f"https://efis.example.invalid/v1/receivables/{DOCUMENT}/payments"),
            ("POST", f"https://efis.example.invalid/v1/receivables/{DOCUMENT}/payments"),
        ])
        self.assertEqual(opener.requests[1]["body"],
                         {"amount": "120.00", "paidOn": "2026-11-10", "method": "ACCOUNT", "reference": REF})
        self.assertNotIn("x-acting-user-id", opener.requests[1]["headers"])
        self.assertEqual(record.writes[-1], {"metanow_fiscal_payments_pending": False, "metanow_fiscal_payments_error": False})

    def test_a_lost_answer_never_records_twice(self):
        env = environment()
        record = self.paid(env)
        self.run_pass(record, (200, {"payments": []}), OSError("timed out"))
        self.assertTrue(record.metanow_fiscal_payments_pending, "tried again on the next pass")
        opener = self.run_pass(record, (200, {"payments": [held("p-1", REF)]}))
        self.assertEqual(len(opener.requests), 1, "efis had it; nothing is posted again")
        self.assertFalse(record.metanow_fiscal_payments_pending)

    def test_an_undone_payment_is_withdrawn(self):
        env = environment()
        record = invoice(env)
        opener = self.run_pass(record, (200, {"payments": [held("p-1", REF)]}), (200, held("p-1", REF, withdrawn="now")))
        self.assertEqual(opener.requests[1]["url"], "https://efis.example.invalid/v1/receivables/payments/p-1/withdraw")
        self.assertFalse(record.metanow_fiscal_payments_pending)

    def test_withdrawn_before_recorded(self):
        env = environment()
        record = invoice(env, partials=[partial(7, 100.0, payment_entry())])
        opener = self.run_pass(record, (200, {"payments": [held("p-1", REF)]}), (200, {}), (201, {}))
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in opener.requests[1:]], ["withdraw", "payments"])

    def test_a_refusal_is_never_retried(self):
        env = environment()
        record = self.paid(env)
        self.run_pass(record, (200, {"payments": []}),
                      (422, {"code": "payment_exceeds_outstanding", "retryable": False, "authorityCode": None}))
        self.assertFalse(record.metanow_fiscal_payments_pending)
        self.assertIn("still owed", record.metanow_fiscal_payments_error)

    def test_an_unknown_refusal_shows_efis_code(self):
        env = environment()
        record = self.paid(env)
        self.run_pass(record, (200, {"payments": []}), (422, {"code": "something_new"}))
        self.assertEqual(record.metanow_fiscal_payments_error, "efis: something_new")

    def test_efis_trouble_is_retried(self):
        env = environment()
        record = self.paid(env)
        self.run_pass(record, (503, None))
        self.assertTrue(record.metanow_fiscal_payments_pending)

    def test_odoo_setup_problems_stop_before_efis_is_called(self):
        env = environment()
        record = invoice(env, partials=[partial(7, 120.0, payment_entry(bank=journal(method=False)))])
        opener = self.run_pass(record)
        self.assertEqual(opener.requests, [])
        self.assertFalse(record.metanow_fiscal_payments_pending)
        self.assertIn("no efis payment method", record.metanow_fiscal_payments_error)

    def test_an_invoice_not_numbered_yet_waits(self):
        env = environment()
        record = self.paid(env, metanow_fiscal_document_id=False, metanow_fiscal_state=outcome.TO_SEND)
        opener = self.run_pass(record)
        self.assertEqual(opener.requests, [])
        self.assertTrue(record.metanow_fiscal_payments_pending)

    def test_a_refused_invoice_is_left_out(self):
        env = environment()
        record = self.paid(env, metanow_fiscal_state=outcome.REFUSED)
        self.assertEqual(self.run_pass(record).requests, [])
        self.assertFalse(record.metanow_fiscal_payments_pending)

    def test_nothing_happens_unless_pending_or_when_locked(self):
        env = environment()
        record = self.paid(env, metanow_fiscal_payments_pending=False)
        self.assertEqual(self.run_pass(record).requests, [])
        record = self.paid(env)
        env.cr.locked.add(record.id)
        self.assertEqual(self.run_pass(record).requests, [])

    def test_send_again_marks_the_invoice(self):
        env = environment()
        record = self.paid(env, metanow_fiscal_payments_pending=False)
        record.action_metanow_fiscal_send_payments()
        self.assertTrue(record.metanow_fiscal_payments_pending)
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"].count, 1)


class MarkTests(unittest.TestCase):
    def test_only_fiscalised_customer_invoices_are_marked(self):
        env = environment()
        env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"] = fake_odoo.Trigger()
        owner = company(env)
        target = move(env, owner)
        others = [move(env, owner, move_type="in_invoice"), move(env, owner, move_type="out_refund"),
                  move(env, company(env, metanow_fiscal_enabled=False))]
        moves = Records([target] + others)
        AccountMove._metanow_fiscal_mark_payments(_Marking(moves, env))
        self.assertTrue(target.metanow_fiscal_payments_pending)
        self.assertFalse(any(other.metanow_fiscal_payments_pending for other in others))
        self.assertEqual(env.refs["metanow_fiscalization.ir_cron_metanow_fiscal_payments"].count, 1)

    def test_the_hooks_mark_the_invoices_on_both_sides(self):
        marked = []
        moves = SimpleNamespace(_metanow_fiscal_mark_payments=lambda: marked.append("partial"))
        reconcile = AccountPartialReconcile(fake_odoo.Env())
        reconcile._metanow_fiscal_invoices = lambda: SimpleNamespace(exists=lambda: moves, **vars(moves))
        reconcile.unlink()
        self.assertEqual(marked, ["partial"])
        self.assertEqual(reconcile.base_calls, ["unlink"])

    def test_a_payment_without_an_entry_marks_its_invoices_when_its_state_changes(self):
        marked = []
        invoices = _Union(marked)
        payment = AccountPayment(fake_odoo.Env(), invoice_ids=invoices)
        payment.write({"state": "paid"})
        payment.write({"memo": "x"})
        self.assertEqual(marked, ["marked"])


class _Marking(Records):
    """The recordset _metanow_fiscal_mark_payments runs on, with the env and sudo it uses."""

    def __init__(self, records, env):
        super().__init__(records)
        self.env = env

    def filtered(self, predicate):
        return _Marking([record for record in self if predicate(record)], self.env)

    def sudo(self):
        return self

    def write(self, vals):
        for record in self:
            record.write(vals)

    def _metanow_fiscal_trigger(self, xml_id):
        self.env.ref(xml_id)._trigger()


class _Union:
    def __init__(self, marked):
        self.marked = marked

    def __or__(self, other):
        return self

    def _metanow_fiscal_mark_payments(self):
        self.marked.append("marked")


if __name__ == "__main__":
    unittest.main()
