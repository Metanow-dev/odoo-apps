from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # One of the codes efis's country profile lists under buyer.identifierType. Empty means
    # efis's default, a domestic taxpayer number.
    metanow_fiscal_identifier_type = fields.Char(
        string="Lloji i identifikuesit për efis / efis identifier type",
        help="Bosh do të thotë numër tatimor vendas. / Empty means a domestic tax number.",
    )
