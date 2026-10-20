{
    "name": "Fiskalizimi me efis / Fiscalisation with efis",
    "summary": "Faturat e klientëve fiskalizohen nga efis. / Customer invoices fiscalised by efis.",
    "description": """
Fiskalizimi i faturave të klientëve nëpërmjet efis.
Customer-invoice fiscalisation through efis.

The module sends each posted customer invoice to efis and stores what efis returns: the fiscal
number, the issuer security code, the registration code and the verification URL. It does not
sign, number or compute a security code, it holds no certificate or key, and it never talks to
the tax authority. It pairs with efis using a single-use code from the efis web app.
""",
    "version": "20.0.1.0.0",
    "category": "Accounting/Localizations",
    "author": "Metanow",
    "website": "https://efis.metanow.com",
    "license": "Other proprietary",
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
    "installable": True,
    "application": False,
}
