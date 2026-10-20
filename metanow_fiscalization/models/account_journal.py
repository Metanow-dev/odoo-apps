from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    # One of the codes efis's country profile lists under paymentMethods, checked against the
    # profile when an invoice is posted. Not a list written into this module.
    metanow_fiscal_payment_method = fields.Char(
        string="Mënyra e pagesës për efis / efis payment method",
        help="Kodi i mënyrës së pagesës sipas profilit të vendit në efis. "
             "/ The payment method code from efis's country profile.",
    )
