from odoo import fields, models
from odoo.exceptions import UserError

from ..efis import client as efis_client
from ..efis.profile import Profile

# The company's efis connection. The credential is what pairing returned for this one tenant:
# revocable in efis, readable only by Settings administrators here, and never a certificate
# or a key. The module holds neither.
PAIRING_FIELDS = (
    "metanow_fiscal_credential",
    "metanow_fiscal_tenant",
    "metanow_fiscal_channel",
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
    metanow_fiscal_profile = fields.Text(
        string="Profili i vendit nga efis / efis country profile",
        copy=False,
        readonly=True,
    )

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
        return True

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
        }
