# DEV-2348: run inside `odoo shell` after ci/post_invoice.py, which paired the company and left
# one registered invoice. Walks the rest of an invoice's life with efis as a customer would:
#
# * a refusal the person has to act on, in words rather than a code (DEV-2223),
# * a cash invoice, from its device to the authority's registration (DEV-2448),
# * an invoice to a registered business sent as an e-invoice, with its EIC (DEV-2358, DEV-2451),
# * a credit note sent as a correction of the invoice it reverses (DEV-2431),
# * the payments efis tracks for the invoice, recorded and withdrawn again (DEV-2432).
#
# Prints ODOO-INVOICE-PASSED on success; odoo-install.sh fails the job without that line.
import os
import time
from datetime import date, timedelta

from odoo.exceptions import UserError

company = env.company  # noqa: F821 - provided by odoo shell
assert company.metanow_fiscal_credential, "the company is not paired with efis; post_invoice.py runs first"
lek = company.currency_id
profile = company._metanow_fiscal_profile()
assert profile is not None, "no country profile"

sales = env["account.journal"].search(  # noqa: F821
    [("type", "=", "sale"), ("company_id", "=", company.id)], limit=1)
assert sales, "the company has no sales journal"
bank = env["account.journal"].search(  # noqa: F821
    [("type", "in", ("bank", "cash")), ("company_id", "=", company.id)], limit=1)
assert bank, "the company has no bank journal"
# Whichever non-cash method efis's profile lists first, as ci/post_invoice.py chooses it: the
# uninstall this check runs after took the journals' methods with it.
cashless = sorted(code for code in profile.codes("paymentMethods")
                  if not profile.flag("paymentMethods", code, "cash"))
assert cashless, "the profile lists no non-cash payment method"
(sales + bank).write({"metanow_fiscal_payment_method": cashless[0]})

buyer = env["res.partner"].create({  # noqa: F821
    "name": "Blerës i regjistruar sh.p.k.", "street": "Rruga 7", "city": "Tiranë",
    "vat": "L91234567K", "country_id": env.ref("base.al").id,  # noqa: F821
})
product = env["product.product"].create({"name": "Tryeza", "list_price": 1000.0, "taxes_id": [(5, 0, 0)]})  # noqa: F821


def refused(what):
    """A block that must raise UserError, inside a savepoint so nothing it touched is kept.

    A plain rollback would take the records this check made since its last commit with it, which
    is how the credit note used to disappear halfway through (DEV-2348).
    """
    class Refused:
        def __enter__(self):
            self.savepoint = env.cr.savepoint()  # noqa: F821
            self.savepoint.__enter__()
            return self

        def __exit__(self, kind, error, traceback):
            if kind is None:
                self.savepoint.__exit__(None, None, None)
                raise AssertionError(what)
            if issubclass(kind, UserError):
                self.said = str(error)
                self.savepoint.__exit__(kind, error, traceback)
                return True
            return False
    return Refused()


def invoice(amount, journal=sales, partner=buyer, due=None):
    values = {
        "move_type": "out_invoice",
        "partner_id": partner.id,
        "journal_id": journal.id,
        "currency_id": lek.id,
        "invoice_line_ids": [(0, 0, {
            "product_id": product.id, "quantity": 1, "price_unit": amount, "tax_ids": [(5, 0, 0)]})],
    }
    if due is not None:
        values["invoice_date"] = date.today().isoformat()
        values["invoice_date_due"] = due.isoformat()
    return env["account.move"].create(values)  # noqa: F821


def registered(move, what):
    """Post the invoice if it is still a draft, send it, and follow it until registered."""
    if move.state == "draft":
        move.action_post()
        env.cr.commit()  # noqa: F821
        move.invalidate_recordset()
        # Queued, or already sent: the job that runs straight after may have taken it.
        assert move.metanow_fiscal_state in ("to_send", "sent", "registered"), (
            what, move.metanow_fiscal_state, move.metanow_fiscal_error)
    move._metanow_fiscal_send()  # nothing to do unless it is still queued
    env.cr.commit()  # noqa: F821
    assert move.metanow_fiscal_security_code, f"{what}: {move.metanow_fiscal_error}"
    deadline = time.time() + 180
    while time.time() < deadline and not move.metanow_fiscal_registration_code:
        move._metanow_fiscal_poll()
        env.cr.commit()  # noqa: F821
        move.invalidate_recordset()
        if move.metanow_fiscal_state in ("rejected", "parked"):
            break
        time.sleep(3)
    assert move.metanow_fiscal_state == "registered", (what, move.metanow_fiscal_state, move.metanow_fiscal_error)
    return move


# ------------------------------------------------------------------ a refusal, in words

# A cash invoice between two taxpayers is capped (Udhëzim 16; efis's cash ceiling). Whether the
# module refuses it before posting or efis refuses it before numbering, what matters here is that
# the person is told in words, and that no number was spent.
cash = env["account.journal"].search(  # noqa: F821
    [("code", "=", "CSH20"), ("company_id", "=", company.id)], limit=1) or env["account.journal"].create({  # noqa: F821
        "name": "Arka / Cash sales", "code": "CSH20", "type": "sale", "company_id": company.id,
        "currency_id": lek.id,
    })
methods = sorted(code for code in profile.codes("paymentMethods") if profile.flag("paymentMethods", code, "cash"))
assert methods, "the profile lists no cash payment method"
cash.write({
    "metanow_fiscal_payment_method": methods[0],
    "metanow_fiscal_device_code": os.environ["EFIS_DEVICE_CODE"],
})
env.cr.commit()  # noqa: F821
over = invoice(150000.0, journal=cash)
env.cr.commit()  # noqa: F821
try:
    with refused("") as outcome:
        over.action_post()
    said = outcome.said
except AssertionError:
    # efis refused it instead, after it was posted: the invoice is left refused, with no number.
    env.cr.commit()  # noqa: F821
    over._metanow_fiscal_send()
    env.cr.commit()  # noqa: F821
    over.invalidate_recordset()
    said = over.metanow_fiscal_error or ""
    assert over.metanow_fiscal_state == "refused", (over.metanow_fiscal_state, said)
assert not over.metanow_fiscal_number, "a refused invoice spent a number"
assert not over.metanow_fiscal_security_code, "a refused invoice was sealed"
# efis's own sentence, not just its code: the person has to know what to change (DEV-2223).
assert len(said.split()) > 3, f"the refusal says too little: {said!r}"
assert "100000" in said, f"the refusal does not name the cash ceiling: {said!r}"

# A cash invoice under the ceiling goes all the way: numbered on its device, sent inside efis's
# own stamp window, and registered (DEV-2448, which held every cash invoice before it was fixed).
under = registered(invoice(1200.0, journal=cash), "the cash invoice")
assert under.metanow_fiscal_verification_url and "cr=" in under.metanow_fiscal_verification_url, (
    "a cash invoice's verification URL names the device it was issued on")
# A cash invoice is never an e-invoice: efis refuses one paid in cash.
assert not under.metanow_fiscal_electronic, "a cash invoice went out as an e-invoice"

# ------------------------------------------------------------------ an invoice as an e-invoice

# DEV-2358: a non-cash invoice to a buyer named by a domestic tax number goes to that buyer as an
# e-invoice, while the company wants them and efis can build them (DEV-2451).
company.metanow_fiscal_einvoice = True
company._metanow_fiscal_refresh_einvoice_platform()
assert company.metanow_fiscal_einvoice_platform == "on", (
    "this efis builds no e-invoices; EFIS_EINVOICE_ENDPOINT is unset in the compose file")
assert company._metanow_fiscal_einvoice_on()
env.cr.commit()  # noqa: F821

electronic = registered(
    invoice(3000.0, due=date.today() + timedelta(days=15)), "the e-invoice")
assert electronic.metanow_fiscal_electronic, "the invoice did not go as an e-invoice"
deadline = time.time() + 180
while time.time() < deadline and not electronic.metanow_fiscal_einvoice_eic:
    electronic._metanow_fiscal_poll()
    env.cr.commit()  # noqa: F821
    electronic.invalidate_recordset()
    if electronic.metanow_fiscal_einvoice_error:
        break
    time.sleep(3)
assert not electronic.metanow_fiscal_einvoice_error, electronic.metanow_fiscal_einvoice_error
assert electronic.metanow_fiscal_einvoice_eic, (
    "the authority's e-invoice code never came back", electronic.metanow_fiscal_einvoice_state)
assert electronic.metanow_fiscal_einvoice_state, "the e-invoice has no state"
# The fiscal invoice's own codes are untouched by the e-invoice's.
assert electronic.metanow_fiscal_einvoice_eic != electronic.metanow_fiscal_registration_code

# A buyer efis cannot name by a domestic tax number gets a plain fiscal invoice instead.
person = env["res.partner"].create({  # noqa: F821
    "name": "Blerës privat", "street": "Rruga 9", "city": "Tiranë", "country_id": env.ref("base.al").id})  # noqa: F821
plain = registered(invoice(400.0, partner=person), "the invoice to a person")
assert not plain.metanow_fiscal_electronic, "an invoice to a buyer with no tax number went as an e-invoice"
assert not plain.metanow_fiscal_einvoice_eic

# ------------------------------------------------------------------ a credit note as a correction

first = env["account.move"].search([  # noqa: F821
    ("move_type", "=", "out_invoice"), ("company_id", "=", company.id),
    ("metanow_fiscal_state", "=", "registered")], order="id", limit=1)
assert first, "post_invoice.py leaves one registered invoice"

reversal = env["account.move.reversal"].with_context(  # noqa: F821
    active_model="account.move", active_ids=first.ids).create({
        "move_ids": [(6, 0, first.ids)], "journal_id": first.journal_id.id, "reason": "Gabim / Mistake"})
reversal.reverse_moves()
note = first.reversal_move_ids[-1] if first.reversal_move_ids else env["account.move"].search(  # noqa: F821
    [("reversed_entry_id", "=", first.id)], order="id desc", limit=1)
assert note and note.move_type == "out_refund", "the credit note was not created"

# The person says what the credit note is: efis keeps a mistake and a return apart, and nothing
# is preselected (DEV-2431).
assert not note.metanow_fiscal_correction_kind, "a kind of correction was preselected"
env.cr.commit()  # noqa: F821
with refused("a credit note was posted without saying what it corrects") as outcome:
    note.action_post()
assert "korrigjon" in outcome.said or "mistake" in outcome.said.lower(), outcome.said
note.invalidate_recordset()
assert note.state == "draft", note.state

note.metanow_fiscal_correction_kind = "corrective"
note.action_post()
env.cr.commit()  # noqa: F821
assert note.metanow_fiscal_state == "to_send", note.metanow_fiscal_state
registered(note, "the credit note")
assert note.metanow_fiscal_number and note.metanow_fiscal_number != first.metanow_fiscal_number
assert note.metanow_fiscal_security_code != first.metanow_fiscal_security_code

# The invoice it corrects keeps its own number and code, and efis says it was superseded.
first.invalidate_recordset()
deadline = time.time() + 60
while time.time() < deadline and first.metanow_fiscal_state != "superseded":
    first._metanow_fiscal_poll()
    env.cr.commit()  # noqa: F821
    first.invalidate_recordset()
    time.sleep(3)
assert first.metanow_fiscal_state == "superseded", (first.metanow_fiscal_state, first.metanow_fiscal_efis_state)
assert first.metanow_fiscal_number == "1/2026", first.metanow_fiscal_number

# ------------------------------------------------------------------ the payments efis tracks

paid = registered(
    invoice(2000.0, due=date.today() + timedelta(days=30)), "the invoice to be paid")
client = company._metanow_fiscal_client()


def held():
    answer = client.list_payments_received(paid.metanow_fiscal_document_id)
    assert answer.status == 200, (answer.status, answer.body)
    return [payment for payment in (answer.body or {}).get("payments") or [] if not payment.get("withdrawnAt")]


assert held() == [], "efis holds a payment on an invoice nobody paid"

register = env["account.payment.register"].with_context(  # noqa: F821
    active_model="account.move", active_ids=paid.ids).create({"journal_id": bank.id})
register.action_create_payments()
env.cr.commit()  # noqa: F821
assert paid.metanow_fiscal_payments_pending, "a payment Odoo recorded is not waiting for efis"
env["account.move"]._cron_metanow_fiscal_payments()  # noqa: F821
env.cr.commit()  # noqa: F821
paid.invalidate_recordset()
assert not paid.metanow_fiscal_payments_error, paid.metanow_fiscal_payments_error
assert not paid.metanow_fiscal_payments_pending, "the payment is still waiting for efis"
recorded = held()
assert len(recorded) == 1, recorded
assert recorded[0]["amount"] == f"{paid.amount_total:.2f}", (recorded[0], paid.amount_total)
assert recorded[0]["reference"].startswith("odoo-"), recorded[0]

# A payment undone in Odoo is withdrawn in efis, never deleted, and the invoice is owed again.
partials = paid.line_ids.filtered(
    lambda line: line.account_id.account_type == "asset_receivable").matched_credit_ids
assert partials, "the payment did not reconcile"
paid.js_remove_outstanding_partial(partials[0].id)
env.cr.commit()  # noqa: F821
assert paid.metanow_fiscal_payments_pending, "undoing a payment is not waiting for efis"
env["account.move"]._cron_metanow_fiscal_payments()  # noqa: F821
env.cr.commit()  # noqa: F821
paid.invalidate_recordset()
assert not paid.metanow_fiscal_payments_error, paid.metanow_fiscal_payments_error
assert held() == [], "efis still holds a payment Odoo no longer has"

print("ODOO-INVOICE-PASSED", note.metanow_fiscal_number, paid.metanow_fiscal_number)
