import logging

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

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

FISCALISED_TYPES = ("out_invoice", "out_refund")
# Not fiscalised by this module yet. Refused while fiscalisation is on, never posted unfiscalised.
UNSUPPORTED_TYPES = ("out_receipt",)
CREDIT_NOTE = "out_refund"

# DEV-2431: what a credit note is, chosen by the person (nothing is preselected). Odoo uses one
# credit note both to fix a mistake and to record a return or a discount; the authority keeps
# the two apart.
CORRECTION_KINDS = [
    (efis_payload.CORRECTIVE, "Gabim në faturë / Mistake on the invoice"),
    (efis_payload.CREDIT, "Kthim ose zbritje / Return or discount"),
]

# The states of an invoice efis has registered with the authority, which a credit note may
# reverse. A superseded invoice was corrected before, and may be credited again.
CORRECTABLE_STATES = (outcome.REGISTERED, outcome.SUPERSEDED)

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
    metanow_fiscal_warnings = fields.Text(
        string="Paralajmërimet e efis / efis's warnings", copy=False, readonly=True,
        help="Dokumenti është lëshuar; efis tregon diçka për t'u parë. "
             "/ The document is issued; efis points out something to look at.")
    metanow_fiscal_electronic = fields.Boolean(
        string="Faturë elektronike / E-invoice", copy=False, readonly=True,
        help="Dërguar te efis si faturë elektronike për blerësin. / Sent to efis as an e-invoice for the buyer.")
    metanow_fiscal_einvoice_state = fields.Char(
        string="Gjendja e faturës elektronike / E-invoice state", copy=False, readonly=True)
    metanow_fiscal_einvoice_eic = fields.Char(
        string="EIC", copy=False, readonly=True,
        help="Kodi që autoriteti i jep faturës elektronike. / The code the authority gives the e-invoice.")
    metanow_fiscal_einvoice_error = fields.Text(
        string="Gabimi i faturës elektronike / E-invoice error", copy=False, readonly=True)
    metanow_fiscal_qr_src = fields.Char(compute="_compute_metanow_fiscal_qr_src")
    # DEV-2436: the operator code of the person who posted the invoice, taken when it is posted.
    # The send job runs as the cron's user, so it reads this, and every send of the invoice
    # carries the same code.
    metanow_fiscal_operator_code = fields.Char(
        string="Kodi i operatorit / Operator code", copy=False, readonly=True)
    metanow_fiscal_correction_kind = fields.Selection(
        CORRECTION_KINDS,
        string="Nota e kreditit për efis / Credit note for efis",
        copy=False,
        help="Gabim në faturë: fatura kishte të dhëna të gabuara. Kthim ose zbritje: një rregullim i mëvonshëm. "
             "/ Mistake on the invoice: the invoice had wrong data. Return or discount: a later adjustment.")

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
                    "Faturat e arkës nuk fiskalizohen ende nga ky modul. "
                    "/ Receipts are not fiscalised by this module yet.")
            if move._metanow_fiscal_applies() and move.move_type == CREDIT_NOTE:
                move._metanow_fiscal_check_credit_note()
        posted = super()._post(soft)
        queued = False
        for move in posted:
            if not move._metanow_fiscal_applies():
                continue
            # Validate before issuing: a refusal here rolls the posting back, and no number is
            # spent in efis.
            operator = self.env.user._metanow_fiscal_operator_for(move.company_id) or False
            move._metanow_fiscal_payload(operator=operator)
            attempt = (move.metanow_fiscal_attempt or 0) + 1
            move.write({
                "metanow_fiscal_operator_code": operator,
                outcome.STATE: outcome.TO_SEND,
                "metanow_fiscal_attempt": attempt,
                "metanow_fiscal_idempotency_key": move._metanow_fiscal_key(attempt),
                outcome.ERROR: False,
            })
            queued = True
        if queued:
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_send")
        return posted

    def _metanow_fiscal_check_credit_note(self):
        """Refuse, before anything is posted, a credit note efis could not register as a correction."""
        self.ensure_one()
        original = self.reversed_entry_id
        if not original:
            raise UserError(
                f"{self.name}: nota e kreditit nuk i referohet asnjë fature; krijojeni nga fatura me \"Notë krediti\". "
                "/ The credit note refers to no invoice; create it from the invoice with \"Credit note\".")
        if (original.metanow_fiscal_state not in CORRECTABLE_STATES
                or not original.metanow_fiscal_security_code or not original.metanow_fiscal_issued_at):
            raise UserError(
                f"{self.name}: fatura {original.name} nuk është regjistruar nga efis, prandaj nuk korrigjohet ende. "
                f"/ The invoice {original.name} has not been registered by efis, so it cannot be corrected yet.")
        if not self.metanow_fiscal_correction_kind:
            raise UserError(
                f"{self.name}: zgjidhni nëse nota e kreditit korrigjon një gabim në faturë apo regjistron një kthim "
                "ose zbritje. / Choose whether the credit note corrects a mistake on the invoice or records a "
                "return or discount.")

    def _metanow_fiscal_key(self, attempt):
        self.ensure_one()
        # Odoo 20 reads parameters by type; get_param is gone (pipeline #5322, job 40950).
        database = self.env["ir.config_parameter"].sudo().get_str("database.uuid") or "db"
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
                "kind": efis_payload.BUSINESS if partner.is_company else None,
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
        # DEV-2358: what an e-invoice carries beyond the fiscal record. The bank account is the
        # company's account the invoice is paid into (the invoice's recipient bank); only an IBAN
        # is sent. delivery_date is read only where this Odoo has the field.
        bank = None
        # On a credit note, Odoo's partner bank is the customer's account the refund is paid to,
        # never an account to pay into, so it is not sent.
        account = self.partner_bank_id if self.move_type != CREDIT_NOTE else None
        if account and account.acc_type == "iban":
            bank = {
                "iban": account.acc_number,
                "bic": account.bank_id.bic if account.bank_id else None,
                "holder": account.acc_holder_name or None,
            }
        delivered = getattr(self, "delivery_date", False)
        corrects = None
        if self.move_type == CREDIT_NOTE:
            original = self.reversed_entry_id
            corrects = {
                "security_code": original.metanow_fiscal_security_code if original else None,
                "issued_at": original.metanow_fiscal_issued_at if original else None,
                "kind": self.metanow_fiscal_correction_kind,
                "electronic": bool(original.metanow_fiscal_electronic) if original else False,
            }
        return {
            "reference": self.name,
            "due_date": self.invoice_date_due.isoformat() if self.invoice_date_due else None,
            "customer_reference": self.ref or None,
            "delivery_date": delivered.isoformat() if delivered else None,
            "bank": bank,
            "corrects": corrects,
            "currency": self.currency_id.name,
            "company_currency": self.company_id.currency_id.name,
            "amount_total": self.amount_total,
            "amount_untaxed": self.amount_untaxed,
            "amount_tax": self.amount_tax,
            "payment_method": self.journal_id.metanow_fiscal_payment_method,
            "buyer": buyer,
            "lines": lines,
        }

    def _metanow_fiscal_config(self, operator=None):
        """The company's configuration, with this invoice's own codes in place of the company's:
        the operator of the person who posted it, and the device of its journal."""
        self.ensure_one()
        config = self.company_id._metanow_fiscal_config()
        operator = operator or self.metanow_fiscal_operator_code
        if operator:
            config["operator_code"] = operator
        device = self.journal_id.metanow_fiscal_device_code
        if device:
            config["device_code"] = device
        return config

    def _metanow_fiscal_payload(self, operator=None):
        self.ensure_one()
        company = self.company_id
        refusal = f"{self.name}: efis nuk do ta pranonte këtë faturë. / efis would not accept this invoice.\n"
        try:
            body = efis_payload.build(
                self._metanow_fiscal_invoice(), self._metanow_fiscal_config(operator), company._metanow_fiscal_profile())
        except efis_payload.PayloadInvalid as error:
            raise UserError(refusal + "\n".join(error.problems)) from error
        # DEV-2436: only codes efis has confirmed. A company that has not read its registry yet
        # (installed before DEV-2436) is checked by efis alone, as before.
        registry = company._metanow_fiscal_registry()
        problems = registry.unconfirmed(body) if registry is not None else []
        if problems:
            raise UserError(refusal + "\n".join(problems))
        return body

    # ------------------------------------------------------------------ write-back

    def _metanow_fiscal_current(self):
        self.ensure_one()
        names = (
            outcome.DOCUMENT_ID, outcome.STATE, outcome.EFIS_STATE, outcome.FISCAL_NUMBER,
            outcome.SECURITY_CODE, outcome.REGISTRATION_CODE, outcome.VERIFICATION_URL,
            outcome.ISSUED_AT, outcome.ERROR, outcome.WARNINGS, outcome.ELECTRONIC,
            outcome.EINVOICE_STATE, outcome.EINVOICE_EIC, outcome.EINVOICE_ERROR,
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
        # Whether it went as an e-invoice is stored with efis's answer, in the same write.
        self._metanow_fiscal_apply(
            outcome.submission_values, response, extra={outcome.ELECTRONIC: bool(body.get("electronic"))})
        if self.metanow_fiscal_state == outcome.REFUSED and outcome.returns_to_draft(response):
            self._metanow_fiscal_return_to_draft()

    def _metanow_fiscal_return_to_draft(self):
        """Put an invoice efis refused before numbering back in draft, with efis's message on it.

        Nothing was numbered, so nothing fiscal is undone: the person who posted it corrects it and
        posts again, and that posting is a new request with a new key. It is never resent as it is.
        If Odoo will not reset it (a lock date, for example), it stays posted as refused, with the
        message, and a person resets it.
        """
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.button_draft()
        except (UserError, ValidationError) as error:
            _logger.warning("invoice %s refused by efis could not return to draft: %s", self.id, error)
            self._metanow_fiscal_store({
                outcome.ERROR: f"{self.metanow_fiscal_error}\n"
                               f"Odoo nuk e ktheu faturën në draft; kthejeni me dorë. "
                               f"/ Odoo did not return the invoice to draft; reset it by hand: {error}",
            })

    def _metanow_fiscal_poll(self):
        self.ensure_one()
        if not self.metanow_fiscal_document_id or not self._metanow_fiscal_lock():
            return
        try:
            client = self.company_id._metanow_fiscal_client()
            response = client.get_document(self.metanow_fiscal_document_id)
        except (efis_client.EfisUnavailable, UserError) as error:
            _logger.info("efis read of %s failed: %s", self.metanow_fiscal_document_id, error)
            return
        einvoice = None
        body = response.body if isinstance(response.body, dict) else {}
        # The e-invoice is exchanged only after fiscalisation, so it is read once efis says the
        # document is registered. Both answers go into the one write.
        registered = 200 <= response.status < 300 and outcome.odoo_state(body.get("state")) == outcome.REGISTERED
        if self.metanow_fiscal_electronic and registered:
            try:
                einvoice = client.get_document_einvoice(self.metanow_fiscal_document_id)
            except efis_client.EfisUnavailable as error:
                _logger.info("efis e-invoice read of %s failed: %s", self.metanow_fiscal_document_id, error)
        self._metanow_fiscal_apply(outcome.poll_values, response, einvoice=einvoice)

    def _metanow_fiscal_apply(self, reader, response, extra=None, einvoice=None):
        try:
            current = self._metanow_fiscal_current()
            values = reader(current, response)
            if values and extra and response is not None and 200 <= response.status < 300:
                values.update(extra)
            if einvoice is not None:
                einvoice_values = outcome.einvoice_values(current, einvoice)
                if einvoice_values:
                    values = dict(values or {}, **einvoice_values)
        except outcome.SecurityCodeConflict as error:
            # Never overwrite a stored code. Say so, loudly, and keep what is stored.
            _logger.error("efis answer conflicts with invoice %s: %s", self.id, error)
            values = {outcome.STATE: outcome.PARKED, outcome.ERROR: f"conflict: {error}"}
        except ValueError as error:
            values = {outcome.ERROR: f"unreadable answer: {error}"}
        self._metanow_fiscal_store(values)
        if values and (values.get(outcome.STATE) in outcome.POLLED
                       or outcome.einvoice_pending(self._metanow_fiscal_current())):
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
        # Registered invoices whose e-invoice efis is still exchanging, in a batch of their own,
        # so e-invoices waiting on the authority never hold back the documents above.
        domain = [
            (outcome.DOCUMENT_ID, "!=", False),
            (outcome.ELECTRONIC, "=", True),
            (outcome.STATE, "=", outcome.REGISTERED),
            (outcome.EINVOICE_STATE, "not in", list(outcome.EINVOICE_FINAL)),
        ]
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
        if "metanow_fiscal_correction_kind" in vals:
            for move in self:
                if move._metanow_fiscal_locked() and vals["metanow_fiscal_correction_kind"] != move.metanow_fiscal_correction_kind:
                    raise UserError(
                        f"{move.name}: nota e kreditit është dërguar te efis dhe lloji i saj nuk ndryshohet. "
                        "/ The credit note has been sent to efis and its kind is never changed.")
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
