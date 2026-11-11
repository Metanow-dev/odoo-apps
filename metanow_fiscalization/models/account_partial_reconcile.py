from odoo import api, models


class AccountPartialReconcile(models.Model):
    _inherit = "account.partial.reconcile"

    # DEV-2432: a reconciliation made or undone changes what an invoice has been paid, so the
    # invoices on either side are sent to efis again. Nothing is called here; a job does it.

    def _metanow_fiscal_invoices(self):
        return (self.debit_move_id.move_id | self.credit_move_id.move_id)

    @api.model_create_multi
    def create(self, vals_list):
        partials = super().create(vals_list)
        partials._metanow_fiscal_invoices()._metanow_fiscal_mark_payments()
        return partials

    def unlink(self):
        invoices = self._metanow_fiscal_invoices()
        result = super().unlink()
        invoices.exists()._metanow_fiscal_mark_payments()
        return result
