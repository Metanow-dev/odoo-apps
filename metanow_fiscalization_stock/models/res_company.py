from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # DEV-2430: off until the business turns it on. efis also keeps transport invoices off in a
    # deployment until the authority's test host confirms their security code and numbering.
    metanow_fiscal_ti_enabled = fields.Boolean(
        string="Faturat e transportit / Transport invoices",
        help="Transferimet e operacioneve me transaksion fature transporti lëshojnë një faturë transporti. "
             "/ Transfers of operations with a transport invoice transaction issue a transport invoice.")
