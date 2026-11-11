from odoo import models


class AccountPayment(models.Model):
    _inherit = "account.payment"

    # DEV-2432: a payment Odoo records without a journal entry pays its invoices directly, with no
    # reconciliation. When it is confirmed, cancelled or moved to other invoices, those invoices
    # are sent to efis again.

    def write(self, vals):
        before = self.invoice_ids if "invoice_ids" in vals else None
        result = super().write(vals)
        if "state" in vals or "invoice_ids" in vals:
            invoices = self.invoice_ids if before is None else before | self.invoice_ids
            invoices._metanow_fiscal_mark_payments()
        return result
