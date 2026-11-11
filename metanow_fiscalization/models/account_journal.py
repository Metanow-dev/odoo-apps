from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..efis import registry as efis_registry


class AccountJournal(models.Model):
    _inherit = "account.journal"

    # One of the codes efis's country profile lists under paymentMethods, checked against the
    # profile when an invoice is posted. Not a list written into this module.
    metanow_fiscal_payment_method = fields.Char(
        string="Mënyra e pagesës për efis / efis payment method",
        help="Kodi i mënyrës së pagesës sipas profilit të vendit në efis. "
             "/ The payment method code from efis's country profile.",
    )
    # DEV-2436: the device a cash invoice from this journal is issued on. Without one, the
    # company's device code is used.
    metanow_fiscal_device_code = fields.Selection(
        selection="_metanow_fiscal_device_choices",
        string="Kodi i pajisjes në efis / efis device code",
        help="Vetëm për faturat me para në dorë, dhe vetëm kodet që efis ka konfirmuar. Bosh: përdoret kodi i kompanisë. "
             "/ Only for cash invoices, and only codes efis has confirmed. Empty: the company's code is used.",
    )

    @api.model
    def _metanow_fiscal_device_choices(self):
        return self.env["res.company"]._metanow_fiscal_choices(efis_registry.DEVICE)

    @api.constrains("metanow_fiscal_device_code", "company_id")
    def _check_metanow_fiscal_device_code(self):
        for journal in self:
            code = journal.metanow_fiscal_device_code
            registry = journal.company_id._metanow_fiscal_registry() if code else None
            if registry is not None and not registry.confirms(efis_registry.DEVICE, code):
                raise ValidationError(
                    f"{efis_registry.LABELS[efis_registry.DEVICE]}: {code} nuk është konfirmuar në efis "
                    f"për {journal.company_id.name}. / {code} is not confirmed in efis for {journal.company_id.name}.")
