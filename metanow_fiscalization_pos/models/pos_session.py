from odoo import api, fields, models
from odoo.exceptions import UserError

from . import deposit
from ..efis import deposits

PREFIX = "metanow_fiscal_initial"
BATCH = 50


class PosSession(models.Model):
    _inherit = "pos.session"

    # DEV-2437: the day's opening float (INITIAL), declared when the session is opened.
    metanow_fiscal_initial_state = fields.Selection(
        deposit.STATES, string="Gjendja fillestare në efis / Opening float in efis", copy=False, readonly=True, index=True)
    metanow_fiscal_initial_document_id = fields.Char(copy=False, readonly=True)
    metanow_fiscal_initial_registration_code = fields.Char(
        string="Kodi i regjistrimit / Registration code", copy=False, readonly=True)
    metanow_fiscal_initial_key = fields.Char(copy=False, readonly=True)
    metanow_fiscal_initial_request = fields.Text(copy=False, readonly=True)
    metanow_fiscal_initial_error = fields.Text(string="Shënim nga efis / Note from efis", copy=False, readonly=True)
    # The Tirana day the float is for: efis takes one INITIAL per device and day.
    metanow_fiscal_initial_day = fields.Date(copy=False, readonly=True, index=True)
    metanow_fiscal_cash_summary = fields.Text(
        string="Futjet dhe tërheqjet në efis / Cash in and out in efis", compute="_compute_metanow_fiscal_cash_summary")

    @api.depends("bank_statement_line_ids.metanow_fiscal_deposit_state")
    def _compute_metanow_fiscal_cash_summary(self):
        labels = dict(deposit.STATES)
        for session in self:
            lines = [
                f"{line.amount:+.2f}  {labels.get(line.metanow_fiscal_deposit_state, '')}"
                + (f"  {line.metanow_fiscal_deposit_error}" if line.metanow_fiscal_deposit_error else "")
                for line in session.bank_statement_line_ids if line.metanow_fiscal_deposit_state
            ]
            session.metanow_fiscal_cash_summary = "\n".join(lines) or False

    def _metanow_fiscal_key(self, kind, record_id):
        database = self.env["ir.config_parameter"].sudo().get_str("database.uuid") or "db"
        return f"odoo-{database}-pos-{kind}-{record_id}"

    # ------------------------------------------------------------------ opening

    def _set_opening_control_data(self, cashbox_value, notes):
        result = super()._set_opening_control_data(cashbox_value, notes)
        for session in self:
            if session.config_id._metanow_fiscal_declares():
                session._metanow_fiscal_queue_initial(cashbox_value)
        self._metanow_fiscal_trigger("metanow_fiscalization_pos.ir_cron_metanow_fiscal_pos_send")
        return result

    def _metanow_fiscal_queue_initial(self, amount):
        """The opening float, once per device and Tirana day: efis takes one INITIAL a day for a
        device, so a second session opened the same day declares none."""
        self.ensure_one()
        device = self.config_id.metanow_fiscal_device_code
        changed_at = deposit.tirana_time(self.start_at)
        day = changed_at[:10]
        self.write({"metanow_fiscal_initial_day": day})
        earlier = self.search_count([
            ("id", "!=", self.id), ("company_id", "=", self.company_id.id),
            ("config_id.metanow_fiscal_device_code", "=", device), ("metanow_fiscal_initial_day", "=", day),
            ("metanow_fiscal_initial_state", "in", list(deposit.HELD))])
        if earlier:
            self.write({"metanow_fiscal_initial_state": "skipped", "metanow_fiscal_initial_error":
                        "Gjendja fillestare e sotme për këtë pajisje është deklaruar nga një seancë më parë. "
                        "/ Today's opening float for this device was declared by an earlier session."})
            return
        deposit.queue(self, PREFIX, deposits.INITIAL, amount, device, changed_at, self._metanow_fiscal_key("initial", self.id))

    # ------------------------------------------------------------------ cash in and out

    def try_cash_in_out(self, _type, amount, reason, partner_id):
        before = {session.id: set(session.bank_statement_line_ids.ids) for session in self}
        result = super().try_cash_in_out(_type, amount, reason, partner_id)
        for session in self:
            if not session.config_id._metanow_fiscal_declares():
                continue
            new_lines = session.bank_statement_line_ids.filtered(lambda line: line.id not in before.get(session.id, set()))
            for line in new_lines:
                deposit.queue(line, "metanow_fiscal_deposit", deposits.DEPOSIT if line.amount > 0 else deposits.WITHDRAW,
                              abs(line.amount), session.config_id.metanow_fiscal_device_code, deposit.tirana_time(),
                              self._metanow_fiscal_key("cash", line.id))
        self._metanow_fiscal_trigger("metanow_fiscalization_pos.ir_cron_metanow_fiscal_pos_send")
        return result

    def delete_cash_in_out(self, absl_id, partner_id):
        line = self.env["account.bank.statement.line"].browse(absl_id).sudo()
        if line.exists() and line.metanow_fiscal_deposit_state in deposit.HELD:
            raise UserError(
                "Kjo lëvizje parash i është deklaruar autoritetit dhe nuk fshihet; regjistroni lëvizjen e kundërt. "
                "/ This cash movement was declared to the authority and is never deleted; record the opposite movement instead.")
        return super().delete_cash_in_out(absl_id, partner_id)

    # ------------------------------------------------------------------ jobs

    def _metanow_fiscal_trigger(self, xml_id):
        cron = self.env.ref(xml_id, raise_if_not_found=False)
        if cron:
            cron._trigger()

    @api.model
    def _cron_metanow_fiscal_pos_send(self):
        for session in self.search([("metanow_fiscal_initial_state", "=", "to_send")], order="id", limit=BATCH):
            deposit.send(session, PREFIX, "pos_session", session.company_id)
        lines = self.env["account.bank.statement.line"].search(
            [("metanow_fiscal_deposit_state", "=", "to_send")], order="id", limit=BATCH)
        for line in lines:
            deposit.send(line, "metanow_fiscal_deposit", "account_bank_statement_line", line.company_id)

    @api.model
    def _cron_metanow_fiscal_pos_poll(self):
        sessions = self.search([("metanow_fiscal_initial_state", "in", list(deposit.POLLED))], limit=BATCH)
        lines = self.env["account.bank.statement.line"].search(
            [("metanow_fiscal_deposit_state", "in", list(deposit.POLLED))], limit=BATCH)
        for company in sessions.company_id | lines.company_id:
            deposit.follow(sessions.filtered(lambda s: s.company_id == company), PREFIX, company)
            deposit.follow(lines.filtered(lambda line: line.company_id == company), "metanow_fiscal_deposit", company)
