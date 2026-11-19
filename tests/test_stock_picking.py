"""The stock add-on's model layer against a mocked ORM boundary and a mocked HTTP client.

What a transfer does with efis's answer to a transport document: the number and codes stored from
an accepted one, and what the person is told when efis refuses (DEV-2641) or when the deployment
does not issue transport invoices at all (DEV-2430).
"""

import json
import unittest
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis.client import EfisClient
from metanow_fiscalization.models.res_company import ResCompany
from metanow_fiscalization.models.res_users import ResUsers
from metanow_fiscalization_stock.efis import transport
from metanow_fiscalization_stock.models.stock_picking import StockPicking
from metanow_fiscalization_stock.models.stock_picking_type import StockPickingType

REFUSED_PLATES = "The vehicle's plates are letters, digits and hyphens."


def company(env, opener=None, **overrides):
    values = dict(
        name="Shembull sh.p.k.",
        vat="K12345678L",
        street="Rruga e Durrësit 1",
        street2=False,
        city="Tiranë",
        country_id=SimpleNamespace(code="AL", metanow_fiscal_country_code=False),
        currency_id=SimpleNamespace(name="ALL", round=lambda value: round(value, 2)),
        metanow_fiscal_enabled=True,
        metanow_fiscal_api_url="https://efis.example.invalid",
        metanow_fiscal_credential="c-1",
        metanow_fiscal_tenant="shembull",
        metanow_fiscal_channel="odoo",
        metanow_fiscal_software_code="ss123ss123",
        metanow_fiscal_ti_enabled=False,
        metanow_fiscal_profile=support.profile().to_json(),
        partner_id=SimpleNamespace(commercial_partner_id=SimpleNamespace(id=1)),
    )
    values.update(overrides)
    record = ResCompany(env, **values)
    if opener is not None:
        record._metanow_fiscal_client = lambda authenticated=True: EfisClient(
            record.metanow_fiscal_api_url,
            credential=record.metanow_fiscal_credential,
            tenant=record.metanow_fiscal_tenant,
            opener=opener,
        )
    return record


def place(**overrides):
    values = dict(street="Rruga e Kavajës 10", street2=False, city="Tiranë")
    values.update(overrides)
    return SimpleNamespace(**values)


def warehouse(**overrides):
    values = dict(
        metanow_fiscal_business_unit_code="bb123bb123",
        metanow_fiscal_place_kind="WAREHOUSE",
        partner_id=place(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def picking(env, owner, document=transport.TRANSFER_NOTE, transaction="TRANSFER", **overrides):
    source = warehouse()
    # The operation type decides which document the transfer issues, and the company's switch
    # decides whether a transport invoice is issued at all: both are the add-on's own code.
    operation = StockPickingType(
        env, warehouse_id=source,
        metanow_fiscal_wtn_transaction=transaction if document == transport.TRANSFER_NOTE else False,
        metanow_fiscal_ti_transaction=transaction if document == transport.TRANSPORT_INVOICE else False)
    unit = SimpleNamespace(name="Units", _compute_quantity=lambda quantity, uom: quantity)
    product = SimpleNamespace(
        display_name="[T-1] Tavolinë", default_code="T-1", standard_price=71.0,
        uom_id=SimpleNamespace(name="Units"))
    buyer = SimpleNamespace(
        name="Dyqani i Durrësit", vat="L91234567K", street="Lagjja 1", street2=False, city="Durrës")
    # Odoo's contact, and the business behind it: the destination reads the contact's address,
    # the receiver of a transport invoice the business.
    buyer.commercial_partner_id = buyer
    values = dict(
        id=7,
        name="WH/OUT/00001",
        company_id=owner,
        picking_type_id=operation,
        location_id=SimpleNamespace(warehouse_id=source),
        location_dest_id=SimpleNamespace(warehouse_id=None),
        partner_id=buyer,
        move_ids=[SimpleNamespace(state="assigned", product_id=product, uom_id=unit, quantity=2.0,
                                  product_uom_qty=2.0)],
        metanow_fiscal_vehicle_plates="AA123BB",
        metanow_fiscal_vehicle_ownership="OWNER",
        metanow_fiscal_goods_flammable=False,
        metanow_fiscal_escort_required=False,
        metanow_fiscal_destination_kind="STORE",
        metanow_fiscal_carrier_id=SimpleNamespace(commercial_partner_id=None),
    )
    values.update(overrides)
    record = StockPicking(env, **values)
    record._compute_metanow_fiscal_wtn_applies()
    return record


def environment():
    env = fake_odoo.Env()
    env.refs["metanow_fiscalization_stock.ir_cron_metanow_fiscal_wtn_send"] = fake_odoo.Trigger()
    env.refs["metanow_fiscalization_stock.ir_cron_metanow_fiscal_wtn_poll"] = fake_odoo.Trigger()
    env.user = ResUsers(env, id=2, metanow_fiscal_operator_code="oo123oo123")
    return env


def queued(*answers, document=transport.TRANSFER_NOTE, transaction="TRANSFER", **overrides):
    """A transfer with its document issued and waiting to be sent, and the answers efis will give."""
    env = environment()
    opener = support.FakeOpener(*answers)
    owner = company(env, opener=opener, metanow_fiscal_ti_enabled=document == transport.TRANSPORT_INVOICE)
    record = picking(env, owner, document=document, transaction=transaction, **overrides)
    record.action_metanow_fiscal_issue_wtn()
    record.writes.clear()
    return record, opener


ACCEPTED = {
    "id": "d-1", "number": "1/2026", "securityCode": "a" * 32,
    "registrationCode": None, "state": "queued",
}


class IssuingTests(unittest.TestCase):
    def test_the_request_is_kept_as_sent_with_the_goods_at_cost(self):
        record, _ = queued((200, dict(ACCEPTED)))
        body = json.loads(record.metanow_fiscal_wtn_request)
        self.assertEqual(body["valueOfGoods"], "142.00")
        self.assertEqual(body["businessUnitCode"], "bb123bb123")
        self.assertEqual(body["transaction"], "TRANSFER")
        self.assertEqual(record.metanow_fiscal_wtn_state, "to_send")
        self.assertEqual(record.metanow_fiscal_transport_kind, transport.TRANSFER_NOTE)

    def test_the_answer_is_stored_and_the_transfer_is_followed(self):
        record, opener = queued((200, dict(ACCEPTED)))
        record._metanow_fiscal_wtn_send()
        self.assertEqual(opener.requests[0]["url"], "https://efis.example.invalid/v1/transport-documents")
        self.assertEqual(record.metanow_fiscal_wtn_number, "1/2026")
        self.assertEqual(record.metanow_fiscal_wtn_security_code, "a" * 32)
        self.assertEqual(record.metanow_fiscal_wtn_state, "sent")
        self.assertEqual(len(record.writes), 1, "efis's answer is one write")
        self.assertEqual(
            record.env.refs["metanow_fiscalization_stock.ir_cron_metanow_fiscal_wtn_poll"].count, 1)

    def test_plates_the_authority_would_refuse_are_refused_before_anything_is_sent(self):
        # DEV-2640: "AA 123 BB", the way plates are written here, is not the authority's
        # VehPlatesSType. Nothing is queued and nothing is sent.
        env = environment()
        opener = support.FakeOpener()
        record = picking(env, company(env, opener=opener), metanow_fiscal_vehicle_plates="AA 123 BB")
        with self.assertRaises(fake_odoo.UserError) as raised:
            record.action_metanow_fiscal_issue_wtn()
        self.assertIn("no spaces", str(raised.exception))
        self.assertEqual(opener.requests, [])
        self.assertFalse(record.metanow_fiscal_wtn_state)


class RefusalTests(unittest.TestCase):
    def test_a_refusal_shows_efis_sentence_then_its_code(self):
        # DEV-2641: what a refused invoice shows (DEV-2223). The person has to know which field
        # to correct before issuing the document again.
        record, opener = queued((422, {
            "code": "schema_violation", "detail": REFUSED_PLATES, "retryable": False,
            "authorityCode": None, "correlationId": "c0ffee"}))
        record._metanow_fiscal_wtn_send()
        self.assertEqual(record.metanow_fiscal_wtn_state, "refused")
        self.assertIn(REFUSED_PLATES, record.metanow_fiscal_wtn_error, "efis's message is shown in full")
        self.assertIn("schema_violation", record.metanow_fiscal_wtn_error, "the code is shown for support")
        self.assertIn("c0ffee", record.metanow_fiscal_wtn_error, "and the correlation id")
        self.assertFalse(record.metanow_fiscal_wtn_number, "a refusal spends no number")
        record._metanow_fiscal_wtn_send()
        self.assertEqual(len(opener.requests), 1, "a refusal is a client fault: never resent")

    def test_a_document_not_issued_today_waits_for_a_person_with_efis_sentence(self):
        record, _ = queued((422, {
            "code": "issued_at_not_today", "detail": "A transport document is issued today.",
            "retryable": False, "authorityCode": None}))
        record._metanow_fiscal_wtn_send()
        self.assertEqual(record.metanow_fiscal_wtn_state, "parked")
        self.assertIn("A transport document is issued today.", record.metanow_fiscal_wtn_error)
        self.assertIn("issued_at_not_today", record.metanow_fiscal_wtn_error)

    def test_efis_trouble_keeps_the_transfer_queued_with_what_efis_said(self):
        record, opener = queued(
            (503, {"code": "upstream_unavailable", "detail": "The authority did not answer.",
                   "retryable": True, "authorityCode": None}),
            (200, dict(ACCEPTED)))
        record._metanow_fiscal_wtn_send()
        self.assertEqual(record.metanow_fiscal_wtn_state, "to_send", "efis's trouble is not the caller's fault")
        self.assertIn("The authority did not answer.", record.metanow_fiscal_wtn_error)
        record._metanow_fiscal_wtn_send()
        self.assertEqual(record.metanow_fiscal_wtn_number, "1/2026")
        self.assertEqual(opener.requests[0]["body"], opener.requests[1]["body"], "the same body")
        self.assertEqual(opener.requests[0]["headers"]["idempotency-key"],
                         opener.requests[1]["headers"]["idempotency-key"], "under the same key")

    def test_a_deployment_that_does_not_issue_transport_invoices_says_so(self):
        # DEV-2430: efis keeps transport invoices off until the authority's test host confirms
        # their security code and numbering. No number is spent and resending changes nothing.
        record, opener = queued(
            (503, {"code": "transport_invoices_disabled", "retryable": False, "authorityCode": None}),
            document=transport.TRANSPORT_INVOICE, transaction="REPAIR")
        self.assertEqual(opener.requests, [])
        record._metanow_fiscal_wtn_send()
        self.assertEqual(opener.requests[0]["url"], "https://efis.example.invalid/v1/transport-invoices")
        self.assertEqual(record.metanow_fiscal_wtn_state, "parked")
        self.assertIn("fatura transporti", record.metanow_fiscal_wtn_error)
        self.assertFalse(record.metanow_fiscal_wtn_number)
        record._metanow_fiscal_wtn_send()
        self.assertEqual(len(opener.requests), 1, "nothing is sent again")


class TransportInvoiceTests(unittest.TestCase):
    def test_a_transport_invoice_names_the_receiver_and_goes_to_its_own_route(self):
        record, opener = queued((200, dict(ACCEPTED)),
                                document=transport.TRANSPORT_INVOICE, transaction="REPAIR")
        body = json.loads(record.metanow_fiscal_wtn_request)
        self.assertEqual(body["transaction"], "REPAIR")
        self.assertEqual(body["receiver"]["taxId"], "L91234567K")
        self.assertEqual(body["receiver"]["town"], "Durrës")
        record._metanow_fiscal_wtn_send()
        self.assertEqual(opener.requests[0]["url"], "https://efis.example.invalid/v1/transport-invoices")
        self.assertEqual(record.metanow_fiscal_transport_kind, transport.TRANSPORT_INVOICE)


if __name__ == "__main__":
    unittest.main()
