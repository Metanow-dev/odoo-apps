# DEV-2443 (decided 12 Nov 2026): on the upgrade to 20.0.1.1.0, every invoice efis registered
# before chatter notes existed gets one, so its values stay readable if the module is later
# uninstalled. Nothing is read from efis or sent to it. A new install has no such invoice.
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["account.move"]._metanow_fiscal_backfill_notes()
