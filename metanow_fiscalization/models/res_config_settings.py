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
    metanow_fiscal_einvoice = fields.Boolean(related="company_id.metanow_fiscal_einvoice", readonly=False)
    # Typed here and exchanged when the settings are saved. It is single-use, so it is not kept.
    metanow_fiscal_pairing_code = fields.Char(string="Kodi i çiftimit / Pairing code")

    def set_values(self):
        # Odoo's settings page saves, and then reloads, before it runs any other button, so the
        # code would be gone by the time a separate pair action ran. It is exchanged on save,
        # after the rest is written, so a URL typed with it is already in place.
        super().set_values()
        for settings in self:
            if settings.metanow_fiscal_pairing_code:
                settings.company_id._metanow_fiscal_pair(settings.metanow_fiscal_pairing_code)
                settings.metanow_fiscal_pairing_code = False

    def action_metanow_fiscal_refresh_profile(self):
        self.ensure_one()
        self.company_id._metanow_fiscal_refresh_profile()
        return True
