# DEV-1731: run inside `odoo shell` (which provides `env`) after metanow_fiscalization is installed.
# Pairs the company with efis, posts one invoice, sends it, and reads it until the mock authority
# has registered it. Asserts that every answer from efis was stored by exactly one write carrying
# every fiscal field together, and that a sent invoice cannot go back to draft.
# Prints ODOO-INSTALL-PASSED on success; odoo-install.sh fails the job without that line.
import os
import time

from odoo.exceptions import UserError

FISCAL = {
    "metanow_fiscal_document_id", "metanow_fiscal_state", "metanow_fiscal_efis_state",
    "metanow_fiscal_number", "metanow_fiscal_security_code", "metanow_fiscal_registration_code",
    "metanow_fiscal_verification_url", "metanow_fiscal_issued_at",
}
ANSWER = {"metanow_fiscal_state", "metanow_fiscal_efis_state", "metanow_fiscal_error"}

company = env.company  # noqa: F821 - provided by odoo shell
lek = env.ref("base.ALL")  # noqa: F821
lek.active = True
company.write({
    "country_id": env.ref("base.al").id,  # noqa: F821
    "currency_id": lek.id,
    "vat": os.environ["EFIS_TAX_ID"],
    "street": "Rruga e Durrësit 1",
    "city": "Tiranë",
})
journal = env["account.journal"].search([("type", "=", "sale"), ("company_id", "=", company.id)], limit=1)  # noqa: F821
if not journal:
    env["account.chart.template"].try_loading("generic_coa", company=company, install_demo=False)  # noqa: F821
    journal = env["account.journal"].search([("type", "=", "sale"), ("company_id", "=", company.id)], limit=1)  # noqa: F821
assert journal, "no sales journal"

company.write({
    "metanow_fiscal_enabled": True,
    "metanow_fiscal_api_url": os.environ["EFIS_BASE_URL"],
    "metanow_fiscal_vat_status": "not_in_vat",
    "metanow_fiscal_business_unit_code": os.environ["EFIS_BUSINESS_UNIT_CODE"],
    "metanow_fiscal_operator_code": os.environ["EFIS_OPERATOR_CODE"],
    "metanow_fiscal_software_code": os.environ["EFIS_SOFTWARE_CODE"],
})
company._metanow_fiscal_pair(os.environ["EFIS_PAIRING_CODE"])
profile = company._metanow_fiscal_profile()
assert profile is not None, "no country profile"
# Whichever non-cash method efis's profile lists first: the job takes it from efis, not from here.
methods = sorted(code for code in profile.codes("paymentMethods") if not profile.flag("paymentMethods", code, "cash"))
assert methods, "the profile lists no non-cash payment method"
journal.metanow_fiscal_payment_method = methods[0]

partner = env["res.partner"].create({"name": "Blerës CI", "street": "Rruga 2", "city": "Durrës"})  # noqa: F821
product = env["product.product"].create({"name": "Karrige", "list_price": 99.01, "taxes_id": [(5, 0, 0)]})  # noqa: F821
move = env["account.move"].create({  # noqa: F821
    "move_type": "out_invoice",
    "partner_id": partner.id,
    "journal_id": journal.id,
    "currency_id": lek.id,
    "invoice_line_ids": [(0, 0, {"product_id": product.id, "quantity": 1, "price_unit": 99.01, "tax_ids": [(5, 0, 0)]})],
})

Move = env.registry["account.move"]  # noqa: F821
original_write = Move.write
answers = []


def counting_write(self, vals):
    if FISCAL & set(vals) or ANSWER & set(vals):
        answers.append(set(vals))
    return original_write(self, vals)


Move.write = counting_write
try:
    move.action_post()
    env.cr.commit()  # noqa: F821
    assert move.metanow_fiscal_state == "to_send", move.metanow_fiscal_state
    answers.clear()

    move._metanow_fiscal_send()
    env.cr.commit()  # noqa: F821
    assert len(answers) == 1, f"the submission answer took {len(answers)} writes: {answers}"
    assert {"metanow_fiscal_document_id", "metanow_fiscal_security_code", "metanow_fiscal_state"} <= answers[0], answers
    assert move.metanow_fiscal_security_code, move.metanow_fiscal_error

    deadline = time.time() + 180
    while time.time() < deadline and not move.metanow_fiscal_registration_code:
        before = len(answers)
        move._metanow_fiscal_poll()
        env.cr.commit()  # noqa: F821
        assert len(answers) - before <= 1, "one poll answer took more than one write"
        move.invalidate_recordset()
        if move.metanow_fiscal_state in ("rejected", "parked"):
            break
        time.sleep(3)
finally:
    Move.write = original_write

assert move.metanow_fiscal_state == "registered", (move.metanow_fiscal_state, move.metanow_fiscal_error)
assert move.metanow_fiscal_registration_code
assert move.metanow_fiscal_verification_url

try:
    move.button_draft()
except UserError:
    pass
else:
    raise AssertionError("a fiscalised invoice went back to draft")
env.cr.rollback()  # noqa: F821

pdf, _ = env["ir.actions.report"]._render_qweb_pdf("account.account_invoices", move.ids)  # noqa: F821
assert pdf, "the invoice report did not render"
print("ODOO-INSTALL-PASSED", move.name, move.metanow_fiscal_number, move.metanow_fiscal_registration_code)
