# DEV-1704: run inside `odoo shell` (which provides `env`) after ci/post_invoice.py paired the
# company. efis posts only to a public https address, which this job's Odoo does not have, so
# deliveries are not tested here (tests/test_webhooks.py checks them). What is checked against
# the real efis: the CI Odoo is not registered and says why; with a public address the module's
# own credential registers an endpoint and gets its secret; registering again disables the old
# endpoint; and the certificate's state is read from efis's health, with its To-Do.
# Prints ODOO-WEBHOOK-PASSED on success; odoo-install.sh fails the job without that line.
company = env.company  # noqa: F821 - provided by odoo shell
parameters = env["ir.config_parameter"].sudo()  # noqa: F821
assert company.metanow_fiscal_webhook_status and "public https" in company.metanow_fiscal_webhook_status, \
    company.metanow_fiscal_webhook_status
assert not company.metanow_fiscal_webhook_secret, "an Odoo efis cannot reach got a webhook secret"

base = parameters.get_str("web.base.url")
parameters.set_str("web.base.url", "https://odoo-ci.example.com")
try:
    assert company._metanow_fiscal_register_webhook(), company.metanow_fiscal_webhook_status
    first = company.metanow_fiscal_webhook_id
    assert first and company.sudo().metanow_fiscal_webhook_secret.startswith("whsec_"), "no secret from efis"
    assert company._metanow_fiscal_register_webhook(), company.metanow_fiscal_webhook_status
    second = company.metanow_fiscal_webhook_id
    assert second and second != first
    client = company._metanow_fiscal_client()
    listed = {item["id"]: item for item in client.list_webhooks().body["endpoints"]}
    assert listed[first]["disabledAt"], "registering again left the old endpoint on"
    assert not listed[second]["disabledAt"], listed[second]
    assert set(listed[second]["events"]) >= {"document.superseded", "document.corrected", "certificate.expiring"}
    # Leave nothing for efis to post to.
    client.disable_webhook(second)
finally:
    parameters.set_str("web.base.url", base)
env.cr.commit()  # noqa: F821

# The test certificate provision.py uploaded is valid for 30 days, which efis calls expiring.
env["account.move"]._cron_metanow_fiscal_health()  # noqa: F821
company.invalidate_recordset()
assert company.metanow_fiscal_certificate_status == "expiring", company.metanow_fiscal_certificate_status
assert company.metanow_fiscal_certificate_until, "no expiry date"
todos = env["mail.activity"].search([  # noqa: F821
    ("res_model", "=", "res.partner"), ("res_id", "=", company.partner_id.id),
    ("summary", "=", "efis: certifikata e nënshkrimit / signing certificate")])
assert todos, "no To-Do for the expiring certificate"
env.cr.commit()  # noqa: F821
print("ODOO-WEBHOOK-PASSED", company.metanow_fiscal_certificate_until, len(todos))
