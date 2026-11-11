import json
import logging
from datetime import datetime

import pytz

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.metanow_fiscalization.efis import client as efis_client

from ..efis import transport
from .stock_warehouse import PLACE_KINDS

_logger = logging.getLogger(__name__)

# The day and the offset are Albania's: a note is issued today there (Art. 39(3)).
TIRANA = pytz.timezone("Europe/Tirane")

WTN_STATES = [
    ("to_send", "Në radhë për efis / Queued for efis"),
    ("sent", "Numëruar nga efis / Numbered by efis"),
    ("deferred", "Shtyrë / Deferred"),
    ("registered", "Regjistruar / Registered"),
    ("rejected", "Refuzuar nga autoriteti / Rejected by the authority"),
    ("refused", "Refuzuar nga efis, pa numër / Refused by efis, no number spent"),
    ("parked", "Në pritje të veprimit / Waiting for action"),
]
# efis may hold a note in these: nothing about it changes, and it is never issued again.
HELD = ("to_send", "sent", "deferred", "registered", "rejected")
POLLED = ("sent", "deferred")
# Written once, from efis's answer.
WRITE_ONCE = (
    "metanow_fiscal_wtn_document_id", "metanow_fiscal_wtn_number", "metanow_fiscal_wtn_security_code",
    "metanow_fiscal_wtn_registration_code",
)
# What the note was issued with. Never changed once efis may hold it.
NOTE_FIELDS = (
    "metanow_fiscal_vehicle_plates", "metanow_fiscal_vehicle_ownership", "metanow_fiscal_goods_flammable",
    "metanow_fiscal_escort_required", "metanow_fiscal_destination_kind", "metanow_fiscal_carrier_id",
)
BATCH = 50


class StockPicking(models.Model):
    _inherit = "stock.picking"

    metanow_fiscal_wtn_state = fields.Selection(
        WTN_STATES, string="Fatura shoqëruese / Transfer note", copy=False, readonly=True, index=True)
    metanow_fiscal_wtn_document_id = fields.Char(string="Dokumenti në efis / efis document", copy=False, readonly=True)
    metanow_fiscal_wtn_number = fields.Char(string="Numri / Number", copy=False, readonly=True)
    metanow_fiscal_wtn_security_code = fields.Char(
        string="Kodi i sigurisë së lëshuesit / Issuer security code", copy=False, readonly=True)
    metanow_fiscal_wtn_registration_code = fields.Char(
        string="Kodi i regjistrimit / Registration code", copy=False, readonly=True)
    metanow_fiscal_wtn_issued_at = fields.Char(string="Lëshuar më / Issued at", copy=False, readonly=True)
    metanow_fiscal_wtn_key = fields.Char(copy=False, readonly=True)
    metanow_fiscal_wtn_attempt = fields.Integer(copy=False, readonly=True)
    # The request as it was sent, kept: a resend carries exactly these bytes under the same key.
    metanow_fiscal_wtn_request = fields.Text(copy=False, readonly=True)
    metanow_fiscal_wtn_error = fields.Text(string="Gabimi / Error", copy=False, readonly=True)

    metanow_fiscal_vehicle_plates = fields.Char(string="Targat e mjetit / Vehicle plates", copy=False)
    metanow_fiscal_vehicle_ownership = fields.Selection(
        [("OWNER", "Mjet i kompanisë / The company's vehicle"), ("THIRDPARTY", "Mjet i palës së tretë / A third party's vehicle")],
        string="Pronari i mjetit / Vehicle owner", copy=False)
    metanow_fiscal_goods_flammable = fields.Boolean(string="Mall i ndezshëm / Flammable goods", copy=False)
    metanow_fiscal_escort_required = fields.Boolean(string="Kërkon shoqërim / Escort required", copy=False)
    metanow_fiscal_destination_kind = fields.Selection(
        PLACE_KINDS, string="Lloji i vendit të destinacionit / Kind of destination", copy=False,
        help="Vetëm kur destinacioni nuk është magazinë e kompanisë. / Only when the destination is not one of the company's warehouses.")
    metanow_fiscal_carrier_id = fields.Many2one(
        "res.partner", string="Transportuesi / Carrier", copy=False,
        help="Bosh kur transporton vetë kompania. / Empty when the company carries the goods itself.")
    metanow_fiscal_wtn_applies = fields.Boolean(compute="_compute_metanow_fiscal_wtn_applies")

    @api.depends("picking_type_id.metanow_fiscal_wtn_transaction", "company_id.metanow_fiscal_enabled")
    def _compute_metanow_fiscal_wtn_applies(self):
        for picking in self:
            picking.metanow_fiscal_wtn_applies = bool(
                picking.company_id.metanow_fiscal_enabled and picking.picking_type_id.metanow_fiscal_wtn_transaction)

    # ------------------------------------------------------------------ the note's content

    def _metanow_fiscal_source_warehouse(self):
        self.ensure_one()
        return self.picking_type_id.warehouse_id or self.location_id.warehouse_id

    @staticmethod
    def _metanow_fiscal_address(partner):
        return {
            "address": " ".join(part for part in (partner.street, partner.street2) if part) or None,
            "city": partner.city or None,
        }

    def _metanow_fiscal_wtn_note(self, issued_at):
        """The transfer as plain values for the request body builder."""
        self.ensure_one()
        company = self.company_id
        source = self._metanow_fiscal_source_warehouse()
        origin = dict(self._metanow_fiscal_address(source.partner_id), kind=source.metanow_fiscal_place_kind) if source else {}
        target = self.location_dest_id.warehouse_id
        if target:
            destination = dict(self._metanow_fiscal_address(target.partner_id), kind=target.metanow_fiscal_place_kind)
        else:
            destination = dict(self._metanow_fiscal_address(self.partner_id), kind=self.metanow_fiscal_destination_kind) \
                if self.partner_id else {"kind": self.metanow_fiscal_destination_kind}
        carrier = self.metanow_fiscal_carrier_id
        if carrier and carrier.commercial_partner_id == company.partner_id.commercial_partner_id:
            carrier = carrier.browse()
        goods = []
        value = 0
        for move in self.move_ids:
            if move.state == "cancel":
                continue
            unit = getattr(move, "uom_id", False) or getattr(move, "product_uom", False)
            quantity = move.quantity or move.product_uom_qty
            goods.append({
                "name": move.product_id.display_name,
                "code": move.product_id.default_code,
                "unit": unit.name if unit else None,
                "quantity": quantity,
            })
            # DEV-2441 working guess (Gleni, 11 Nov 2026): the goods are valued at cost.
            in_product_unit = unit._compute_quantity(quantity, move.product_id.uom_id) if unit else quantity
            value += in_product_unit * move.product_id.standard_price
        return {
            "transaction": self.picking_type_id.metanow_fiscal_wtn_transaction,
            "issued_at": issued_at,
            "value_of_goods": company.currency_id.round(value),
            "vehicle_plates": self.metanow_fiscal_vehicle_plates,
            "vehicle_ownership": self.metanow_fiscal_vehicle_ownership,
            "flammable": self.metanow_fiscal_goods_flammable,
            "escort": self.metanow_fiscal_escort_required,
            "from": origin,
            "to": destination,
            "issuer": {"name": company.name, "address": " ".join(p for p in (company.street, company.street2) if p) or None,
                       "town": company.city},
            "carrier": {"name": carrier.name} if carrier else None,
            "goods": goods,
        }

    def _metanow_fiscal_wtn_config(self):
        self.ensure_one()
        company = self.company_id
        source = self._metanow_fiscal_source_warehouse()
        return {
            "business_unit_code": source.metanow_fiscal_business_unit_code if source else None,
            # The person who issues it, or the company's (DEV-2436).
            "operator_code": self.env.user._metanow_fiscal_operator_for(company),
            "software_code": company.metanow_fiscal_software_code,
        }

    def _metanow_fiscal_wtn_body(self, issued_at):
        self.ensure_one()
        try:
            body = transport.build(self._metanow_fiscal_wtn_note(issued_at), self._metanow_fiscal_wtn_config())
        except transport.TransportInvalid as error:
            raise UserError(
                f"{self.name}: efis nuk do ta pranonte këtë faturë shoqëruese. / efis would not accept this transfer note.\n"
                + "\n".join(error.problems)) from error
        registry = self.company_id._metanow_fiscal_registry()
        problems = registry.unconfirmed(body) if registry is not None else []
        if problems:
            raise UserError(f"{self.name}: " + "\n".join(problems))
        return body

    # ------------------------------------------------------------------ issuing

    @staticmethod
    def _metanow_fiscal_now():
        """Now in Tirana, to the second, with its offset (CLAUDE.md: explicit offset, no milliseconds)."""
        return datetime.now(TIRANA).replace(microsecond=0).isoformat()

    def action_metanow_fiscal_issue_wtn(self):
        """Issue the transfer note, today, immediately before the goods move (Art. 39(3)).

        Everything efis would refuse for a reason Odoo can see is refused here, and nothing is
        queued. The note is sent by the job that runs straight after, under a key that makes a
        resend return the same note, never a second one.
        """
        for picking in self:
            if not picking.metanow_fiscal_wtn_applies:
                raise UserError(f"{picking.name}: ky operacion nuk lëshon faturë shoqëruese. "
                                "/ This operation issues no transfer note.")
            if picking.metanow_fiscal_wtn_state in HELD:
                raise UserError(f"{picking.name}: fatura shoqëruese është lëshuar tashmë. / The transfer note has already been issued.")
            if picking.metanow_fiscal_wtn_request and picking._metanow_fiscal_wtn_adopt():
                continue  # efis had numbered the earlier one: it is the note.
            issued_at = self._metanow_fiscal_now()
            body = picking._metanow_fiscal_wtn_body(issued_at)
            attempt = (picking.metanow_fiscal_wtn_attempt or 0) + 1
            database = self.env["ir.config_parameter"].sudo().get_str("database.uuid") or "db"
            picking.write({
                "metanow_fiscal_wtn_state": "to_send",
                "metanow_fiscal_wtn_attempt": attempt,
                "metanow_fiscal_wtn_key": f"odoo-{database}-wtn-{picking.id}-{attempt}",
                "metanow_fiscal_wtn_request": json.dumps(body, ensure_ascii=False, sort_keys=True),
                "metanow_fiscal_wtn_issued_at": issued_at,
                "metanow_fiscal_wtn_error": False,
            })
        self._metanow_fiscal_trigger("metanow_fiscalization_stock.ir_cron_metanow_fiscal_wtn_send")
        return True

    def _metanow_fiscal_wtn_adopt(self):
        """Before a new attempt: did efis number the earlier one after all (an answer that never
        came back)? It is found by its issue time and business unit, the note's own identity."""
        self.ensure_one()
        earlier = json.loads(self.metanow_fiscal_wtn_request)
        try:
            listing = self.company_id._metanow_fiscal_client().list_transport_documents()
        except efis_client.EfisUnavailable as error:
            raise UserError(f"efis nuk u përgjigj; provoni përsëri. / efis did not answer; try again. ({error})") from error
        if listing.status != 200:
            raise UserError(f"efis nuk i dha faturat shoqëruese ({listing.status}). / efis did not list the transfer notes ({listing.status}).")
        notes = [note for note in (listing.body or {}).get("transportDocuments") or []
                 if note.get("businessUnitCode") == earlier.get("businessUnitCode")
                 and _same_moment(note.get("issuedAt"), earlier.get("issuedAt"))]
        # Two transfers issued from one unit in the same second share both, so a note another
        # transfer already holds is not this one's.
        held = set(self.sudo().search([("metanow_fiscal_wtn_document_id", "in", [note.get("id") for note in notes]),
                                       ("id", "!=", self.id)]).mapped("metanow_fiscal_wtn_document_id"))
        candidates = [note for note in notes if note.get("id") not in held]
        if len(candidates) > 1:
            raise UserError(
                f"{self.name}: efis ka më shumë se një faturë shoqëruese të lëshuar në të njëjtën sekondë nga kjo njësi; "
                "kontrollojini në efis para se ta lëshoni përsëri. / efis holds more than one transfer note issued "
                "in the same second from this unit; check them in efis before issuing again.")
        if candidates:
            self.write(dict(transport.answer_values(candidates[0]), metanow_fiscal_wtn_error=False))
            return True
        return False

    def _metanow_fiscal_trigger(self, xml_id):
        cron = self.env.ref(xml_id, raise_if_not_found=False)
        if cron:
            cron._trigger()

    def _metanow_fiscal_lock(self):
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM stock_picking WHERE id = %s FOR UPDATE SKIP LOCKED", [self.id])
        if not self.env.cr.fetchone():
            return False
        self.invalidate_recordset()
        return True

    def _metanow_fiscal_wtn_send(self):
        self.ensure_one()
        if not self._metanow_fiscal_lock() or self.metanow_fiscal_wtn_state != "to_send":
            return
        try:
            client = self.company_id._metanow_fiscal_client()
            answer = client.submit_transport_document(json.loads(self.metanow_fiscal_wtn_request), self.metanow_fiscal_wtn_key)
        except efis_client.EfisUnavailable as error:
            # No answer. The next pass resends the same body under the same key.
            self.write({"metanow_fiscal_wtn_error": f"efis_unavailable: {error}"})
            return
        except UserError as error:
            self.write({"metanow_fiscal_wtn_state": "parked", "metanow_fiscal_wtn_error": str(error)})
            return
        body = answer.body if isinstance(answer.body, dict) else {}
        if answer.status in (200, 202):
            self.write(dict(transport.answer_values(body), metanow_fiscal_wtn_error=False))
            if self.metanow_fiscal_wtn_state in POLLED:
                self._metanow_fiscal_trigger("metanow_fiscalization_stock.ir_cron_metanow_fiscal_wtn_poll")
            return
        code = body.get("code") or str(answer.status)
        if answer.status >= 500 or answer.status == 429:
            self.write({"metanow_fiscal_wtn_error": f"efis: {code}"})  # efis's trouble: sent again
        elif code in ("issued_at_not_today", "idempotency_key_reused"):
            # Not issued today, or the key names another note: a person issues it again, and the
            # check before that finds a note efis did number.
            self.write({"metanow_fiscal_wtn_state": "parked", "metanow_fiscal_wtn_error": f"efis: {code}"})
        else:
            # efis refused before numbering: no number was spent. Corrected and issued again.
            self.write({"metanow_fiscal_wtn_state": "refused", "metanow_fiscal_wtn_error": f"efis: {code}"})

    @api.model
    def _cron_metanow_fiscal_wtn_send(self):
        for picking in self.search([("metanow_fiscal_wtn_state", "=", "to_send")], order="id", limit=BATCH):
            picking._metanow_fiscal_wtn_send()

    @api.model
    def _cron_metanow_fiscal_wtn_poll(self):
        pickings = self.search([("metanow_fiscal_wtn_state", "in", list(POLLED)),
                                ("metanow_fiscal_wtn_document_id", "!=", False)], order="id", limit=BATCH)
        for company in pickings.company_id:
            try:
                listing = company._metanow_fiscal_client().list_transport_documents()
            except (efis_client.EfisUnavailable, UserError) as error:
                _logger.info("efis transfer notes not read for company %s: %s", company.id, error)
                continue
            if listing.status != 200:
                continue
            notes = {note.get("id"): note for note in (listing.body or {}).get("transportDocuments") or []}
            for picking in pickings.filtered(lambda p: p.company_id == company):
                note = notes.get(picking.metanow_fiscal_wtn_document_id)
                if note and picking._metanow_fiscal_lock():
                    values = {name: value for name, value in transport.answer_values(note).items()
                              if (picking[name] or False) != (value or False)}
                    if values:
                        picking.write(values)

    # ------------------------------------------------------------------ guards

    def write(self, vals):
        for picking in self:
            for name in WRITE_ONCE:
                if name in vals and picking[name] and vals[name] != picking[name]:
                    raise UserError(f"{picking.name}: {name} është ruajtur nga efis dhe nuk ndryshohet. "
                                    f"/ {name} was stored from efis and is never changed.")
            if picking.metanow_fiscal_wtn_state in HELD and any(
                    name in vals and _differs(picking[name], vals[name]) for name in NOTE_FIELDS):
                raise UserError(f"{picking.name}: fatura shoqëruese është lëshuar dhe nuk ndryshohet. "
                                "/ The transfer note has been issued and is never changed.")
        return super().write(vals)


def _differs(stored, value):
    stored = stored.id if hasattr(stored, "id") else stored
    return (stored or False) != (value or False)


def _same_moment(left, right):
    try:
        return datetime.fromisoformat(left) == datetime.fromisoformat(right)
    except (TypeError, ValueError):
        return False
