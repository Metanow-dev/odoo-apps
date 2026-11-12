{
    "name": "AL - Faturë shoqëruese",
    "summary": "Albania: issue warehouse transfer notes and transport invoices for Odoo stock transfers through efis.",
    "description": """
The warehouse transfer note (faturë shoqëruese) for Odoo stock transfers in Albania, through efis,
an external service by Metanow (https://metanow.com). An add-on to "AL - Fiskalizimi me efis".

A transfer whose operation type names a transaction gets an "Issue transfer note" button. efis
numbers the note from the business unit of the warehouse the goods leave from, computes its
security code and registers it with the tax authority; the transfer and the delivery slip show
the number and both codes. Transfers of goods sent to another person for processing,
examination or repair can issue a transport invoice instead, once the company turns them on.
The module does not sign, number or compute a code, and it holds no certificate or key.
""",
    "version": "20.0.1.0.0",
    "category": "Inventory/Inventory",
    "author": "Metanow",
    "website": "https://metanow.com",
    "support": "support@odoo.metanow.com",
    "license": "OPL-1",
    "depends": ["metanow_fiscalization", "stock"],
    "data": [
        "data/ir_cron.xml",
        "views/stock_warehouse_views.xml",
        "views/stock_picking_type_views.xml",
        "views/stock_picking_views.xml",
        "views/res_config_settings_views.xml",
        "report/report_deliveryslip.xml",
    ],
    "images": [],
    "installable": True,
    "application": False,
}
