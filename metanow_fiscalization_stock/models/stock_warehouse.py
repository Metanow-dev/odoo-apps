from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.metanow_fiscalization.efis import registry as efis_registry

from ..efis import transport

PLACE_KINDS = [
    ("WAREHOUSE", "Magazinë / Warehouse"),
    ("EXHIBITION", "Ekspozitë / Exhibition"),
    ("STORE", "Dyqan / Store"),
    ("SALE", "Pikë shitjeje / Point of sale"),
    ("OTHER", "Tjetër / Other"),
]
assert tuple(code for code, _ in PLACE_KINDS) == transport.DESTINATION_KINDS


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    # DEV-2429: a transfer note is numbered from the sequence of the business unit the goods
    # leave from (Udhëzim 20 Art. 26), so each warehouse names its own, chosen from the codes efis
    # confirmed. There is no fallback to the company's code: a note numbered from the wrong unit
    # cannot be taken back.
    metanow_fiscal_business_unit_code = fields.Selection(
        selection="_metanow_fiscal_business_unit_choices",
        string="Kodi i njësisë së biznesit në efis / efis business unit code")
    metanow_fiscal_place_kind = fields.Selection(
        PLACE_KINDS, string="Lloji i vendit për efis / Kind of place for efis")

    @api.model
    def _metanow_fiscal_business_unit_choices(self):
        return self.env["res.company"]._metanow_fiscal_choices(efis_registry.BUSINESS_UNIT)

    @api.constrains("metanow_fiscal_business_unit_code", "company_id")
    def _check_metanow_fiscal_business_unit_code(self):
        for warehouse in self:
            code = warehouse.metanow_fiscal_business_unit_code
            registry = warehouse.company_id._metanow_fiscal_registry() if code else None
            if registry is not None and not registry.confirms(efis_registry.BUSINESS_UNIT, code):
                raise ValidationError(
                    f"{efis_registry.LABELS[efis_registry.BUSINESS_UNIT]}: {code} nuk është konfirmuar në efis "
                    f"për {warehouse.company_id.name}. / {code} is not confirmed in efis for {warehouse.company_id.name}.")
