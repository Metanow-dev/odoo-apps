from odoo import api, fields, models
from odoo.exceptions import ValidationError

TRANSACTIONS = [
    ("TRANSFER", "Transferim / Transfer"),
    ("DOOR", "Shitje derë më derë / Door to door"),
    ("SALES", "Shitje / Sales"),
    ("EXAMINATION", "Ekzaminim / Examination"),
]


# DEV-2430: goods sent to another person for processing, examination or repair, owner unchanged.
TI_TRANSACTIONS = [
    ("PROCESSING", "Përpunim / Processing"),
    ("EXAMINATION", "Ekzaminim / Examination"),
    ("REPAIR", "Riparim / Repair"),
]


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    # DEV-2429: which transfer note this operation's transfers get. Nothing is preselected: which
    # Odoo transfer needs a note, and of which kind, is still with the accountant (DEV-2441).
    metanow_fiscal_wtn_transaction = fields.Selection(
        TRANSACTIONS, string="Transaksioni i faturës shoqëruese / Transfer note transaction",
        help="Bosh: transferimet e këtij operacioni nuk lëshojnë faturë shoqëruese. "
             "/ Empty: this operation's transfers issue no transfer note.")
    # DEV-2430: or a transport invoice, with its own transactions. One or the other, never both;
    # nothing preselected (EXAMINATION exists for both, and the rule is with the helpdesk, DEV-2036).
    metanow_fiscal_ti_transaction = fields.Selection(
        TI_TRANSACTIONS, string="Transaksioni i faturës së transportit / Transport invoice transaction",
        help="Për mallra që i dërgohen një personi tjetër për përpunim, ekzaminim ose riparim. "
             "/ For goods sent to another person for processing, examination or repair.")

    @api.constrains("metanow_fiscal_wtn_transaction", "metanow_fiscal_ti_transaction")
    def _check_metanow_fiscal_one_document(self):
        for picking_type in self:
            if picking_type.metanow_fiscal_wtn_transaction and picking_type.metanow_fiscal_ti_transaction:
                raise ValidationError(
                    f"{picking_type.name}: një operacion lëshon ose faturë shoqëruese ose faturë transporti, jo të dyja. "
                    "/ An operation issues a transfer note or a transport invoice, never both.")

    def _metanow_fiscal_document(self, company):
        """Which transport document this operation's transfers issue at the company, if any."""
        self.ensure_one()
        if self.metanow_fiscal_wtn_transaction:
            return "wtn", self.metanow_fiscal_wtn_transaction
        if self.metanow_fiscal_ti_transaction and company.metanow_fiscal_ti_enabled:
            return "ti", self.metanow_fiscal_ti_transaction
        return None, None
