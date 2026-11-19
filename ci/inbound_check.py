# DEV-2348: run inside `odoo shell` after ci/invoice_check.py, which left the first company
# sending e-invoices. A purchase e-invoice reaches a business the way a real one does -- issued by
# someone else, to this business's tax number -- and becomes a draft vendor bill it can answer
# (DEV-2433).
#
# The supplier is a second Odoo company paired to a second efis business, so the e-invoice is
# built, signed and registered by efis exactly as the buyer's own are. Nothing here reaches the
# authority except through the mock.
#
# Prints ODOO-INBOUND-PASSED on success; odoo-install.sh fails the job without that line.
import os
import time
from datetime import date

buyer_company = env.company  # noqa: F821 - provided by odoo shell
assert buyer_company.metanow_fiscal_credential, "the buying company is not paired with efis"
assert buyer_company.metanow_fiscal_einvoice_platform == "on", "this efis builds no e-invoices"
lek = buyer_company.currency_id

# The business turns received e-invoices on, and nothing arrives before the day it did: the
# module stamps that day itself, so turning it on never pulls in a year of history (DEV-2433).
buyer_company.metanow_fiscal_inbound_enabled = True
assert buyer_company.metanow_fiscal_inbound_since == date.today(), buyer_company.metanow_fiscal_inbound_since
env.cr.commit()  # noqa: F821

# ------------------------------------------------------------------ the supplier's company

supplier = env["res.company"].create({  # noqa: F821
    "name": "Furnitori sh.p.k. / The supplier",
    "country_id": env.ref("base.al").id,  # noqa: F821
    "currency_id": lek.id,
    "vat": os.environ["EFIS_SUPPLIER_TAX_ID"],
    "street": "Rruga e Elbasanit 3",
    "city": "Tiranë",
})
env.user.write({"company_ids": [(4, supplier.id)]})  # noqa: F821
env["account.chart.template"].with_company(supplier).try_loading(  # noqa: F821
    "generic_coa", company=supplier, install_demo=False)
# The chart of accounts brings its own currency with it, so the company's is set after loading:
# an invoice in anything but the company's own currency is not fiscalised by this module yet.
supplier.currency_id = lek.id
assert supplier.currency_id == lek, supplier.currency_id.name
journal = env["account.journal"].search(  # noqa: F821
    [("type", "=", "sale"), ("company_id", "=", supplier.id)], limit=1)
assert journal, "the supplier has no sales journal"

supplier.write({
    "metanow_fiscal_enabled": True,
    "metanow_fiscal_api_url": os.environ["EFIS_BASE_URL"],
    "metanow_fiscal_vat_status": "in_vat",
    "metanow_fiscal_einvoice": True,
    "metanow_fiscal_business_unit_code": os.environ["EFIS_BUSINESS_UNIT_CODE"],
    "metanow_fiscal_operator_code": os.environ["EFIS_OPERATOR_CODE"],
    "metanow_fiscal_software_code": os.environ["EFIS_SOFTWARE_CODE"],
})
supplier._metanow_fiscal_pair(os.environ["EFIS_SUPPLIER_PAIRING_CODE"])
assert supplier.metanow_fiscal_credential, "the supplier did not pair with efis"
assert supplier.metanow_fiscal_tenant != buyer_company.metanow_fiscal_tenant, "both companies paired as one business"
profile = supplier._metanow_fiscal_profile()
cashless = sorted(code for code in profile.codes("paymentMethods")
                  if not profile.flag("paymentMethods", code, "cash"))
journal.metanow_fiscal_payment_method = cashless[0]
env.cr.commit()  # noqa: F821

# ------------------------------------------------------------------ the supplier's e-invoice

# The buyer, as the supplier's books know it: a business named by its tax number, which is what
# makes the invoice an e-invoice.
customer = env["res.partner"].with_company(supplier).create({  # noqa: F821
    "name": buyer_company.name, "vat": buyer_company.vat, "is_company": True,
    "street": buyer_company.street, "city": buyer_company.city,
    "country_id": env.ref("base.al").id,  # noqa: F821
})
goods = env["product.product"].create({"name": "Lëndë e parë / Materials", "list_price": 900.0})  # noqa: F821
bought = env["account.move"].with_company(supplier).create({  # noqa: F821
    "move_type": "out_invoice",
    "partner_id": customer.id,
    "journal_id": journal.id,
    "currency_id": lek.id,
    "invoice_date": date.today().isoformat(),
    "invoice_date_due": date.today().replace(day=28).isoformat(),
    "invoice_line_ids": [(0, 0, {
        "product_id": goods.id, "quantity": 2, "price_unit": 900.0, "tax_ids": [(5, 0, 0)]})],
})
bought.action_post()
env.cr.commit()  # noqa: F821
bought._metanow_fiscal_send()
env.cr.commit()  # noqa: F821
assert bought.metanow_fiscal_electronic, (
    "the supplier's invoice did not go as an e-invoice", bought.metanow_fiscal_error)

deadline = time.time() + 180
while time.time() < deadline and not bought.metanow_fiscal_einvoice_eic:
    bought._metanow_fiscal_poll()
    env.cr.commit()  # noqa: F821
    bought.invalidate_recordset()
    if bought.metanow_fiscal_einvoice_error:
        break
    time.sleep(3)
assert not bought.metanow_fiscal_einvoice_error, bought.metanow_fiscal_einvoice_error
assert bought.metanow_fiscal_einvoice_eic, "the supplier's e-invoice has no EIC"
eic = bought.metanow_fiscal_einvoice_eic

# ------------------------------------------------------------------ the buyer's vendor bill

# efis asks the authority for the e-invoices this business bought, so the bill appears once that
# listing has run. The module's job then reads it from efis with its own credential.
Move = env["account.move"].with_company(buyer_company)  # noqa: F821
deadline = time.time() + 240  # efis asks the authority once a minute in this job
bill = Move.browse()
while time.time() < deadline and not bill:
    Move._cron_metanow_fiscal_inbound()
    env.cr.commit()  # noqa: F821
    bill = Move.search([("move_type", "=", "in_invoice"), ("company_id", "=", buyer_company.id),
                        ("metanow_fiscal_inbound_eic", "=", eic)], limit=1)
    if bill:
        break
    time.sleep(5)
assert bill, f"the purchase e-invoice {eic} never arrived as a vendor bill"

assert bill.state == "draft", "a received e-invoice became a posted bill on its own"
assert bill.metanow_fiscal_inbound_id, "the bill does not say which e-invoice in efis it came from"
assert bill.metanow_fiscal_inbound_seller == supplier.vat, bill.metanow_fiscal_inbound_seller
# What the authority holds, in its own words: the amount and the currency it carries.
assert bill.metanow_fiscal_inbound_amount == f"{bought.amount_total:.2f} {lek.name}", (
    bill.metanow_fiscal_inbound_amount, bought.amount_total)
assert bill.metanow_fiscal_inbound_status, "the bill does not say what the authority holds"
assert bill.partner_id.vat and bill.partner_id.vat.endswith(supplier.vat[1:]), (
    "the bill is not on the supplier's contact", bill.partner_id.name, bill.partner_id.vat)
# DEV-2462: the EIC is in the chatter too, so a reinstall can find it again.
assert any("efis" in (message.body or "") for message in bill.message_ids), "no chatter note from efis"

# The person answers it, and efis is told. A book review on the 11th declares it either way, so
# the answer is the one thing the bill is for.
assert bill.metanow_fiscal_inbound_waiting, "the bill is not waiting for an answer"
bill.action_metanow_fiscal_accept()
env.cr.commit()  # noqa: F821
bill.invalidate_recordset()
assert bill.metanow_fiscal_inbound_decision, "the answer was not recorded"
assert not bill.metanow_fiscal_inbound_waiting, "the bill still waits after it was answered"

# A draft bill has no number yet, so the e-invoice's own codes identify it in the job log.
print("ODOO-INBOUND-PASSED", eic, bill.metanow_fiscal_inbound_id)
