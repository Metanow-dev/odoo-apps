from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    metanow_fiscal_enabled = fields.Boolean(related="company_id.metanow_fiscal_enabled", readonly=False)
    metanow_fiscal_api_url = fields.Char(related="company_id.metanow_fiscal_api_url", readonly=False)
    metanow_fiscal_tenant = fields.Char(related="company_id.metanow_fiscal_tenant")
    metanow_fiscal_channel = fields.Char(related="company_id.metanow_fiscal_channel")
    metanow_fiscal_business_unit_code = fields.Char(
        related="company_id.metanow_fiscal_business_unit_code", readonly=False)
    metanow_fiscal_operator_code = fields.Char(related="company_id.metanow_fiscal_operator_code", readonly=False)
    metanow_fiscal_software_code = fields.Char(related="company_id.metanow_fiscal_software_code", readonly=False)
    metanow_fiscal_device_code = fields.Char(related="company_id.metanow_fiscal_device_code", readonly=False)
    metanow_fiscal_vat_status = fields.Selection(related="company_id.metanow_fiscal_vat_status", readonly=False)
    # Typed here and exchanged at once. It is single-use, so it is not kept.
    metanow_fiscal_pairing_code = fields.Char(string="Kodi i çiftimit / Pairing code")

    def action_metanow_fiscal_pair(self):
        self.ensure_one()
        self.company_id._metanow_fiscal_pair(self.metanow_fiscal_pairing_code)
        self.metanow_fiscal_pairing_code = False
        return True

    def action_metanow_fiscal_refresh_profile(self):
        self.ensure_one()
        self.company_id._metanow_fiscal_refresh_profile()
        return True
