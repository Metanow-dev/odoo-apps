"""DEV-2429: an Odoo stock transfer as efis's warehouse transfer note request."""

import copy
import importlib.util
import unittest

import support  # registers the main module under its Odoo name


def _load_transport():
    # The request builder alone, without the add-on's models (and so without Odoo).
    spec = importlib.util.spec_from_file_location(
        "metanow_fiscalization_stock_transport", support.STOCK_DIR / "efis" / "transport.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = _load_transport()


def note(**overrides):
    values = {
        "transaction": "TRANSFER",
        "issued_at": "2026-11-11T15:01:50+01:00",
        "value_of_goods": 142.0,
        "vehicle_plates": "AA 123 BB",
        "vehicle_ownership": "OWNER",
        "flammable": False,
        "escort": False,
        "from": {"address": "Rruga e Kavajës 10", "city": "Tiranë", "kind": "WAREHOUSE"},
        "to": {"address": "Lagjja 1", "city": "Durrës", "kind": "STORE"},
        "issuer": {"name": "Shembull sh.p.k.", "address": "Rruga e Durrësit 1", "town": "Tiranë"},
        "carrier": None,
        "goods": [{"name": "[K-1] Karrige", "code": "K-1", "unit": "Units", "quantity": 4.0}],
    }
    values.update(overrides)
    return values


CONFIG = {"business_unit_code": "bb123bb123", "operator_code": "oo123oo123", "software_code": "ss123ss123"}


def build(**overrides):
    config = dict(CONFIG, **overrides.pop("config", {}))
    return transport.build(note(**overrides), config)


class BuildTests(unittest.TestCase):
    def test_a_transfer_between_two_warehouses(self):
        body = build()
        self.assertEqual(body["transaction"], "TRANSFER")
        self.assertEqual(body["businessUnitCode"], "bb123bb123")
        self.assertEqual(body["issuedAt"], "2026-11-11T15:01:50+01:00")
        self.assertEqual(body["valueOfGoods"], "142.00")
        self.assertEqual(body["goods"], [{"name": "[K-1] Karrige", "unit": "Units", "quantity": "4", "code": "K-1"}])
        self.assertEqual(body["from"]["kind"], "WAREHOUSE")
        self.assertIsNone(body["carrier"], "the company carries the goods itself")

    def test_every_key_is_one_efis_publishes(self):
        schemas = support.openapi()["components"]["schemas"]
        body = build()
        self.assertEqual(set(body), set(schemas["TransportDocumentRequest"]["properties"]))
        self.assertLessEqual(set(body["from"]), set(schemas["TransportPlaceInput"]["properties"]))
        self.assertLessEqual(set(body["issuer"]), set(schemas["TransportPartyInput"]["properties"]))
        self.assertLessEqual(set(body["goods"][0]), set(schemas["TransportGoodsInput"]["properties"]))

    def test_money_carries_exactly_two_decimals(self):
        self.assertEqual(build(value_of_goods=7)["valueOfGoods"], "7.00")
        with self.assertRaises(transport.TransportInvalid):
            build(value_of_goods=1.005)

    def test_the_issue_time_has_an_offset_and_whole_seconds(self):
        for bad in ("2026-11-11T15:01:50", "2026-11-11T15:01:50.123+01:00", "2026-11-11T15:01:50Z", None):
            with self.assertRaises(transport.TransportInvalid, msg=bad):
                build(issued_at=bad)

    def test_no_transaction_is_chosen_for_the_business(self):
        for transaction in (None, False, "", "SALE"):
            with self.assertRaises(transport.TransportInvalid) as raised:
                build(transaction=transaction)
            self.assertIn("choose it on the operation type", str(raised.exception))

    def test_each_code_is_required(self):
        for key in CONFIG:
            with self.assertRaises(transport.TransportInvalid, msg=key):
                build(config={key: None})

    def test_the_vehicle_is_required(self):
        with self.assertRaises(transport.TransportInvalid) as raised:
            build(vehicle_plates=" ", vehicle_ownership=None)
        self.assertEqual(len(raised.exception.problems), 2)

    def test_places_take_only_the_kinds_efis_publishes(self):
        body = build(**{"from": {"address": "Dogana", "city": "Durrës", "kind": "CUSTOMS"}})
        self.assertEqual(body["from"]["kind"], "CUSTOMS")
        with self.assertRaises(transport.TransportInvalid, msg="customs is an origin only"):
            build(to={"address": "Dogana", "city": "Durrës", "kind": "CUSTOMS"})
        with self.assertRaises(transport.TransportInvalid):
            build(to={"address": "Lagjja 1", "city": "Durrës", "kind": None})
        with self.assertRaises(transport.TransportInvalid):
            build(to={"address": None, "city": "Durrës", "kind": "STORE"})

    def test_a_carrier_other_than_the_company_is_issued_from_efis_for_now(self):
        with self.assertRaises(transport.TransportInvalid) as raised:
            build(carrier={"name": "Transport sh.p.k."})
        self.assertIn("issue the transfer note from efis", str(raised.exception))

    def test_goods_need_a_name_a_unit_and_a_positive_quantity(self):
        with self.assertRaises(transport.TransportInvalid):
            build(goods=[])
        for item in ({"name": "", "unit": "Units", "quantity": 1}, {"name": "X", "unit": None, "quantity": 1},
                     {"name": "X", "unit": "Units", "quantity": 0}, {"name": "X", "unit": "Units", "quantity": -2}):
            with self.assertRaises(transport.TransportInvalid, msg=repr(item)):
                build(goods=[item])

    def test_the_issuer_needs_a_name_address_and_town(self):
        with self.assertRaises(transport.TransportInvalid):
            build(issuer={"name": "Shembull sh.p.k.", "address": None, "town": "Tiranë"})

    def test_the_input_is_not_changed(self):
        values = note()
        kept = copy.deepcopy(values)
        transport.build(values, CONFIG)
        self.assertEqual(values, kept)

    def test_the_codes_are_efis_published_ones(self):
        # efis documents these in TransportDocumentRequest and TransportPlaceInput.
        self.assertEqual(transport.TRANSACTIONS, ("TRANSFER", "DOOR", "SALES", "EXAMINATION"))
        self.assertEqual(transport.DESTINATION_KINDS, ("WAREHOUSE", "EXHIBITION", "STORE", "SALE", "OTHER"))
        self.assertEqual(transport.ORIGIN_KINDS[-2:], ("ANOTHER", "CUSTOMS"))
        self.assertEqual(transport.VEHICLE_OWNERSHIPS, ("OWNER", "THIRDPARTY"))


class AnswerTests(unittest.TestCase):
    def test_efis_answer_is_stored_as_the_transfer_shows_it(self):
        answer = {"id": "n-1", "number": "1/2026", "businessUnitCode": "bb123bb123", "issuedAt": "2026-11-11T15:01:50+01:00",
                  "securityCode": "a" * 32, "state": "queued", "registrationCode": None}
        values = transport.answer_values(answer)
        self.assertEqual(values["metanow_fiscal_wtn_state"], "sent")
        self.assertEqual(values["metanow_fiscal_wtn_number"], "1/2026")
        self.assertFalse(values["metanow_fiscal_wtn_registration_code"])
        self.assertEqual(transport.answer_values(dict(answer, state="accepted", registrationCode="fic"))["metanow_fiscal_wtn_state"], "registered")
        self.assertEqual(transport.answer_values(dict(answer, state="rejected"))["metanow_fiscal_wtn_state"], "rejected")

    def test_the_answer_is_efis_published_shape(self):
        properties = set(support.openapi()["components"]["schemas"]["TransportDocumentResponse"]["properties"])
        self.assertLessEqual({"id", "number", "securityCode", "state", "registrationCode"}, properties)


if __name__ == "__main__":
    unittest.main()
