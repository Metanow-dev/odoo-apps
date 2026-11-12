import json
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..efis import client as efis_client
from ..efis import registry as efis_registry
from ..efis.profile import Profile
from ..efis.restore import CONFLICT, REASONS

_logger = logging.getLogger(__name__)

# The company's efis connection. The credential is what pairing returned for this one tenant:
# revocable in efis, readable only by Settings administrators here, and never a certificate
# or a key. The module holds neither.
PAIRING_FIELDS = (
    "metanow_fiscal_credential",
    "metanow_fiscal_tenant",
    "metanow_fiscal_channel",
)

# DEV-2436: the company's codes, each of the kind efis's registry confirms it under.
COMPANY_CODES = (
    ("metanow_fiscal_business_unit_code", efis_registry.BUSINESS_UNIT),
    ("metanow_fiscal_operator_code", efis_registry.OPERATOR),
    ("metanow_fiscal_software_code", efis_registry.SOFTWARE),
    ("metanow_fiscal_device_code", efis_registry.DEVICE),
)


class ResCompany(models.Model):
    _inherit = "res.company"

    metanow_fiscal_enabled = fields.Boolean(
        string="Fiskalizimi me efis / Fiscalisation with efis",
        help="Faturat e klientëve fiskalizohen nga efis kur postohen. "
             "/ Customer invoices are fiscalised by efis when they are posted.",
    )
    metanow_fiscal_api_url = fields.Char(
        string="Adresa e efis / efis address",
        help="Adresa bazë e API-së së efis, p.sh. https://efis.metanow.com "
             "/ The base address of efis's API.",
    )
    metanow_fiscal_credential = fields.Char(
        string="Kredenciali i efis / efis credential",
        groups="base.group_system",
        copy=False,
        readonly=True,
    )
    metanow_fiscal_tenant = fields.Char(
        string="Biznesi në efis / Business in efis",
        copy=False,
        readonly=True,
    )
    metanow_fiscal_channel = fields.Char(
        string="Sistemi klient në efis / Client system in efis",
        copy=False,
        readonly=True,
    )
    metanow_fiscal_business_unit_code = fields.Char(string="Kodi i njësisë së biznesit / Business unit code")
    metanow_fiscal_operator_code = fields.Char(string="Kodi i operatorit / Operator code")
    metanow_fiscal_software_code = fields.Char(string="Kodi i programit / Software code")
    metanow_fiscal_device_code = fields.Char(
        string="Kodi i pajisjes / Device code",
        help="Vetëm për faturat me para në dorë. / Only for cash invoices.",
    )
    metanow_fiscal_vat_status = fields.Selection(
        [
            ("in_vat", "Në TVSH / Registered for VAT"),
            ("not_in_vat", "Jo në TVSH / Not registered for VAT"),
        ],
        string="Statusi i TVSH / VAT status",
    )
    metanow_fiscal_einvoice = fields.Boolean(
        string="Fatura elektronike për bizneset / E-invoices to businesses",
        default=True,
        help="Një faturë pa para në dorë për një blerës me numër tatimor dërgohet si faturë elektronike. "
             "/ A non-cash invoice to a buyer with a tax number is sent as an e-invoice.",
    )
    metanow_fiscal_profile = fields.Text(
        string="Profili i vendit nga efis / efis country profile",
        copy=False,
        readonly=True,
    )
    # DEV-2433: received e-invoices become draft vendor bills, from the day this is turned on.
    metanow_fiscal_inbound_enabled = fields.Boolean(
        string="E-faturat e marra si fatura furnitori / Received e-invoices as vendor bills",
        help="Çdo e-faturë që një furnitor i dërgon biznesit bëhet një faturë furnitori në draft. "
             "/ Each e-invoice a supplier sends the business becomes a draft vendor bill.")
    metanow_fiscal_inbound_since = fields.Date(
        string="Nga data / From",
        help="Vetëm e-faturat e marra nga kjo ditë. / Only e-invoices received from this day.")
    metanow_fiscal_inbound_ignored = fields.Text(copy=False, readonly=True)
    metanow_fiscal_registry = fields.Text(
        string="Kodet e konfirmuara në efis / Codes confirmed in efis",
        copy=False,
        readonly=True,
    )
    # DEV-2443: fiscal values read back from efis into invoices an uninstall emptied.
    metanow_fiscal_restore_pending = fields.Boolean(copy=False, readonly=True)
    metanow_fiscal_restore_summary = fields.Text(
        string="Rikthimi i fundit nga efis / Last restore from efis", copy=False, readonly=True)

    @api.constrains(*(name for name, _ in COMPANY_CODES))
    def _check_metanow_fiscal_codes(self):
        # Only a code efis has confirmed may be chosen. Before the company has read its registry
        # from efis there is nothing to check against; posting checks again (account_move).
        for company in self:
            registry = company._metanow_fiscal_registry()
            if registry is None:
                continue
            problems = [
                f"{efis_registry.LABELS[kind]}: {company[name]}"
                for name, kind in COMPANY_CODES
                if company[name] and not registry.confirms(kind, company[name])
            ]
            if problems:
                raise ValidationError(
                    "Këto kode nuk janë konfirmuar në efis. / These codes are not confirmed in efis.\n"
                    + "\n".join(problems))

    def _metanow_fiscal_client(self, authenticated=True):
        self.ensure_one()
        if not self.metanow_fiscal_api_url:
            raise UserError("Vendosni adresën e efis. / Set the efis address.")
        try:
            if not authenticated:
                return efis_client.EfisClient(self.metanow_fiscal_api_url)
            company = self.sudo()
            return efis_client.EfisClient(
                self.metanow_fiscal_api_url,
                credential=company.metanow_fiscal_credential,
                tenant=company.metanow_fiscal_tenant,
            )
        except ValueError as error:
            raise UserError(str(error)) from error

    def _metanow_fiscal_pair(self, code):
        """Exchange a single-use pairing code from efis for this company's credential."""
        self.ensure_one()
        code = (code or "").strip()
        if not code:
            raise UserError("Shkruani kodin e çiftimit nga efis. / Enter the pairing code from efis.")
        try:
            response = self._metanow_fiscal_client(authenticated=False).pair(code)
        except efis_client.EfisUnavailable as error:
            raise UserError(f"efis nuk u përgjigj. / efis did not answer. ({error})") from error
        body = response.body if isinstance(response.body, dict) else {}
        if response.status != 200 or not body.get("credential") or not body.get("tenantId"):
            raise UserError(
                "Kodi i çiftimit nuk u pranua; kërkoni një kod të ri në efis. "
                "/ The pairing code was not accepted; ask efis for a new one.")
        self.sudo().write({
            "metanow_fiscal_credential": body["credential"],
            "metanow_fiscal_tenant": body["tenantId"],
            "metanow_fiscal_channel": body.get("channel") or False,
        })
        self._metanow_fiscal_refresh_profile()
        # The codes are read too, so they can be chosen at once. Pairing has succeeded by now,
        # so a failed read does not undo it: "Check the configuration" reads them again.
        try:
            self._metanow_fiscal_refresh_registry()
        except UserError as error:
            _logger.warning("efis registry not read after pairing company %s: %s", self.id, error)
        # DEV-2443: invoices that lost their fiscal values in an uninstall get them back from efis.
        # On a company with none, the run finds nothing and only says so.
        self._metanow_fiscal_restore_request()
        return True

    def _metanow_fiscal_restore_request(self):
        """Queue a restore from efis. It runs in the restore job, so a long history never holds
        up the page that asked for it."""
        self.sudo().write({
            "metanow_fiscal_restore_pending": True,
            "metanow_fiscal_restore_summary": "Në radhë... / Queued...",
        })
        cron = self.env.ref("metanow_fiscalization.ir_cron_metanow_fiscal_restore", raise_if_not_found=False)
        if cron:
            cron._trigger()

    def _metanow_fiscal_restore_finished(self, failure=None):
        """Say what the restore found, counted from the invoices themselves, so running it again
        gives the same numbers."""
        self.ensure_one()
        if failure:
            summary = f"Rikthimi nuk u krye. / The restore did not run.\n{failure}"
        else:
            Move = self.env["account.move"].sudo()
            mine = [("company_id", "=", self.id)]
            restored = Move.search_count(mine + [("metanow_fiscal_restored", "=", True)])
            conflicts = Move.search_count(mine + [("metanow_fiscal_restore_note", "=", REASONS[CONFLICT])])
            skipped = Move.search_count(mine + [("metanow_fiscal_restore_note", "!=", False)]) - conflicts
            review = Move.search_count(mine + [("metanow_fiscal_payments_review", "=", True)])
            summary = (f"Rikthyer: {restored} · Lënë mënjanë: {skipped} · Në konflikt: {conflicts} "
                       f"/ Restored: {restored} · Left alone: {skipped} · In conflict: {conflicts}")
            if review:
                summary += (f"\nPagesat e {review} faturave nuk janë dërguar; kontrollojini dhe dërgojini. "
                            f"/ The payments of {review} invoices have not been sent; check them, then send them.")
        self.sudo().write({"metanow_fiscal_restore_pending": False, "metanow_fiscal_restore_summary": summary})

    def _metanow_fiscal_refresh_registry(self):
        """Read the codes efis has confirmed for this business, and keep them on the company."""
        self.ensure_one()
        try:
            response = self._metanow_fiscal_client().get_registry()
        except efis_client.EfisUnavailable as error:
            raise UserError(f"efis nuk u përgjigj. / efis did not answer. ({error})") from error
        if response.status != 200:
            raise UserError(
                f"efis nuk i dha kodet e regjistruara ({response.status}). "
                f"/ efis did not return the registered codes ({response.status}).")
        try:
            registry = efis_registry.Registry(response.body)
        except ValueError as error:
            raise UserError("efis ktheu kode të palexueshme. / efis returned unreadable codes.") from error
        self.write({"metanow_fiscal_registry": registry.to_json()})
        return registry

    def _metanow_fiscal_registry(self):
        self.ensure_one()
        try:
            return efis_registry.Registry.from_json(self.metanow_fiscal_registry)
        except ValueError:
            return None

    @api.model
    def _metanow_fiscal_choices(self, kind):
        """The confirmed codes of one kind, for a pick list. Every company the user is working in
        counts, so a record of any of them shows its code; each record's own company is checked
        when it is saved."""
        codes = set()
        for company in self.env.companies:
            registry = company._metanow_fiscal_registry()
            if registry is not None:
                codes.update(registry.codes[kind])
        return [(code, code) for code in sorted(codes)]

    def _metanow_fiscal_refresh_profile(self):
        self.ensure_one()
        country = self.country_id.code if self.country_id else None
        if not country:
            raise UserError("Kompania nuk ka shtet. / The company has no country.")
        try:
            response = self._metanow_fiscal_client(authenticated=False).get_profile(country)
        except efis_client.EfisUnavailable as error:
            raise UserError(f"efis nuk u përgjigj. / efis did not answer. ({error})") from error
        if response.status != 200:
            raise UserError(
                f"efis nuk ka profil për {country}. / efis has no profile for {country}.")
        try:
            profile = Profile(response.body)
        except ValueError as error:
            raise UserError("efis ktheu një profil të palexueshëm. / efis returned an unreadable profile.") from error
        self.write({"metanow_fiscal_profile": profile.to_json()})
        return profile

    def _metanow_fiscal_profile(self):
        self.ensure_one()
        try:
            return Profile.from_json(self.metanow_fiscal_profile)
        except ValueError:
            return None

    def _metanow_fiscal_config(self):
        """Plain values for the request body builder."""
        self.ensure_one()
        company = self.sudo()
        status = company.metanow_fiscal_vat_status
        return {
            "client_system": company.metanow_fiscal_channel,
            "tax_id": company.vat,
            "issuer_in_vat": {"in_vat": True, "not_in_vat": False}.get(status),
            "seller_name": company.name,
            "seller_address": " ".join(part for part in (company.street, company.street2) if part) or None,
            "seller_town": company.city,
            "seller_country_code": company.country_id.metanow_fiscal_country_code if company.country_id else None,
            "business_unit_code": company.metanow_fiscal_business_unit_code,
            "operator_code": company.metanow_fiscal_operator_code,
            "software_code": company.metanow_fiscal_software_code,
            "device_code": company.metanow_fiscal_device_code,
            "einvoice": bool(company.metanow_fiscal_einvoice),
        }

    def write(self, vals):
        # Turned on without a day: from today, so the business's whole history is not imported.
        if not (vals.get("metanow_fiscal_inbound_enabled") and not vals.get("metanow_fiscal_inbound_since")):
            return super().write(vals)
        today = fields.Date.context_today(self)
        for company in self:
            super(ResCompany, company).write(dict(
                vals, metanow_fiscal_inbound_since=company.metanow_fiscal_inbound_since or today))
        return True

    def _metanow_fiscal_inbound_ignored(self):
        self.ensure_one()
        try:
            return set(json.loads(self.metanow_fiscal_inbound_ignored or "[]"))
        except ValueError:
            return set()

    def _metanow_fiscal_ignore_inbound(self, inbound_id):
        self.ensure_one()
        ignored = self._metanow_fiscal_inbound_ignored() | {inbound_id}
        self.write({"metanow_fiscal_inbound_ignored": json.dumps(sorted(ignored))})
