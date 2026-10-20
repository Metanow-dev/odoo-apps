from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    # One of the codes efis's country profile lists under lines.exemptionReason, for a 0% tax
    # that is a VAT exemption. Empty for an ordinary rate.
    metanow_fiscal_exemption = fields.Char(
        string="Përjashtimi nga TVSH për efis / efis VAT exemption",
        help="Kodi i përjashtimit sipas profilit të vendit në efis. "
             "/ The exemption code from efis's country profile.",
    )
