# DEV-2443: run inside `odoo shell` (which provides `env`) after ci/post_invoice.py.
# Uninstalls metanow_fiscalization through Odoo's own uninstall dialog, as an administrator would.
# Asserts that the dialog warns, that it waits for the confirmation, and that once confirmed the
# uninstall goes ahead: it is never blocked.
# Prints ODOO-UNINSTALL-PASSED on success; odoo-install.sh fails the job without that line.
from odoo.exceptions import UserError

module = env["ir.module.module"].search([("name", "=", "metanow_fiscalization")])  # noqa: F821
assert module.state == "installed", module.state
wizard = env["base.module.uninstall"].create({"module_ids": [(6, 0, module.ids)]})  # noqa: F821
assert wizard.metanow_fiscal_warning, "no warning on a database with invoices sent to efis"
assert "Restore from efis" in wizard.metanow_fiscal_warning, wizard.metanow_fiscal_warning

try:
    wizard.action_uninstall()
except UserError:
    pass
else:
    raise AssertionError("the module was uninstalled without the confirmation")
env.cr.rollback()  # noqa: F821

wizard = env["base.module.uninstall"].create({"module_ids": [(6, 0, module.ids)], "metanow_fiscal_confirmed": True})  # noqa: F821
wizard.action_uninstall()
env.cr.commit()  # noqa: F821
module.invalidate_recordset()
assert module.state == "uninstalled", module.state
print("ODOO-UNINSTALL-PASSED")
