from odoo import fields, models

TRANSACTIONS = [
    ("TRANSFER", "Transferim / Transfer"),
    ("DOOR", "Shitje derë më derë / Door to door"),
    ("SALES", "Shitje / Sales"),
    ("EXAMINATION", "Ekzaminim / Examination"),
]


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    # DEV-2429: which transfer note this operation's transfers get. Nothing is preselected: which
    # Odoo transfer needs a note, and of which kind, is still with the accountant (DEV-2441).
    metanow_fiscal_wtn_transaction = fields.Selection(
        TRANSACTIONS, string="Transaksioni i faturës shoqëruese / Transfer note transaction",
        help="Bosh: transferimet e këtij operacioni nuk lëshojnë faturë shoqëruese. "
             "/ Empty: this operation's transfers issue no transfer note.")
