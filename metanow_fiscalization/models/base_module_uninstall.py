from odoo import api, fields, models
from odoo.exceptions import UserError

MODULE = "metanow_fiscalization"

# DEV-2443, decided 12 Nov 2026: uninstalling is never blocked. Admins sometimes uninstall a
# module to fix something, and a block would only push them to force it. efis keeps every
# document, and "Restore from efis" brings the values back, so a clear warning and an explicit
# confirmation are enough.
WARNING = (
    "Ky modul ka dërguar {count} fatura te efis. Çinstalimi fshin nga Odoo numrin fiskal, kodet, "
    "kodin QR dhe bllokimin kundër ndryshimeve të këtyre faturave. efis i ruan të gjitha dokumentet: "
    "pasi ta instaloni përsëri modulin dhe ta çiftoni kompaninë, të dhënat rikthehen me "
    "\"Rikthe të dhënat nga efis\" te Cilësimet. Deri atëherë mos i ndryshoni dhe mos i ripostoni këto fatura.\n"
    "This module has sent {count} invoices to efis. Uninstalling removes from Odoo their fiscal "
    "number, codes, QR code and the lock against edits. efis keeps every document: after you install "
    "the module again and pair the company, the data comes back with \"Restore from efis\" in "
    "Settings. Until then, do not change or post these invoices again."
)


class BaseModuleUninstall(models.TransientModel):
    _inherit = "base.module.uninstall"

    metanow_fiscal_warning = fields.Text(compute="_compute_metanow_fiscal_warning")
    metanow_fiscal_confirmed = fields.Boolean(
        string="E kuptoj: të dhënat fiskale fshihen nga Odoo dhe rikthehen nga efis. "
               "/ I understand: the fiscal data is removed from Odoo and restored from efis.")

    @api.depends("module_ids")
    def _compute_metanow_fiscal_warning(self):
        for wizard in self:
            wizard.metanow_fiscal_warning = wizard._metanow_fiscal_warning() or False

    def _metanow_fiscal_warning(self):
        """The warning, when this uninstall removes the module from a database with invoices sent
        to efis; otherwise None. Uninstalling a module this one depends on removes it too."""
        if MODULE not in {module.name for module in self._get_modules()}:
            return None
        count = self.env["account.move"].sudo().search_count(
            ["|", ("metanow_fiscal_document_id", "!=", False), ("metanow_fiscal_state", "!=", False)])
        return WARNING.format(count=count) if count else None

    def action_uninstall(self):
        if self._metanow_fiscal_warning() and not self.metanow_fiscal_confirmed:
            raise UserError(
                "Konfirmoni që e kuptoni çfarë fshihet, pastaj çinstaloni. "
                "/ Confirm that you understand what is removed, then uninstall.")
        return super().action_uninstall()
