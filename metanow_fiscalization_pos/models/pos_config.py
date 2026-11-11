from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.metanow_fiscalization.efis import registry as efis_registry


class PosConfig(models.Model):
    _inherit = "pos.config"

    # DEV-2437: the fiscal device whose cash this point of sale moves, picked from the codes efis
    # confirmed. Empty: this point of sale declares no cash to efis.
    metanow_fiscal_device_code = fields.Selection(
        selection="_metanow_fiscal_device_choices",
        string="Kodi i pajisjes në efis / efis device code",
        help="Gjendja fillestare, futjet dhe tërheqjet e parave deklarohen te efis për këtë pajisje. Bosh: nuk deklarohet asgjë. "
             "/ The opening float and cash put in or taken out are declared to efis for this device. Empty: nothing is declared.")

    @api.model
    def _metanow_fiscal_device_choices(self):
        return self.env["res.company"]._metanow_fiscal_choices(efis_registry.DEVICE)

    @api.constrains("metanow_fiscal_device_code", "company_id")
    def _check_metanow_fiscal_device_code(self):
        for config in self:
            code = config.metanow_fiscal_device_code
            registry = config.company_id._metanow_fiscal_registry() if code else None
            if registry is not None and not registry.confirms(efis_registry.DEVICE, code):
                raise ValidationError(
                    f"{efis_registry.LABELS[efis_registry.DEVICE]}: {code} nuk është konfirmuar në efis "
                    f"për {config.company_id.name}. / {code} is not confirmed in efis for {config.company_id.name}.")

    def _metanow_fiscal_declares(self):
        self.ensure_one()
        return bool(self.company_id.metanow_fiscal_enabled and self.metanow_fiscal_device_code)
