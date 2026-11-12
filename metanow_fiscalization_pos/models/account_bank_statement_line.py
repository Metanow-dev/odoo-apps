from odoo import fields, models

from . import deposit


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    # DEV-2437: a Point of Sale cash in or out, declared to efis as DEPOSIT or WITHDRAW.
    metanow_fiscal_deposit_state = fields.Selection(
        deposit.STATES, string="Në efis / In efis", copy=False, readonly=True, index=True)
    metanow_fiscal_deposit_document_id = fields.Char(copy=False, readonly=True)
    metanow_fiscal_deposit_registration_code = fields.Char(copy=False, readonly=True)
    metanow_fiscal_deposit_key = fields.Char(copy=False, readonly=True)
    metanow_fiscal_deposit_request = fields.Text(copy=False, readonly=True)
    metanow_fiscal_deposit_error = fields.Text(copy=False, readonly=True)
