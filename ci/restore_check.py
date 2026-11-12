# DEV-2443: run inside `odoo shell` (which provides `env`) after the module was uninstalled
# (ci/uninstall.py) and installed again. Sets the company up again, pairs it with a new code, and
# runs the restore. Checks K5 and K6 of the test checklist: the invoice ci/post_invoice.py
# fiscalised has its fiscal values and QR back, is locked again, and nothing new reached efis.
# Prints ODOO-RESTORE-PASSED on success; odoo-install.sh fails the job without that line.
import os

from odoo.exceptions import UserError

name, number, registration = os.environ["EFIS_FISCALISED"].split()

company = env.company  # noqa: F821 - provided by odoo shell
move = env["account.move"].search([("name", "=", name), ("company_id", "=", company.id)])  # noqa: F821
assert len(move) == 1, f"the invoice {name} is not in Odoo after the reinstall"
assert not move.metanow_fiscal_document_id and not move.metanow_fiscal_number, "the uninstall kept the fiscal fields"
# The bug DEV-2443 fixes: an emptied invoice is no longer locked.
assert not move._metanow_fiscal_locked(), "an emptied invoice is still locked before the restore"

company.write({
    "metanow_fiscal_enabled": True,
    "metanow_fiscal_api_url": os.environ["EFIS_BASE_URL"],
    "metanow_fiscal_vat_status": "not_in_vat",
    "metanow_fiscal_business_unit_code": os.environ["EFIS_BUSINESS_UNIT_CODE"],
    "metanow_fiscal_operator_code": os.environ["EFIS_OPERATOR_CODE"],
    "metanow_fiscal_software_code": os.environ["EFIS_SOFTWARE_CODE"],
})
company._metanow_fiscal_pair(os.environ["EFIS_PAIRING_CODE"])
assert company.metanow_fiscal_restore_pending, "pairing did not queue a restore"
client = company._metanow_fiscal_client()

listed = client.list_documents(limit=500)
assert listed.status == 200, listed
before = len(listed.body["documents"])

env["account.move"]._cron_metanow_fiscal_restore()  # noqa: F821
env.cr.commit()  # noqa: F821
move.invalidate_recordset()
company.invalidate_recordset()

assert not company.metanow_fiscal_restore_pending, company.metanow_fiscal_restore_summary
assert "Restored: 1" in company.metanow_fiscal_restore_summary, company.metanow_fiscal_restore_summary
# K5: every value is back, and the printout carries them.
assert move.metanow_fiscal_restored, move.metanow_fiscal_restore_note
assert move.metanow_fiscal_number == number, (move.metanow_fiscal_number, number)
assert move.metanow_fiscal_registration_code == registration, move.metanow_fiscal_registration_code
assert move.metanow_fiscal_security_code and move.metanow_fiscal_issued_at, "a fiscal value is missing"
assert move.metanow_fiscal_verification_url and move.metanow_fiscal_qr_src, "the QR is missing"
assert move.metanow_fiscal_state == "registered", move.metanow_fiscal_state
assert any("Restored from efis" in (message.body or "") for message in move.message_ids), "no chatter note"
pdf, _ = env["ir.actions.report"]._render_qweb_pdf("account.account_invoices", move.ids)  # noqa: F821
assert pdf, "the invoice report did not render"

# K6: locked again, and nothing new reached efis.
try:
    move.button_draft()
except UserError:
    pass
else:
    raise AssertionError("a restored invoice went back to draft")
env.cr.rollback()  # noqa: F821
after = client.list_documents(limit=500)
assert after.status == 200 and len(after.body["documents"]) == before, "the restore sent something to efis"

# Running it again changes nothing.
company._metanow_fiscal_restore_request()
env["account.move"]._cron_metanow_fiscal_restore()  # noqa: F821
move.invalidate_recordset()
assert move.metanow_fiscal_number == number
print("ODOO-RESTORE-PASSED", move.name, move.metanow_fiscal_number)
