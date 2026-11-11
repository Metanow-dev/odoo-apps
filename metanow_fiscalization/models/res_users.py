from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..efis import registry as efis_registry


class ResUsers(models.Model):
    _inherit = "res.users"

    # DEV-2436: the operator code an invoice this person posts carries. One per company, as the
    # codes are each business's own; without one, the company's operator code is used.
    metanow_fiscal_operator_code = fields.Selection(
        selection="_metanow_fiscal_operator_choices",
        string="Kodi i operatorit në efis / efis operator code",
        company_dependent=True,
        help="Vetëm kodet që efis ka konfirmuar. Bosh: përdoret kodi i kompanisë. "
             "/ Only codes efis has confirmed. Empty: the company's code is used.",
    )

    @api.model
    def _metanow_fiscal_operator_choices(self):
        return self.env["res.company"]._metanow_fiscal_choices(efis_registry.OPERATOR)

    @api.constrains("metanow_fiscal_operator_code")
    def _check_metanow_fiscal_operator_code(self):
        # The value is the current company's, so it is checked against that company's registry.
        company = self.env.company
        registry = company._metanow_fiscal_registry()
        if registry is None:
            return
        for user in self:
            code = user.metanow_fiscal_operator_code
            if code and not registry.confirms(efis_registry.OPERATOR, code):
                raise ValidationError(
                    f"{efis_registry.LABELS[efis_registry.OPERATOR]}: {code} nuk është konfirmuar në efis "
                    f"për {company.name}. / {code} is not confirmed in efis for {company.name}.")

    def _metanow_fiscal_operator_for(self, company):
        """The operator code this person issues under at the company, or the company's own."""
        self.ensure_one()
        return self.with_company(company).metanow_fiscal_operator_code or company.metanow_fiscal_operator_code
