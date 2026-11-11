{
    "name": "AL - Arka POS me efis",
    "summary": "Albania: declare Point of Sale cash (opening float, cash in and out) to the tax authority through efis.",
    "description": """
The cash register (arka) for Odoo Point of Sale in Albania, through efis, an external service by
Metanow (https://metanow.com). An add-on to "AL - Fiskalizimi me efis".

A point of sale that names its efis device declares the day's opening float when a session is
opened, and each cash in or out, to efis, which registers them with the tax authority. The module
does not fiscalise Point of Sale orders, does not sign or number anything, and holds no
certificate or key.
""",
    "version": "20.0.1.0.0",
    "category": "Sales/Point of Sale",
    "author": "Metanow",
    "website": "https://metanow.com",
    "support": "support@odoo.metanow.com",
    "license": "OPL-1",
    "depends": ["metanow_fiscalization", "point_of_sale"],
    "data": [
        "data/ir_cron.xml",
        "views/pos_config_views.xml",
        "views/pos_session_views.xml",
    ],
    "images": [],
    "installable": True,
    "application": False,
}
