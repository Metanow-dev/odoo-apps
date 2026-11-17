from odoo import api, fields, models
from odoo.exceptions import UserError

from ..efis import registry as efis_registry
from .res_company import COMPANY_CODES

# DEV-2436: each company code is chosen from a list of the codes efis has confirmed. The pick
# list is the settings field; the company keeps the code as text, as before.
PICKS = tuple((f"{name}_pick", name, kind) for name, kind in COMPANY_CODES)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    metanow_fiscal_enabled = fields.Boolean(related="company_id.metanow_fiscal_enabled", readonly=False)
    metanow_fiscal_api_url = fields.Char(related="company_id.metanow_fiscal_api_url", readonly=False)
    metanow_fiscal_tenant = fields.Char(related="company_id.metanow_fiscal_tenant")
    metanow_fiscal_channel = fields.Char(related="company_id.metanow_fiscal_channel")
    metanow_fiscal_business_unit_code_pick = fields.Selection(
        selection="_metanow_fiscal_business_unit_choices", string="Kodi i njësisë së biznesit / Business unit code")
    metanow_fiscal_operator_code_pick = fields.Selection(
        selection="_metanow_fiscal_operator_choices", string="Kodi i operatorit / Operator code")
    metanow_fiscal_software_code_pick = fields.Selection(
        selection="_metanow_fiscal_software_choices", string="Kodi i programit / Software code")
    metanow_fiscal_device_code_pick = fields.Selection(
        selection="_metanow_fiscal_device_choices", string="Kodi i pajisjes / Device code",
        help="Vetëm për faturat me para në dorë. / Only for cash invoices.")
    metanow_fiscal_registry_notice = fields.Text(compute="_compute_metanow_fiscal_registry_notice")
    metanow_fiscal_vat_status = fields.Selection(related="company_id.metanow_fiscal_vat_status", readonly=False)
    metanow_fiscal_einvoice = fields.Boolean(related="company_id.metanow_fiscal_einvoice", readonly=False)
    metanow_fiscal_inbound_enabled = fields.Boolean(related="company_id.metanow_fiscal_inbound_enabled", readonly=False)
    metanow_fiscal_inbound_since = fields.Date(related="company_id.metanow_fiscal_inbound_since", readonly=False)
    # Typed here and exchanged when the settings are saved. It is single-use, so it is not kept.
    metanow_fiscal_pairing_code = fields.Char(string="Kodi i çiftimit / Pairing code")
    # DEV-2443
    metanow_fiscal_restore_pending = fields.Boolean(related="company_id.metanow_fiscal_restore_pending")
    metanow_fiscal_restore_summary = fields.Text(related="company_id.metanow_fiscal_restore_summary")

    # DEV-1704
    metanow_fiscal_webhook_status = fields.Text(related="company_id.metanow_fiscal_webhook_status")
    metanow_fiscal_certificate_notice = fields.Text(compute="_compute_metanow_fiscal_certificate_notice")

    @api.depends("company_id")
    def _compute_metanow_fiscal_certificate_notice(self):
        for settings in self:
            settings.metanow_fiscal_certificate_notice = settings.company_id._metanow_fiscal_certificate_notice() or False

    def action_metanow_fiscal_register_webhook(self):
        """Ask efis again to post its events here, after the Odoo address changed, for example."""
        self.ensure_one()
        if not self.company_id.metanow_fiscal_tenant:
            raise UserError("Çiftoni kompaninë me efis më parë. / Pair the company with efis first.")
        self.company_id._metanow_fiscal_register_webhook()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_metanow_fiscal_restore(self):
        """Read the fiscal values back from efis into the invoices that lost them. Only reads."""
        self.ensure_one()
        if not self.company_id.metanow_fiscal_tenant:
            raise UserError("Çiftoni kompaninë me efis më parë. / Pair the company with efis first.")
        self.company_id._metanow_fiscal_restore_request()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_metanow_fiscal_restore_send_payments(self):
        """Send the payments of every restored invoice a person has now reviewed."""
        self.ensure_one()
        moves = self.env["account.move"].search([
            ("company_id", "=", self.company_id.id), ("metanow_fiscal_payments_review", "=", True)])
        moves.action_metanow_fiscal_send_payments()
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_metanow_fiscal_restore_skipped(self):
        """The invoices the restore left alone, each with the reason on it."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Rikthimi nga efis / Restore from efis",
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id), ("metanow_fiscal_restore_note", "!=", False)],
        }

    def _metanow_fiscal_own_choices(self, kind):
        # The settings edit the current company, so the list is that company's alone.
        registry = self.env.company._metanow_fiscal_registry()
        return registry.choices(kind) if registry is not None else []

    @api.model
    def _metanow_fiscal_business_unit_choices(self):
        return self._metanow_fiscal_own_choices(efis_registry.BUSINESS_UNIT)

    @api.model
    def _metanow_fiscal_operator_choices(self):
        return self._metanow_fiscal_own_choices(efis_registry.OPERATOR)

    @api.model
    def _metanow_fiscal_software_choices(self):
        return self._metanow_fiscal_own_choices(efis_registry.SOFTWARE)

    @api.model
    def _metanow_fiscal_device_choices(self):
        return self._metanow_fiscal_own_choices(efis_registry.DEVICE)

    @staticmethod
    def _metanow_fiscal_shown(company, registry, name, kind):
        """What a pick list shows for a stored code: the code if efis confirms it, else nothing."""
        code = company[name]
        return code if registry is not None and registry.confirms(kind, code) else False

    @api.model
    def get_values(self):
        values = super().get_values()
        company = self.env.company
        registry = company._metanow_fiscal_registry()
        for pick, name, kind in PICKS:
            values[pick] = self._metanow_fiscal_shown(company, registry, name, kind)
        return values

    @api.depends("company_id")
    def _compute_metanow_fiscal_registry_notice(self):
        for settings in self:
            settings.metanow_fiscal_registry_notice = settings._metanow_fiscal_registry_notice() or False

    def _metanow_fiscal_registry_notice(self):
        company = self.company_id
        if not company.metanow_fiscal_tenant:
            return None
        registry = company._metanow_fiscal_registry()
        if registry is None:
            return ("Kodet nuk janë lexuar ende nga efis; shtypni \"Kontrollo konfigurimin\". "
                    "/ The codes have not been read from efis yet; press \"Check the configuration\".")
        lines = [
            f"{efis_registry.LABELS[kind]}: {company[name]}"
            for _, name, kind in PICKS
            if company[name] and not registry.confirms(kind, company[name])
        ]
        if not lines:
            return None
        return ("Këto kode të ruajtura nuk janë konfirmuar në efis; zgjidhni një kod të konfirmuar. "
                "/ These stored codes are not confirmed in efis; choose a confirmed code.\n" + "\n".join(lines))

    def set_values(self):
        # Odoo's settings page saves, and then reloads, before it runs any other button, so the
        # code would be gone by the time a separate pair action ran. It is exchanged on save,
        # after the rest is written, so a URL typed with it is already in place.
        super().set_values()
        for settings in self:
            settings._metanow_fiscal_save_codes()
            if settings.metanow_fiscal_pairing_code:
                settings.company_id._metanow_fiscal_pair(settings.metanow_fiscal_pairing_code)
                settings.metanow_fiscal_pairing_code = False

    def _metanow_fiscal_save_codes(self):
        """Write a code the person changed in a pick list, and only that.

        A stored code efis does not confirm shows as an empty pick list. Saving the page leaves it
        as it is, so a code typed before DEV-2436, or one efis has since retired, is never cleared
        without anyone choosing to. Until the company has read its registry, nothing is written.
        """
        company = self.company_id
        registry = company._metanow_fiscal_registry()
        if registry is None:
            return
        changes = {}
        for pick, name, kind in PICKS:
            chosen = self[pick] or False
            if chosen != self._metanow_fiscal_shown(company, registry, name, kind):
                changes[name] = chosen
        if changes:
            company.write(changes)

    def action_metanow_fiscal_refresh_profile(self):
        self.ensure_one()
        self.company_id._metanow_fiscal_refresh_profile()
        if not self.company_id.metanow_fiscal_tenant:
            return True
        self.company_id._metanow_fiscal_refresh_registry()
        # The pick lists are built when the page loads, so it reloads to offer the codes just read.
        return {"type": "ir.actions.client", "tag": "reload"}
