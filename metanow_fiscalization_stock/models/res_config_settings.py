from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    metanow_fiscal_ti_enabled = fields.Boolean(related="company_id.metanow_fiscal_ti_enabled", readonly=False)
