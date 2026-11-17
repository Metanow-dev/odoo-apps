import json
import logging
from datetime import date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import plaintext2html

from ..efis import client as efis_client
from ..efis import inbound as efis_inbound
from ..efis import outcome
from ..efis import payment_records
from ..efis import payload as efis_payload
from ..efis import money
from ..efis import qr
from ..efis import receivables
from ..efis import restore
from ..efis.profile import PAYMENT_METHODS

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

# DEV-2432: what efis said when it would not take a payment, in the person's words. Any other
# refusal is shown with efis's code.
PAYMENT_REFUSALS = {
    "not_receivable": "efis nuk ndjek pagesat e kësaj fature (nuk ka afat pagese, ose është me para në dorë). "
                      "/ efis does not track payments on this invoice (it has no due date, or it was paid in cash).",
    "payment_exceeds_outstanding": "Pagesat e kësaj fature kalojnë shumën që mbetet për t'u paguar në efis. "
                                   "/ The payments on this invoice are more than efis says is still owed.",
    "invalid_method": "efis nuk e njeh mënyrën e pagesës së ditarit. / efis does not know the journal's payment method.",
    "invalid_paid_on": "Data e pagesës nuk pranohet nga efis. / efis does not accept the payment date.",
}

# The states of an invoice efis has registered with the authority, which a credit note may
# reverse. A superseded invoice was corrected before, and may be credited again.
CORRECTABLE_STATES = (outcome.REGISTERED, outcome.SUPERSEDED)

BATCH = 50
# DEV-2443: invoices restored from efis in one run of the restore job. A larger history is
# restored over several runs, each one starting where the last stopped.
RESTORE_BATCH = 200

RESTORED_NOTE = ("Rikthyer nga efis pas riinstalimit të modulit; asgjë nuk u dërgua. "
                 "/ Restored from efis after the module was reinstalled; nothing was sent.")
REGISTERED_NOTE = "Regjistruar nga efis. / Registered by efis."
BACKFILLED_NOTE = ("Regjistruar nga efis më parë; të dhënat ruhen këtu që të lexohen edhe pa modulin. "
                   "/ Registered by efis earlier; the values are kept here so they can be read without the module.")


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
        string="EIC e e-faturës / Sent e-invoice EIC", copy=False, readonly=True,
        help="Kodi që autoriteti i jep faturës elektronike. / The code the authority gives the e-invoice.")
    metanow_fiscal_einvoice_error = fields.Text(
        string="Gabimi i faturës elektronike / E-invoice error", copy=False, readonly=True)
    metanow_fiscal_qr_src = fields.Char(compute="_compute_metanow_fiscal_qr_src")
    # DEV-2436: the operator code of the person who posted the invoice, taken when it is posted.
    # The send job runs as the cron's user, so it reads this, and every send of the invoice
    # carries the same code.
    metanow_fiscal_operator_code = fields.Char(
        string="Kodi i operatorit / Operator code", copy=False, readonly=True)
    # DEV-2432: the invoice's payments are sent to efis's receivables by a job. Set whenever
    # Odoo's payments on it change; cleared once efis holds what Odoo has.
    metanow_fiscal_payments_pending = fields.Boolean(
        string="Pagesat në pritje për efis / Payments waiting for efis", copy=False, readonly=True, index=True)
    metanow_fiscal_payments_error = fields.Text(
        string="Gabimi i pagesave / Payments error", copy=False, readonly=True)
    # DEV-2434: Instruction 24 payment records for an e-invoice's cash payments, kept by efis and
    # filed by a person on the platform by the 10th of the following month.
    metanow_fiscal_payment_records_pending = fields.Boolean(copy=False, readonly=True, index=True)
    metanow_fiscal_payment_records = fields.Text(copy=False, readonly=True)
    metanow_fiscal_payment_record_due = fields.Date(
        string="Regjistrimi i pagesës për t'u depozituar deri më / Payment record to file by",
        copy=False, readonly=True, index=True,
        help="Dita e fundit për ta depozituar në platformë regjistrimin e një pagese me para në dorë. "
             "/ The last day to file a cash payment's record on the platform.")
    metanow_fiscal_payment_records_note = fields.Text(
        string="Regjistrimet e pagesave / Payment records", copy=False, readonly=True)
    metanow_fiscal_payment_records_summary = fields.Text(
        string="Regjistrimet e pagesave me para në dorë / Cash payment records",
        compute="_compute_metanow_fiscal_payment_records_summary")
    # DEV-2433: a vendor bill made from an e-invoice a supplier sent, as efis lists it. efis
    # carries no lines, so these are what the bill knows from the authority.
    metanow_fiscal_inbound_id = fields.Char(
        string="E-fatura e marrë në efis / Received e-invoice in efis", copy=False, readonly=True, index=True)
    # DEV-2461: its own label. Odoo shows both on bank statement lines, through account.move.
    metanow_fiscal_inbound_eic = fields.Char(
        string="EIC e faturës së marrë / Received e-invoice EIC", copy=False, readonly=True)
    metanow_fiscal_inbound_seller = fields.Char(
        string="Numri tatimor i shitësit / Seller's tax number", copy=False, readonly=True)
    metanow_fiscal_inbound_amount = fields.Char(
        string="Shuma sipas autoritetit / Amount the authority holds", copy=False, readonly=True)
    metanow_fiscal_inbound_status = fields.Char(
        string="Gjendja te autoriteti / Status at the authority", copy=False, readonly=True)
    metanow_fiscal_inbound_answer_by = fields.Date(
        string="Përgjigjuni para / Answer before", copy=False, readonly=True,
        help="Rishikimi i librave më 11 të muajit pasardhës: atë ditë të dhënat deklarohen vetë. "
             "/ The book review on the 11th of the following month: on that day the data is declared by itself.")
    metanow_fiscal_inbound_waiting = fields.Boolean(
        string="Pret përgjigjen tuaj / Waiting for your answer", copy=False, readonly=True, index=True)
    metanow_fiscal_inbound_decision = fields.Char(
        string="Përgjigjja juaj / Your answer", copy=False, readonly=True)
    metanow_fiscal_inbound_fiscalised = fields.Boolean(copy=False, readonly=True)
    metanow_fiscal_inbound_pdf_pending = fields.Boolean(copy=False, readonly=True)
    metanow_fiscal_inbound_note = fields.Text(
        string="Shënim nga efis / Note from efis", copy=False, readonly=True)
    # DEV-2443: what efis returned, also kept in the chatter, which stays when the module is
    # uninstalled. Posted once per invoice.
    metanow_fiscal_noted = fields.Boolean(copy=False, readonly=True)
    # DEV-1704: efis posted an event about this invoice's document; the poll reads it again,
    # whatever its state.
    metanow_fiscal_refresh = fields.Boolean(copy=False, readonly=True, index=True)
    metanow_fiscal_restored = fields.Boolean(
        string="Rikthyer nga efis / Restored from efis", copy=False, readonly=True, index=True)
    metanow_fiscal_restore_note = fields.Text(
        string="Rikthimi nga efis / Restore from efis", copy=False, readonly=True)
    # Restored, and its payments not sent yet: a restore only reads, so a person sends them.
    metanow_fiscal_payments_review = fields.Boolean(copy=False, readonly=True, index=True)
    metanow_fiscal_correction_kind = fields.Selection(
        CORRECTION_KINDS,
        string="Nota e kreditit për efis / Credit note for efis",
        copy=False,
        help="Gabim në faturë: fatura kishte të dhëna të gabuara. Kthim ose zbritje: një rregullim i mëvonshëm. "
             "/ Mistake on the invoice: the invoice had wrong data. Return or discount: a later adjustment.")

    @api.depends("metanow_fiscal_payment_records")
    def _compute_metanow_fiscal_payment_records_summary(self):
        for move in self:
            lines = []
            for record in move._metanow_fiscal_held_records():
                state = (f"depozituar / filed {record.get('filedReference') or ''}".strip()
                         if record.get("filedAt") else "pa depozituar / not filed")
                lines.append(f"{record['paidOn']}  {record['amount']}  "
                             f"afati / due {record['dueBy']}  {state}")
            move.metanow_fiscal_payment_records_summary = "\n".join(lines) or False

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
            return False
        try:
            client = self.company_id._metanow_fiscal_client()
            response = client.get_document(self.metanow_fiscal_document_id)
        except (efis_client.EfisUnavailable, UserError) as error:
            _logger.info("efis read of %s failed: %s", self.metanow_fiscal_document_id, error)
            return False
        einvoice = None
        body = response.body if isinstance(response.body, dict) else {}
        # The e-invoice is exchanged only after fiscalisation, so it is read once efis says the
        # document is registered. Both answers go into the one write.
        registered = 200 <= response.status < 300 and outcome.odoo_state(body.get("state")) == outcome.REGISTERED
        # DEV-2443: a restored invoice efis had not registered yet could not say whether it went as
        # an e-invoice (efis builds the e-invoice only after registration), so it is read once here.
        restored = self.metanow_fiscal_restored and not self.metanow_fiscal_electronic
        extra = None
        if (self.metanow_fiscal_electronic or restored) and registered:
            try:
                einvoice = client.get_document_einvoice(self.metanow_fiscal_document_id)
            except efis_client.EfisUnavailable as error:
                _logger.info("efis e-invoice read of %s failed: %s", self.metanow_fiscal_document_id, error)
            if restored and einvoice is not None and einvoice.status == 200:
                extra = {outcome.ELECTRONIC: True}
            elif restored:
                einvoice = None
        self._metanow_fiscal_apply(outcome.poll_values, response, extra=extra, einvoice=einvoice)

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
        noting = bool(values) and values.get(outcome.STATE) == outcome.REGISTERED and not self.metanow_fiscal_noted
        if noting:
            values["metanow_fiscal_noted"] = True
        self._metanow_fiscal_store(values)
        if noting:
            self._metanow_fiscal_post_note(REGISTERED_NOTE)
        if values and (values.get(outcome.STATE) in outcome.POLLED
                       or outcome.einvoice_pending(self._metanow_fiscal_current())):
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_poll")

    @api.model
    def _cron_metanow_fiscal_send(self):
        for move in self.search([(outcome.STATE, "=", outcome.TO_SEND)], order="id", limit=BATCH):
            move._metanow_fiscal_send()

    @api.model
    def _cron_metanow_fiscal_poll(self):
        # DEV-1704: first the documents efis posted an event about, which may be registered
        # already (a corrective superseded one, the buyer refused its e-invoice).
        domain = [("metanow_fiscal_refresh", "=", True), (outcome.DOCUMENT_ID, "!=", False)]
        for move in self.search(domain, order="id", limit=BATCH):
            if move._metanow_fiscal_poll() is not False:
                move.write({"metanow_fiscal_refresh": False})
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

    def _metanow_fiscal_post_note(self, heading):
        """The fiscal values as a chatter note: readable in Odoo even without the module."""
        self.ensure_one()
        text = restore.note(self._metanow_fiscal_current())
        self.message_post(body=plaintext2html(f"{heading}\n{text}" if text else heading))

    # ------------------------------------------------------------------ restore (DEV-2443)

    def _metanow_fiscal_restore_values(self):
        """The invoice as plain values for restore.match."""
        self.ensure_one()
        partner = self.commercial_partner_id
        return {
            "key": self.id,
            "reference": self.name,
            "amount_total": self.amount_total,
            "credit_note": self.move_type == CREDIT_NOTE,
            "currency": self.currency_id.name,
            "buyer_identifier": partner.vat if partner else None,
        }

    @api.model
    def _metanow_fiscal_restore_candidates(self, company):
        """The company's posted customer invoices that carry no fiscal state at all: the ones an
        uninstall emptied, and the ones posted before fiscalisation was on. An invoice this
        installation queued or sent is never one of them."""
        return self.search([
            ("company_id", "=", company.id), ("move_type", "in", list(FISCALISED_TYPES)),
            ("state", "=", "posted"), (outcome.STATE, "=", False), (outcome.DOCUMENT_ID, "=", False),
        ], order="id")

    @api.model
    def _metanow_fiscal_linked(self, document_ids):
        """The efis document ids, among these, that an Odoo invoice already stores."""
        if not document_ids:
            return set()
        moves = self.search([(outcome.DOCUMENT_ID, "in", sorted(document_ids))])
        return {move.metanow_fiscal_document_id for move in moves}

    @api.model
    def _metanow_fiscal_restore(self, company):
        """One run of the restore for a company: read every efis document, restore at most
        RESTORE_BATCH invoices, and note why each other one was left alone. Returns True when
        nothing is left to restore. Only reads from efis; never sends."""
        client = company._metanow_fiscal_client()

        def read_page(after, limit):
            response = client.list_documents(after=after, limit=limit)
            if response.status != 200 or not isinstance(response.body, dict):
                raise UserError(
                    f"efis nuk dha listën e dokumenteve ({response.status}). "
                    f"/ efis did not return the list of documents ({response.status}).")
            return response.body

        try:
            documents = restore.pages(read_page)
        except efis_client.EfisUnavailable as error:
            raise UserError(f"efis nuk u përgjigj. / efis did not answer. ({error})") from error
        invoices = {move.id: move for move in self._metanow_fiscal_restore_candidates(company)}
        linked = self._metanow_fiscal_linked({document["id"] for document in documents if document.get("id")})
        matches, skipped = restore.match(
            [move._metanow_fiscal_restore_values() for move in invoices.values()], documents, linked)

        notes = {}
        for key, document in matches[:RESTORE_BATCH]:
            notes[key] = invoices[key]._metanow_fiscal_restore_one(client, document)
        # An invoice dated before efis's first document was never sent to it: it was posted
        # before fiscalisation was turned on. Only the others are listed as not found.
        days = sorted(str(document["issuedAt"])[:10] for document in documents if document.get("issuedAt"))
        first_day = days[0] if days else None
        for key, reason in skipped:
            dated = invoices[key].invoice_date
            dated = dated.isoformat() if dated else None
            unsent = reason == restore.NO_MATCH and (first_day is None or (dated is not None and dated < first_day))
            notes[key] = None if unsent else reason
        for key, reason in notes.items():
            move = invoices[key]
            if move.metanow_fiscal_restored:
                continue
            text = restore.REASONS[reason] if reason else False
            if (move.metanow_fiscal_restore_note or False) != text:
                move.write({"metanow_fiscal_restore_note": text})
        return len(matches) <= RESTORE_BATCH

    def _metanow_fiscal_restore_one(self, client, document):
        """Fill this invoice from its efis document in one write. Returns why it was not
        restored, or None."""
        self.ensure_one()
        if not self._metanow_fiscal_lock() or self.metanow_fiscal_state or self.metanow_fiscal_document_id:
            return None  # Another worker has it, or it was queued since the list was read.
        try:
            einvoice = client.get_document_einvoice(document["id"])
        except efis_client.EfisUnavailable:
            return restore.EINVOICE_UNREAD
        if einvoice.status not in (200, 404):
            return restore.EINVOICE_UNREAD
        current = self._metanow_fiscal_current()
        try:
            values = outcome.document_values(current, document)
            # 404: efis built no e-invoice. For a document not registered yet that is not certain,
            # so the poll reads it once more when efis registers it (metanow_fiscal_restored).
            values[outcome.ELECTRONIC] = einvoice.status == 200
            if einvoice.status == 200:
                values.update(outcome.einvoice_values(current, einvoice) or {})
        except outcome.SecurityCodeConflict as error:
            _logger.error("efis document conflicts with invoice %s on restore: %s", self.id, error)
            self._metanow_fiscal_store({outcome.STATE: outcome.PARKED, outcome.ERROR: f"conflict: {error}"})
            return restore.CONFLICT
        values.update({
            "metanow_fiscal_restored": True,
            "metanow_fiscal_restore_note": False,
            "metanow_fiscal_noted": True,
            # A restore only reads (decided 12 Nov 2026). The invoice's payments are not sent:
            # a person reviews what came back, then sends them (action_metanow_fiscal_send_payments).
            "metanow_fiscal_payments_review": self.move_type == "out_invoice",
        })
        self._metanow_fiscal_store(values)
        self._metanow_fiscal_post_note(RESTORED_NOTE)
        if values.get(outcome.STATE) in outcome.POLLED or outcome.einvoice_pending(self._metanow_fiscal_current()):
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_poll")
        return None

    @api.model
    def _cron_metanow_fiscal_health(self):
        """DEV-1704: the certificate's state from efis, daily and whenever efis posts that it is
        running out; and the country profile again when efis posts that it changed."""
        companies = self.env["res.company"].sudo().search([
            ("metanow_fiscal_enabled", "=", True), ("metanow_fiscal_tenant", "!=", False)])
        for company in companies:
            company._metanow_fiscal_refresh_health()

    @api.model
    def _metanow_fiscal_backfill_notes(self):
        """One chatter note per invoice efis registered before notes existed (decided 12 Nov
        2026): the chatter stays when the module is uninstalled, so an upgrade before an uninstall
        keeps every registered invoice's values readable. Reads nothing from efis and sends
        nothing. Returns how many notes it posted."""
        domain = [(outcome.DOCUMENT_ID, "!=", False), (outcome.STATE, "in", [outcome.REGISTERED, outcome.SUPERSEDED]),
                  ("metanow_fiscal_noted", "=", False)]
        posted = 0
        for move in self.search(domain, order="id"):
            move.write({"metanow_fiscal_noted": True})
            move._metanow_fiscal_post_note(BACKFILLED_NOTE)
            posted += 1
        return posted

    @api.model
    def _cron_metanow_fiscal_restore(self):
        companies = self.env["res.company"].sudo().search([("metanow_fiscal_restore_pending", "=", True)])
        for company in companies:
            try:
                done = self.with_company(company).sudo()._metanow_fiscal_restore(company)
            except UserError as error:
                _logger.info("efis restore for company %s did not run: %s", company.id, error)
                company._metanow_fiscal_restore_finished(str(error))
                continue
            if done:
                company._metanow_fiscal_restore_finished()
            else:
                self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_restore")

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
        # DEV-2433: a draft bill made from a received e-invoice and deleted is not made again.
        for move in self:
            if move.metanow_fiscal_inbound_id:
                move.company_id.sudo()._metanow_fiscal_ignore_inbound(move.metanow_fiscal_inbound_id)
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

    # ------------------------------------------------------------------ payments (DEV-2432)

    def _metanow_fiscal_payments_apply(self):
        """Whether efis tracks this invoice's payments: an invoice efis has numbered, not refused."""
        self.ensure_one()
        return (self.move_type == "out_invoice" and bool(self.company_id.metanow_fiscal_enabled)
                and self.metanow_fiscal_state not in (outcome.REJECTED, outcome.REFUSED))

    def _metanow_fiscal_mark_payments(self):
        """Odoo's payments on these invoices changed: send them to efis on the next pass."""
        invoices = self.filtered(lambda move: move.move_type == "out_invoice" and move.company_id.metanow_fiscal_enabled)
        if invoices:
            # A payment a person registers or undoes is theirs to send; a restored invoice's review
            # ends with it.
            invoices.sudo().write({"metanow_fiscal_payments_pending": True, "metanow_fiscal_payment_records_pending": True,
                                   "metanow_fiscal_payments_review": False})
            self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_payments")

    def _metanow_fiscal_wanted_payments(self):
        """The payments Odoo holds on this invoice, as efis would record them, keyed by reference.

        Money that came in counts: a reconciliation with a payment or a bank statement line, and a
        payment Odoo recorded without a journal entry. A reconciliation with a credit note does not
        count; efis already takes the credit note off what is owed.
        """
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_str("database.uuid") or "db"
        profile = self.company_id._metanow_fiscal_profile()
        known = profile.codes(PAYMENT_METHODS) if profile is not None else set()
        wanted = {}
        problems = []

        def add(kind, record_id, amount, paid_on, journal):
            method = (journal.metanow_fiscal_payment_method or "").strip().upper()
            if not method:
                problems.append(
                    f"Ditari {journal.name} nuk ka mënyrë pagese për efis. / The journal {journal.name} has no efis payment method.")
                return
            if method not in known:
                problems.append(
                    f"Mënyra e pagesës {method} e ditarit {journal.name} nuk njihet nga efis. "
                    f"/ efis does not know the payment method {method} of the journal {journal.name}.")
                return
            wanted[receivables.reference(database, kind, record_id)] = {
                "amount": money.format_money(money.cents(amount)),
                "paidOn": paid_on.isoformat(),
                "method": method,
            }

        received, problems = self._metanow_fiscal_payments_received()
        for kind, record_id, amount, paid_on, journal in received:
            add(kind, record_id, amount, paid_on, journal)
        return wanted, problems

    def _metanow_fiscal_payments_received(self):
        """The money that came in on this invoice: (kind, record id, amount, day, journal) each.

        A reconciliation with a payment or a bank statement line, and a payment Odoo recorded
        without a journal entry for this one invoice. A reconciliation with a credit note is not
        money received.
        """
        self.ensure_one()
        received = []
        problems = []
        receivable_lines = self.line_ids.filtered(lambda line: line.account_id.account_type == "asset_receivable")
        for partial in receivable_lines.matched_credit_ids:
            counterpart = partial.credit_move_id.move_id
            payment = counterpart.origin_payment_id
            if not payment and not counterpart.statement_line_id:
                continue
            paid_on = payment.date if payment else counterpart.date
            received.append(("rec", partial.id, partial.amount, paid_on, counterpart.journal_id))
        for payment in self.matched_payment_ids:
            if payment.move_id or payment.state not in ("paid", "reconciled"):
                continue
            invoices = payment.invoice_ids.filtered(lambda move: move.move_type == "out_invoice")
            if len(invoices) != 1:
                problems.append(
                    f"Pagesa {payment.name} paguan disa fatura pa regjistrim kontabël, prandaj nuk ndahet dot; "
                    "vendosni një llogari në pritje në ditarin e saj. / The payment "
                    f"{payment.name} pays several invoices without a journal entry, so it cannot be split; "
                    "give its journal an outstanding account.")
                continue
            received.append(("pay", payment.id, payment.amount, payment.date, payment.journal_id))
        return received, problems

    def _metanow_fiscal_sync_payments(self, today=None):
        """One pass: make efis's payments on this invoice what Odoo's are."""
        self.ensure_one()
        if not self._metanow_fiscal_lock() or not self.metanow_fiscal_payments_pending:
            return
        if not self._metanow_fiscal_payments_apply():
            self._metanow_fiscal_store({"metanow_fiscal_payments_pending": False, "metanow_fiscal_payments_error": False})
            return
        if not self.metanow_fiscal_document_id:
            return  # Not numbered yet: a later pass records them.
        wanted, problems = self._metanow_fiscal_wanted_payments()
        if problems:
            # Odoo's own setup is wrong. Nothing is sent until a person fixes it and sends again.
            self._metanow_fiscal_store({"metanow_fiscal_payments_pending": False,
                                        "metanow_fiscal_payments_error": "\n".join(problems)})
            return
        document = self.metanow_fiscal_document_id
        database = self.env["ir.config_parameter"].sudo().get_str("database.uuid") or "db"
        try:
            client = self.company_id._metanow_fiscal_client()
            listing = client.list_payments_received(document)
            if listing.status != 200:
                return self._metanow_fiscal_payments_failed(listing)
            held = (listing.body or {}).get("payments") or []
            withdraw, record, waiting = receivables.plan(
                wanted, held, database, today or fields.Date.context_today(self))
            # Withdrawn first, so a payment recorded again is never counted twice against what is owed.
            for payment_id in withdraw:
                answer = client.withdraw_payment_received(payment_id)
                if answer.status not in (200, 404):
                    return self._metanow_fiscal_payments_failed(answer)
            for payment in record:
                answer = client.record_payment_received(document, payment)
                if answer.status != 201:
                    return self._metanow_fiscal_payments_failed(answer)
        except efis_client.EfisUnavailable as error:
            # No answer: the next pass reads what efis holds and carries on from there.
            self._metanow_fiscal_store({"metanow_fiscal_payments_error": f"efis_unavailable: {error}"})
            return
        except UserError as error:
            self._metanow_fiscal_store({"metanow_fiscal_payments_pending": False, "metanow_fiscal_payments_error": str(error)})
            return
        self._metanow_fiscal_store({"metanow_fiscal_payments_pending": waiting, "metanow_fiscal_payments_error": False})

    def _metanow_fiscal_payments_failed(self, answer):
        """efis would not do it. A refusal of the request (4xx) is never retried; efis's own trouble is."""
        body = answer.body if isinstance(answer.body, dict) else {}
        code = body.get("code") or str(answer.status)
        message = PAYMENT_REFUSALS.get(code, f"efis: {code}")
        retry = answer.status >= 500 or answer.status == 429
        self._metanow_fiscal_store({"metanow_fiscal_payments_pending": retry, "metanow_fiscal_payments_error": message})

    @api.model
    def _cron_metanow_fiscal_payments(self):
        for move in self.search([("metanow_fiscal_payments_pending", "=", True)], order="id", limit=BATCH):
            move._metanow_fiscal_sync_payments()
        # DEV-2434: records to make, and records still to be filed, read again so the invoice says
        # when a person has filed them in efis.
        domain = ["|", ("metanow_fiscal_payment_records_pending", "=", True), ("metanow_fiscal_payment_record_due", "!=", False)]
        for move in self.search(domain, order="id", limit=BATCH):
            move._metanow_fiscal_sync_payment_records()

    def action_metanow_fiscal_send_payments(self):
        """Send this invoice's payments to efis again, after a person has fixed what was wrong."""
        for move in self:
            if move.move_type == "out_invoice":
                move.write({"metanow_fiscal_payments_pending": True, "metanow_fiscal_payment_records_pending": True,
                            "metanow_fiscal_payments_review": False})
        self._metanow_fiscal_trigger("metanow_fiscalization.ir_cron_metanow_fiscal_payments")
        return True

    # ------------------------------------------------------------------ payment records (DEV-2434)

    def _metanow_fiscal_held_records(self):
        try:
            return json.loads(self.metanow_fiscal_payment_records or "[]")
        except ValueError:
            return []

    def _metanow_fiscal_records_apply(self):
        """An e-invoice the authority registered: its cash payments are Instruction 24's."""
        self.ensure_one()
        return (self.move_type == "out_invoice" and bool(self.company_id.metanow_fiscal_enabled)
                and bool(self.metanow_fiscal_electronic) and bool(self.metanow_fiscal_document_id)
                and bool(self.metanow_fiscal_registration_code) and bool(self.metanow_fiscal_security_code))

    def _metanow_fiscal_cash_payments(self):
        """The cash payments on this invoice, as a payment record names them. Only a cash journal's
        payment is one no bank reports; any other cause is a person's to record in efis."""
        received, problems = self._metanow_fiscal_payments_received()
        cash = [
            {"amount": money.format_money(money.cents(amount)), "paidOn": paid_on.isoformat()}
            for _, _, amount, paid_on, journal in received if journal.type == "cash"
        ]
        return cash, problems

    def _metanow_fiscal_sync_payment_records(self):
        """One pass: make the records of this e-invoice's cash payments, and read back their state."""
        self.ensure_one()
        if not self._metanow_fiscal_lock():
            return
        if not (self.metanow_fiscal_payment_records_pending or self.metanow_fiscal_payment_record_due):
            return
        if not self._metanow_fiscal_records_apply():
            if not self.metanow_fiscal_document_id and self.metanow_fiscal_state not in (outcome.REJECTED, outcome.REFUSED):
                return  # Not numbered yet: a later pass makes them.
            self._metanow_fiscal_store({"metanow_fiscal_payment_records_pending": False, "metanow_fiscal_payment_record_due": False})
            return
        wanted, problems = self._metanow_fiscal_cash_payments()
        buyer = "".join((self.commercial_partner_id.vat or "").split())
        if wanted and not buyer:
            problems.append("Blerësi nuk ka numër tatimor. / The buyer has no tax number.")
        if problems:
            self._metanow_fiscal_store({"metanow_fiscal_payment_records_pending": False,
                                        "metanow_fiscal_payment_records_note": "\n".join(problems)})
            return
        known = self._metanow_fiscal_held_records()
        if not wanted and not known:
            self._metanow_fiscal_store({"metanow_fiscal_payment_records_pending": False})
            return
        document = self.metanow_fiscal_document_id
        days = sorted({payment["paidOn"] for payment in wanted} | {record["paidOn"] for record in known})
        try:
            client = self.company_id._metanow_fiscal_client()
            # efis lists by the day each record is due, the 10th of the month after the payment;
            # from the first payment's day to well after the last one covers them all.
            listing = client.list_payment_records(days[0], (date.fromisoformat(days[-1]) + timedelta(days=45)).isoformat())
            if listing.status != 200:
                return self._metanow_fiscal_records_failed(listing)
            held = [record for record in (listing.body or {}).get("records") or []
                    if record.get("documentId") == document and record.get("cause") == payment_records.CAUSE]
            create, extra = payment_records.plan(wanted, held)
            for payment in create:
                answer = client.create_payment_record({
                    "documentId": document,
                    "buyerTaxId": buyer,
                    # Not named by Instruction 24 for a cash payment; efis's own console sends none.
                    "providerTaxId": None,
                    "registrationCode": self.metanow_fiscal_registration_code,
                    "securityCode": self.metanow_fiscal_security_code,
                    "amount": payment["amount"],
                    "paidOn": payment["paidOn"],
                    "cause": payment_records.CAUSE,
                })
                if answer.status != 201:
                    self._metanow_fiscal_store({"metanow_fiscal_payment_records": json.dumps(held)})
                    return self._metanow_fiscal_records_failed(answer)
                held.append(answer.body)
        except efis_client.EfisUnavailable as error:
            # No answer: the next pass reads what efis holds and makes only what is missing.
            self._metanow_fiscal_store({"metanow_fiscal_payment_records_note": f"efis_unavailable: {error}"})
            return
        except UserError as error:
            self._metanow_fiscal_store({"metanow_fiscal_payment_records_pending": False,
                                        "metanow_fiscal_payment_records_note": str(error)})
            return
        extra_ids = {record.get("id") for record in extra}
        open_days = [record["dueBy"] for record in held if not record.get("filedAt") and record.get("id") not in extra_ids]
        # efis has no way to take a record back. Say which ones Odoo no longer has a payment for:
        # one not filed yet must not be filed; one already filed must be put right on the platform.
        notes = []
        unfiled = [record for record in extra if not record.get("filedAt")]
        filed = [record for record in extra if record.get("filedAt")]
        if unfiled:
            notes.append("Odoo nuk i ka më këto pagesa me para në dorë; mos i depozitoni regjistrimet e tyre. "
                         "/ Odoo no longer has these cash payments; do not file their records.\n"
                         + "\n".join(f"{record['paidOn']}  {record['amount']}" for record in unfiled))
        if filed:
            notes.append("Këto regjistrime janë depozituar, por Odoo nuk i ka më pagesat; korrigjojini në platformë. "
                         "/ These records were filed, but Odoo no longer has their payments; put them right on the platform.\n"
                         + "\n".join(f"{record['paidOn']}  {record['amount']}  {record.get('filedReference') or ''}".rstrip()
                                     for record in filed))
        note = "\n\n".join(notes) or None
        self._metanow_fiscal_store({
            "metanow_fiscal_payment_records_pending": False,
            "metanow_fiscal_payment_records": json.dumps(held, sort_keys=True),
            "metanow_fiscal_payment_record_due": min(open_days) if open_days else False,
            "metanow_fiscal_payment_records_note": note or False,
        })

    def _metanow_fiscal_records_failed(self, answer):
        body = answer.body if isinstance(answer.body, dict) else {}
        code = body.get("code") or str(answer.status)
        retry = answer.status >= 500 or answer.status == 429
        self._metanow_fiscal_store({"metanow_fiscal_payment_records_pending": retry,
                                    "metanow_fiscal_payment_records_note": f"efis: {code}"})

    # ------------------------------------------------------------------ received e-invoices (DEV-2433)

    @api.model
    def _cron_metanow_fiscal_inbound(self):
        companies = self.env["res.company"].sudo().search([
            ("metanow_fiscal_enabled", "=", True), ("metanow_fiscal_inbound_enabled", "=", True),
            ("metanow_fiscal_tenant", "!=", False)])
        for company in companies:
            try:
                self.with_company(company).sudo()._metanow_fiscal_import_inbound(company)
            except (efis_client.EfisUnavailable, UserError) as error:
                _logger.info("efis received e-invoices not read for company %s: %s", company.id, error)

    @api.model
    def _metanow_fiscal_import_inbound(self, company):
        """Make a draft bill for each e-invoice received since the company turned this on, and keep
        the bills already made in step with what efis says about them."""
        client = company._metanow_fiscal_client()
        response = client.list_inbound()
        if response.status != 200:
            _logger.info("efis did not list received e-invoices for company %s: %s", company.id, response.status)
            return
        views = (response.body or {}).get("einvoices") or []
        known = {move.metanow_fiscal_inbound_id: move for move in self.search([
            ("company_id", "=", company.id), ("metanow_fiscal_inbound_id", "in", [view.get("id") for view in views])])}
        ignored = company._metanow_fiscal_inbound_ignored()
        since = company.metanow_fiscal_inbound_since
        for view in views:
            current = efis_inbound.values(view)
            bill = known.get(current["id"])
            if bill:
                bill._metanow_fiscal_inbound_refresh(current, client)
            elif current["id"] not in ignored and since and (efis_inbound.received_on(view) or since) >= since:
                self._metanow_fiscal_inbound_create(company, current, client)

    @api.model
    def _metanow_fiscal_inbound_create(self, company, current, client):
        notes = ["efis nuk dërgon rreshtat e faturës; shkruajini nga PDF-ja. "
                 "/ efis does not send the invoice's lines; enter them from the PDF."]
        partner = self._metanow_fiscal_inbound_partner(company, current["seller"])
        if not partner:
            notes.append(f"Asnjë kontakt me numrin tatimor {current['seller']}; zgjidhni furnitorin. "
                         f"/ No contact has the tax number {current['seller']}; choose the supplier.")
        values = {
            "move_type": "in_invoice",
            "company_id": company.id,
            "partner_id": partner.id if partner else False,
            "ref": current["number"],
            "metanow_fiscal_inbound_id": current["id"],
            "metanow_fiscal_inbound_eic": current["eic"],
            "metanow_fiscal_inbound_seller": current["seller"],
            "metanow_fiscal_inbound_amount": current["amount"],
            "metanow_fiscal_inbound_status": current["status"],
            "metanow_fiscal_inbound_answer_by": current["answer_by"],
            "metanow_fiscal_inbound_waiting": current["waiting"],
            "metanow_fiscal_inbound_decision": current["decision"],
            "metanow_fiscal_inbound_fiscalised": current["fiscalised"],
            "metanow_fiscal_inbound_pdf_pending": current["has_pdf"],
        }
        if current["due_on"]:
            values["invoice_date_due"] = current["due_on"]
        if current["currency"] and current["currency"] != company.currency_id.name:
            currency = self.env["res.currency"].with_context(active_test=False).search([("name", "=", current["currency"])], limit=1)
            if currency:
                values["currency_id"] = currency.id
            else:
                notes.append(f"Monedha {current['currency']} nuk gjendet në Odoo. / The currency {current['currency']} is not in Odoo.")
        values["metanow_fiscal_inbound_note"] = "\n".join(notes)
        bill = self.with_company(company).create(values)
        bill._metanow_fiscal_inbound_attach_pdf(client)
        return bill

    @api.model
    def _metanow_fiscal_inbound_partner(self, company, seller):
        if not seller:
            return self.env["res.partner"]
        candidates = self.env["res.partner"].search([
            ("vat", "!=", False), ("vat", "ilike", seller[-9:]),
            "|", ("company_id", "=", False), ("company_id", "=", company.id)])
        matches = candidates.filtered(lambda partner: efis_inbound.tax_id(partner.vat) == seller)
        commercial = matches.commercial_partner_id
        return commercial[:1] if len(commercial) == 1 else self.env["res.partner"]

    def _metanow_fiscal_inbound_attach_pdf(self, client):
        self.ensure_one()
        if not self.metanow_fiscal_inbound_pdf_pending:
            return
        answer = client.get_inbound_pdf(self.metanow_fiscal_inbound_id)
        content = answer.body if isinstance(answer.body, (bytes, bytearray)) else b""
        if answer.status != 200 or not content.startswith(b"%PDF"):
            return  # Tried again on the next pass.
        self.env["ir.attachment"].create({
            "name": f"{self.ref or self.metanow_fiscal_inbound_eic or 'e-invoice'}.pdf",
            "res_model": "account.move",
            "res_id": self.id,
            "raw": bytes(content),
            "mimetype": "application/pdf",
        })
        self.write({"metanow_fiscal_inbound_pdf_pending": False})

    def _metanow_fiscal_inbound_refresh(self, current, client=None):
        self.ensure_one()
        changes = {
            name: value for name, value in (
                ("metanow_fiscal_inbound_status", current["status"]),
                ("metanow_fiscal_inbound_answer_by", current["answer_by"]),
                ("metanow_fiscal_inbound_waiting", current["waiting"]),
                ("metanow_fiscal_inbound_decision", current["decision"]),
                ("metanow_fiscal_inbound_fiscalised", current["fiscalised"]),
            )
            if (self[name] or False) != (value or False)
        }
        if changes:
            self.write(changes)
        if client is not None and self.metanow_fiscal_inbound_pdf_pending:
            try:
                self._metanow_fiscal_inbound_attach_pdf(client)
            except efis_client.EfisUnavailable:
                pass

    def action_metanow_fiscal_accept(self):
        return self._metanow_fiscal_decide(efis_inbound.ACCEPT)

    def action_metanow_fiscal_refuse(self):
        return self._metanow_fiscal_decide(efis_inbound.REFUSE)

    def _metanow_fiscal_decide(self, decision):
        """Send the buyer's answer. efis queues it and sends it once; it can never be withdrawn,
        and a second answer is refused (decision_exists), so pressing again is safe."""
        for bill in self:
            if not bill.metanow_fiscal_inbound_id:
                continue
            client = bill.company_id._metanow_fiscal_client()
            try:
                answer = client.decide_inbound(bill.metanow_fiscal_inbound_id, decision)
            except efis_client.EfisUnavailable as error:
                raise UserError(f"efis nuk u përgjigj; provoni përsëri. / efis did not answer; try again. ({error})") from error
            body = answer.body if isinstance(answer.body, dict) else {}
            code = body.get("code")
            if answer.status == 422 and code == "einvoice_not_fiscalised":
                raise UserError(
                    "Kjo e-faturë nuk është fiskalizuar nga shitësi, prandaj nuk pranohet; mund ta refuzoni. "
                    "/ The seller has not fiscalised this e-invoice, so it cannot be accepted; you may refuse it.")
            if answer.status not in (202, 409):
                raise UserError(f"efis nuk e mori përgjigjen. / efis did not take the answer. ({code or answer.status})")
            # Read back what efis now holds, whether this answer or one given before (409).
            detail = client.get_inbound(bill.metanow_fiscal_inbound_id)
            view = (detail.body or {}).get("einvoice") if detail.status == 200 and isinstance(detail.body, dict) else None
            if view:
                bill._metanow_fiscal_inbound_refresh(efis_inbound.values(view))
            elif answer.status == 202:
                bill.write({"metanow_fiscal_inbound_waiting": False,
                            "metanow_fiscal_inbound_decision": efis_inbound.decision_text(body)})
        return True
