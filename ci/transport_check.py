# DEV-2348: run inside `odoo shell` after metanow_fiscalization_stock is installed and the
# company is paired (ci/post_invoice.py ran first). Walks a stock transfer through efis as a
# customer would: a warehouse transfer note for goods leaving a warehouse (DEV-2429), followed
# until the authority registers it, printed on the delivery slip, and frozen afterwards. Then the
# same transfer as a transport invoice (DEV-2430), which this deployment keeps switched off.
#
# The company carries its own goods here. A transfer that names a carrier (DEV-2444) needs an
# efis that publishes carrier.idType in its country profile, which the pinned one does not, so
# that case moves with contract/ (DEV-2348 finding, 19 Nov 2026).
#
# Prints ODOO-TRANSPORT-PASSED on success; odoo-install.sh fails the job without that line.
import os
import time

from odoo.exceptions import UserError, ValidationError

ISSUED = ("to_send", "sent", "deferred", "registered", "rejected")


def refused(what):
    """A block that must raise UserError, inside a savepoint so nothing it touched is kept."""
    class Refused:
        def __enter__(self):
            self.savepoint = env.cr.savepoint()  # noqa: F821
            self.savepoint.__enter__()
            return self

        def __exit__(self, kind, error, traceback):
            if kind is None:
                self.savepoint.__exit__(None, None, None)
                raise AssertionError(what)
            if issubclass(kind, UserError):
                self.savepoint.__exit__(kind, error, traceback)
                return True
            return False
    return Refused()


company = env.company  # noqa: F821 - provided by odoo shell
assert company.metanow_fiscal_enabled, "the company is not fiscalising; post_invoice.py runs first"
assert company.metanow_fiscal_credential, "the company is not paired with efis"

warehouse = env["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)  # noqa: F821
assert warehouse, "the company has no warehouse"
# DEV-2429: the note is numbered from the business unit the goods leave from, so the warehouse
# names its own code, out of the ones efis confirmed, and says what kind of place it is.
warehouse.write({
    "metanow_fiscal_business_unit_code": os.environ["EFIS_BUSINESS_UNIT_CODE"],
    "metanow_fiscal_place_kind": "WAREHOUSE",
})
assert warehouse.metanow_fiscal_business_unit_code, "the warehouse's business unit code was refused"

delivery = warehouse.out_type_id
delivery.metanow_fiscal_wtn_transaction = "TRANSFER"
# An operation issues one document or the other, never both (DEV-2430). The savepoint is what
# keeps the refused half out of the transaction the rest of this check commits.
try:
    with env.cr.savepoint():  # noqa: F821
        delivery.metanow_fiscal_ti_transaction = "REPAIR"
except ValidationError:
    delivery.invalidate_recordset()
else:
    raise AssertionError("one operation type named both a transfer note and a transport invoice")
assert not delivery.metanow_fiscal_ti_transaction, delivery.metanow_fiscal_ti_transaction

customer = env["res.partner"].create({  # noqa: F821
    "name": "Dyqani i Durrësit", "street": "Lagjja 1", "city": "Durrës",
    "vat": "L91234567K", "country_id": env.ref("base.al").id,  # noqa: F821
})
product = env["product.product"].create({  # noqa: F821
    "name": "Tavolinë", "default_code": "T-1", "is_storable": True, "standard_price": 71.0,
})


def transfer(picking_type, partner, destination_kind):
    picking = env["stock.picking"].create({  # noqa: F821
        "picking_type_id": picking_type.id,
        "partner_id": partner.id,
        "location_id": picking_type.default_location_src_id.id,
        "location_dest_id": picking_type.default_location_dest_id.id,
        "move_ids": [(0, 0, {
            "product_id": product.id,
            "product_uom_qty": 2,
            "location_id": picking_type.default_location_src_id.id,
            "location_dest_id": picking_type.default_location_dest_id.id,
        })],
    })
    picking.write({
        "metanow_fiscal_vehicle_plates": "AA-123-BB",
        "metanow_fiscal_vehicle_ownership": "OWNER",
        "metanow_fiscal_destination_kind": destination_kind,
    })
    return picking


# ------------------------------------------------------------------ the transfer note

picking = transfer(delivery, customer, "STORE")
assert picking.metanow_fiscal_wtn_applies, "the transfer issues no transport document"
assert picking.metanow_fiscal_transport_document == "wtn", picking.metanow_fiscal_transport_document

# DEV-2640: plates written the way they are written here, with spaces, are not the authority's
# VehPlatesSType. The person is told before anything is queued, and nothing is sent.
with refused("plates with spaces were queued; efis refuses them as a schema violation"):
    picking.metanow_fiscal_vehicle_plates = "AA 123 BB"
    picking.action_metanow_fiscal_issue_wtn()
picking.invalidate_recordset()
assert not picking.metanow_fiscal_wtn_state, picking.metanow_fiscal_wtn_state
assert picking.metanow_fiscal_vehicle_plates == "AA-123-BB", picking.metanow_fiscal_vehicle_plates

picking.action_metanow_fiscal_issue_wtn()
env.cr.commit()  # noqa: F821
assert picking.metanow_fiscal_wtn_state == "to_send", picking.metanow_fiscal_wtn_state
assert picking.metanow_fiscal_transport_kind == "wtn", picking.metanow_fiscal_transport_kind
# The goods are valued at cost, the working guess of DEV-2441: two tables at 71.00.
assert '"valueOfGoods": "142.00"' in picking.metanow_fiscal_wtn_request, picking.metanow_fiscal_wtn_request
# The company carrying its own goods is no carrier at all: the note's issuer is the carrier
# (Law 87/2019 Art. 40(1)(h)).
assert '"carrier": null' in picking.metanow_fiscal_wtn_request, picking.metanow_fiscal_wtn_request

picking._metanow_fiscal_wtn_send()
env.cr.commit()  # noqa: F821
assert picking.metanow_fiscal_wtn_number, picking.metanow_fiscal_wtn_error
assert picking.metanow_fiscal_wtn_security_code, picking.metanow_fiscal_wtn_error
number, code = picking.metanow_fiscal_wtn_number, picking.metanow_fiscal_wtn_security_code

deadline = time.time() + 180
while time.time() < deadline and not picking.metanow_fiscal_wtn_registration_code:
    env["stock.picking"]._cron_metanow_fiscal_wtn_poll()  # noqa: F821
    env.cr.commit()  # noqa: F821
    picking.invalidate_recordset()
    if picking.metanow_fiscal_wtn_state == "rejected":
        break
    time.sleep(3)
assert picking.metanow_fiscal_wtn_state == "registered", (
    picking.metanow_fiscal_wtn_state, picking.metanow_fiscal_wtn_error)
assert picking.metanow_fiscal_wtn_registration_code
# efis numbered it once and nothing renumbered it while it was followed.
assert picking.metanow_fiscal_wtn_number == number, (picking.metanow_fiscal_wtn_number, number)
assert picking.metanow_fiscal_wtn_security_code == code

# An issued note is never changed, and nothing of efis's is overwritten.
for vals, what in (
    ({"metanow_fiscal_wtn_number": "9/2026"}, "the number"),
    ({"metanow_fiscal_wtn_security_code": "0" * 32}, "the security code"),
    ({"metanow_fiscal_vehicle_plates": "BB-456-CC"}, "the vehicle plates"),
    ({"metanow_fiscal_vehicle_ownership": "THIRDPARTY"}, "whom the vehicle belongs to"),
):
    with refused(f"{what} changed after the note was issued"):
        picking.write(vals)
    picking.invalidate_recordset()

# It is never issued a second time.
with refused("the transfer note was issued twice"):
    picking.action_metanow_fiscal_issue_wtn()
picking.invalidate_recordset()

slip, _ = env["ir.actions.report"]._render_qweb_pdf("stock.action_report_delivery", picking.ids)  # noqa: F821
assert slip, "the delivery slip did not render"
html = env["ir.qweb"]._render(  # noqa: F821
    "stock.report_delivery_document", {"docs": picking, "o": picking, "doc_model": "stock.picking"})
text = str(html)
assert number in text, "the delivery slip does not print the note's number"
assert code in text, "the delivery slip does not print the issuer security code"
assert picking.metanow_fiscal_wtn_registration_code in text, "the delivery slip does not print the registration code"
assert "NSLFSH" in text and "TIIC" not in text, "the delivery slip names the transport invoice's codes"

# ------------------------------------------------------------------ the transport invoice

repairs = env["stock.picking.type"].create({  # noqa: F821
    "name": "Dërgim për riparim / Send for repair",
    "sequence_code": "RIP",
    "code": "outgoing",
    "company_id": company.id,
    "warehouse_id": warehouse.id,
    "default_location_src_id": delivery.default_location_src_id.id,
    "default_location_dest_id": delivery.default_location_dest_id.id,
    "metanow_fiscal_ti_transaction": "REPAIR",
})
company.metanow_fiscal_ti_enabled = False
env.cr.commit()  # noqa: F821
repair = transfer(repairs, customer, "OTHER")
assert not repair.metanow_fiscal_wtn_applies, "a transport invoice was offered while the company has them off"
with refused("a transport invoice was issued while the company has them off"):
    repair.action_metanow_fiscal_issue_wtn()

company.metanow_fiscal_ti_enabled = True
repair = transfer(repairs, customer, "OTHER")
repair.invalidate_recordset()
assert repair.metanow_fiscal_transport_document == "ti", repair.metanow_fiscal_transport_document
repair.action_metanow_fiscal_issue_wtn()
env.cr.commit()  # noqa: F821
assert repair.metanow_fiscal_transport_kind == "ti", repair.metanow_fiscal_transport_kind
# The receiver is the transfer's partner: a transport invoice names whom the goods go to.
assert '"receiver"' in repair.metanow_fiscal_wtn_request, repair.metanow_fiscal_wtn_request
repair._metanow_fiscal_wtn_send()
env.cr.commit()  # noqa: F821
# This deployment does not issue transport invoices (Efis:TransportInvoices:Enabled is off), so
# the transfer waits for a person with efis's reason, and no number was spent.
assert repair.metanow_fiscal_wtn_state == "parked", (repair.metanow_fiscal_wtn_state, repair.metanow_fiscal_wtn_error)
assert not repair.metanow_fiscal_wtn_number, repair.metanow_fiscal_wtn_number
assert "fatura transporti" in (repair.metanow_fiscal_wtn_error or ""), repair.metanow_fiscal_wtn_error

print("ODOO-TRANSPORT-PASSED", number, picking.metanow_fiscal_wtn_registration_code)
