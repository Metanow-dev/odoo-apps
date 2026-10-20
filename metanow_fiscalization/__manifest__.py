{
    "name": "AL - Fiskalizimi me efis",
    "summary": "Albania: fiscalise Odoo customer invoices with the Albanian tax authority (CIS) through efis.",
    "description": """
Albanian fiscalisation (Fiskalizimi) of Odoo customer invoices, through efis, an external service
by Metanow (https://metanow.com).

The module sends each posted customer invoice to efis and stores what efis returns: the fiscal
number, the issuer security code, the registration code and the verification URL.
It does not sign, number or compute a security code, it holds no certificate or key, and it never
talks to the tax authority. It needs an efis account and pairs with efis using a single-use code
from the efis web app. Nothing is sent until an administrator turns fiscalisation on and pairs the
company.
""",
    "version": "20.0.1.0.2",
    "category": "Accounting/Localizations",
    "author": "Metanow",
    "website": "https://metanow.com",
    "support": "support@odoo.metanow.com",
    "license": "OPL-1",
    "depends": ["account"],
    "data": [
        "data/ir_cron.xml",
        "views/account_move_views.xml",
        "views/res_config_settings_views.xml",
        "views/account_journal_views.xml",
        "views/account_tax_views.xml",
        "views/res_partner_views.xml",
        "views/res_country_views.xml",
        "report/report_invoice.xml",
    ],
    "images": [
        "static/description/cover.png",
        "static/description/efis_home_screenshot.png",
        "static/description/efis_documents_screenshot.png",
        "static/description/efis_document_screenshot.png",
        "static/description/efis_pairing_screenshot.png",
    ],
    "installable": True,
    "application": False,
}
