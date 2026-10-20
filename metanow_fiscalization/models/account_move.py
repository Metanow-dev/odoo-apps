import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..efis import client as efis_client
from ..efis import outcome
from ..efis import payload as efis_payload
from ..efis import qr

_logger = logging.getLogger(__name__)

FISCAL_STATES = [
    (outcome.TO_SEND, "Në radhë për efis / Queued for efis"),
    (outcome.SENT, "Dërguar te efis / Sent to efis"),
    (outcome.REGISTERED, "Regjistruar / Registered"),
    (outcome.DEFERRED, "Shtyrë / Deferred"),
    (outcome.REJECTED, "Refuzuar nga autoriteti / Rejected by the authority"),
    (outcome.SUPERSEDED, "Zëvendësuar / Superseded"),
    (outcome.REFUSED, "Refuzuar nga efis, pa numër / Refused by efis, no number spent"),
    (outcome.PARKED, "Në pritje të veprimit / Waiting for action"),
]

FISCALISED_TYPES = ("out_invoice",)
# Posted through efis's corrective flow, which this module does not drive yet. They are refused
# while fiscalisation is on, never posted unfiscalised.
UNSUPPORTED_TYPES = ("out_refund", "out_receipt")

BATCH = 50


class AccountMove(models.Model):
    _inherit = "account.move"

    metanow_fiscal_state = fields.Selection(
        FISCAL_STATES, string="Gjendja fiskale / Fiscal state", copy=False, readonly=True, index=True)
    metanow_fiscal_document_id = fields.Char(
        string="Dokumenti në efis / efis document", copy=False, readonly=True, index=True)
    metanow_fiscal_efis_state = fields.Char(string="Gjendja në efis / State in efis", copy=False, readonly=True)
    metanow_fiscal_number = fields.Char(string="Numri fiskal / Fiscal number", copy=False, readonly=True)
    metanow_fiscal_security_code = fields.Char(
        string="Kodi i sigurisë së lëshuesit / Issuer security code", copy=False, readonly=True)
    metanow_fiscal_registration_code = fields.Char(
        string="Kodi i regjistrimit / Registration code", copy=False, readonly=True)
    metanow_fiscal_verification_url = fields.Char(
        string="Adresa e verifikimit / Verification URL", copy=False, readonly=True)
    metanow_fiscal_issued_at = fields.Char(
        string="Lëshuar më / Issued at", copy=False, readonly=True,
        help="Koha e lëshimit sipas efis, me zhvendosjen e saj. / The issue time as efis states it, with its offset.")
    metanow_fiscal_idempotency_key = fields.Char(copy=False, readonly=True)
    metanow_fiscal_attempt = fields.Integer(copy=False, readonly=True, default=0)
    metanow_fiscal_error = fields.Text(string="Përgjigja e efis / efis's answer", copy=False, readonly=True)
    metanow_fiscal_qr_src = fields.Char(compute="_compute_metanow_fiscal_qr_src")

    @api.depends("metanow_fiscal_verification_url")
    def _compute_metanow_fiscal_qr_src(self):
        for move in self:
            move.metanow_fiscal_qr_src = qr.barcode_src(move.metanow_fiscal_verification_url)

    # ------------------------------------------------------------------ posting

    def _metanow_fiscal_applies(self):
        self.ensure_one()
        return bool(self.company_id.metanow_fiscal_enabled) and self.move_type in FISCALISED_TYPES

    def _post(self, soft=True):
        for move in self:
            if move.company_id.metanow_fiscal_enabled and move.move_type in UNSUPPORTED_TYPES:
                raise UserError(
                    "Notat e kreditit dhe faturat e arkës nuk fiskalizohen ende nga ky modul; "
                    "lëshoni korrigjimin nga efis. / Credit notes and receipts are not fiscalised by this "
                    "module yet; issue the correction from efis.")
        posted = super()._post(soft)
        queued = False
        for move in posted:
            if not move._metanow_fiscal_applies():
                continue
            # Validate before issuing: a refusal here rolls the posting back, and no number is
            # spent in efis.
            move._metanow_fiscal_payload()
            attempt = (move.metanow_fiscal_attempt or 0) + 1
            move.write({
                outcome.STATE: outcome.TO_SEND,
                "metanow_fiscal_attempt": attempt,
                "metanow_fiscal_idempotency_key": move._metanow_fiscal_key(attempt),
                outcome.ERROR: False,
            })
            queued = True
        if queued:
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_send")
        return posted

    def _metanow_fiscal_key(self, attempt):
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "db"
        return f"odoo-{database}-{self.id}-{attempt}"

    def _metanow_fiscal_trigger(self, xml_id):
        cron = self.env.ref(xml_id, raise_if_not_found=False)
        if cron:
            cron._trigger()

    def _metanow_fiscal_invoice(self):
        """The invoice as plain values for the request body builder."""
        self.ensure_one()
        partner = self.commercial_partner_id
        buyer = None
        if partner:
            buyer = {
                "name": partner.name,
                "identifier": partner.vat,
                "identifier_type": partner.metanow_fiscal_identifier_type,
                "address": " ".join(part for part in (partner.street, partner.street2) if part) or None,
                "town": partner.city,
                "country_code": partner.country_id.metanow_fiscal_country_code if partner.country_id else None,
            }
        lines = []
        for line in self.invoice_line_ids:
            if line.display_type != "product":
                continue
            lines.append({
                "description": line.name or (line.product_id.display_name if line.product_id else None),
                "unit": line.product_uom_id.name if line.product_uom_id else None,
                "quantity": line.quantity,
                "price_subtotal": line.price_subtotal,
                "price_total": line.price_total,
                "item_code": line.product_id.default_code if line.product_id else None,
                "taxes": [
                    {
                        "amount_type": tax.amount_type,
                        "amount": tax.amount,
                        "exemption": tax.metanow_fiscal_exemption,
                    }
                    for tax in line.tax_ids
                ],
            })
        return {
            "reference": self.name,
            "currency": self.currency_id.name,
            "company_currency": self.company_id.currency_id.name,
            "amount_total": self.amount_total,
            "amount_untaxed": self.amount_untaxed,
            "amount_tax": self.amount_tax,
            "payment_method": self.journal_id.metanow_fiscal_payment_method,
            "buyer": buyer,
            "lines": lines,
        }

    def _metanow_fiscal_payload(self):
        self.ensure_one()
        company = self.company_id
        try:
            return efis_payload.build(
                self._metanow_fiscal_invoice(), company._metanow_fiscal_config(), company._metanow_fiscal_profile())
        except efis_payload.PayloadInvalid as error:
            raise UserError(
                f"{self.name}: efis nuk do ta pranonte këtë faturë. / efis would not accept this invoice.\n"
                + "\n".join(error.problems)) from error

    # ------------------------------------------------------------------ write-back

    def _metanow_fiscal_current(self):
        self.ensure_one()
        names = (
            outcome.DOCUMENT_ID, outcome.STATE, outcome.EFIS_STATE, outcome.FISCAL_NUMBER,
            outcome.SECURITY_CODE, outcome.REGISTRATION_CODE, outcome.VERIFICATION_URL,
            outcome.ISSUED_AT, outcome.ERROR,
        )
        return {name: self[name] or None for name in names}

    def _metanow_fiscal_store(self, values):
        """The one write of an answer from efis. Never split into several writes."""
        self.ensure_one()
        if values:
            self.write(values)

    def _metanow_fiscal_lock(self):
        """Take this invoice's row, or report that another worker holds it."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE SKIP LOCKED", [self.id])
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        return True

    def _metanow_fiscal_send(self):
        self.ensure_one()
        if not self._metanow_fiscal_lock() or self.metanow_fiscal_state != outcome.TO_SEND:
            return
        try:
            body = self._metanow_fiscal_payload()
        except UserError as error:
            # The posted invoice no longer passes (the configuration changed since posting).
            # Nothing was sent, so nothing was numbered: park it for a person to look at.
            self._metanow_fiscal_store({outcome.STATE: outcome.PARKED, outcome.ERROR: str(error)})
            return
        try:
            client = self.company_id._metanow_fiscal_client()
            response = client.submit_document(body, self.metanow_fiscal_idempotency_key)
        except efis_client.EfisUnavailable as error:
            self._metanow_fiscal_store({outcome.ERROR: f"efis_unavailable: {error}"})
            return
        except UserError as error:
            self._metanow_fiscal_store({outcome.STATE: outcome.PARKED, outcome.ERROR: str(error)})
            return
        self._metanow_fiscal_apply(outcome.submission_values, response)

    def _metanow_fiscal_poll(self):
        self.ensure_one()
        if not self.metanow_fiscal_document_id or not self._metanow_fiscal_lock():
            return
        try:
            response = self.company_id._metanow_fiscal_client().get_document(self.metanow_fiscal_document_id)
        except (efis_client.EfisUnavailable, UserError) as error:
            _logger.info("efis read of %s failed: %s", self.metanow_fiscal_document_id, error)
            return
        self._metanow_fiscal_apply(outcome.poll_values, response)

    def _metanow_fiscal_apply(self, reader, response):
        try:
            values = reader(self._metanow_fiscal_current(), response)
        except outcome.SecurityCodeConflict as error:
            # Never overwrite a stored code. Say so, loudly, and keep what is stored.
            _logger.error("efis answer conflicts with invoice %s: %s", self.id, error)
            values = {outcome.STATE: outcome.PARKED, outcome.ERROR: f"conflict: {error}"}
        except ValueError as error:
            values = {outcome.ERROR: f"unreadable answer: {error}"}
        self._metanow_fiscal_store(values)
        if values and values.get(outcome.STATE) in outcome.POLLED:
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_poll")

    @api.model
    def _cron_metanow_fiscal_send(self):
        for move in self.search([(outcome.STATE, "=", outcome.TO_SEND)], order="id", limit=BATCH):
            move._metanow_fiscal_send()

    @api.model
    def _cron_metanow_fiscal_poll(self):
        domain = [(outcome.STATE, "in", list(outcome.POLLED)), (outcome.DOCUMENT_ID, "!=", False)]
        for move in self.search(domain, order="id", limit=BATCH):
            move._metanow_fiscal_poll()

    def action_metanow_fiscal_retry(self):
        """Send a parked invoice again: the same key and the same body, never a new document."""
        for move in self:
            if move.metanow_fiscal_state != outcome.PARKED:
                continue
            state = outcome.SENT if move.metanow_fiscal_document_id else outcome.TO_SEND
            move.write({outcome.STATE: state})
        self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_send")
        self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_poll")
        return True

    # ------------------------------------------------------------------ guards

    def _metanow_fiscal_locked(self):
        """True once efis may hold a document for this invoice."""
        self.ensure_one()
        return bool(self.metanow_fiscal_document_id) or (
            bool(self.metanow_fiscal_state) and self.metanow_fiscal_state not in outcome.RESETTABLE)

    def _metanow_fiscal_refuse_change(self):
        for move in self:
            if move._metanow_fiscal_locked():
                raise UserError(
                    f"{move.name}: fatura është dërguar për fiskalizim dhe nuk ndryshohet më; "
                    "korrigjohet vetëm me një dokument korrigjues. / The invoice has been sent for "
                    "fiscalisation and is never edited again; it is corrected only by a corrective document.")

    def button_draft(self):
        self._metanow_fiscal_refuse_change()
        return super().button_draft()

    def button_cancel(self):
        self._metanow_fiscal_refuse_change()
        return super().button_cancel()

    def unlink(self):
        self._metanow_fiscal_refuse_change()
        return super().unlink()

    def write(self, vals):
        changing = [name for name in outcome.WRITE_ONCE if name in vals]
        if changing:
            for move in self:
                for name in changing:
                    stored = move[name]
                    if stored and vals[name] != stored:
                        raise UserError(
                            f"{move.name}: {name} është ruajtur nga efis dhe nuk ndryshohet. "
                            f"/ {name} was stored from efis and is never changed.")
        return super().write(vals)
