from odoo import fields, models


class ResCountry(models.Model):
    _inherit = "res.country"

    # The country as efis's profile lists it under buyer.countryCode. Odoo's own code is not
    # used: the format the authority expects is not in the research, so it is configured.
    metanow_fiscal_country_code = fields.Char(
        string="Kodi i vendit për efis / efis country code",
    )
