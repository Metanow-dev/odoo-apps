from odoo import fields, models

from .account_move import CORRECTION_KINDS


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    # DEV-2431: chosen when the credit note is made, because "Reverse and Create Invoice" posts
    # the credit note in the same step and there is no draft to choose it on. Nothing is
    # preselected.
    metanow_fiscal_correction_kind = fields.Selection(
        CORRECTION_KINDS, string="Nota e kreditit për efis / Credit note for efis")
    metanow_fiscal_enabled = fields.Boolean(related="company_id.metanow_fiscal_enabled")

    def _prepare_default_reversal(self, move):
        values = super()._prepare_default_reversal(move)
        if move.move_type == "out_invoice" and self.metanow_fiscal_correction_kind:
            values["metanow_fiscal_correction_kind"] = self.metanow_fiscal_correction_kind
        return values
