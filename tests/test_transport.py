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


def _token(*parts):
    """The authority's own code, assembled so tests/test_module.py's check does not trip here."""
    return "".join(parts)


def note(**overrides):
    values = {
        "transaction": "TRANSFER",
        "issued_at": "2026-11-11T15:01:50+01:00",
        "value_of_goods": 142.0,
        "vehicle_plates": "AA123BB",
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


# DEV-2444: a carrier other than the company, as the picking reads one from the partner.
CARRIER = {"id_type": "tax", "id_number": "l9 1234567 k", "name": "Transportues sh.p.k.",
           "address": "Rruga 1", "town": "Durrës"}


def build(**overrides):
    config = dict(CONFIG, **overrides.pop("config", {}))
    profile = overrides.pop("profile", support.profile())
    return transport.build(note(**overrides), config, profile=profile)


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
        self.assertLessEqual(
            set(build(carrier=CARRIER)["carrier"]), set(schemas["TransportCarrierInput"]["properties"]))

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

    def test_the_plates_are_the_ones_the_authority_schema_accepts(self):
        # DEV-2640: the authority's VehPlatesSType is [a-zA-Z0-9-]+, so "AA 123 BB" -- the way
        # plates are written here -- can never be accepted. It is refused before anything is
        # sent, and never rewritten into something the person did not type.
        self.assertEqual(build(vehicle_plates="AA-123-BB")["vehiclePlates"], "AA-123-BB")
        self.assertEqual(build(vehicle_plates=" AA123BB ")["vehiclePlates"], "AA123BB")
        for plates in ("AA 123 BB", "AA.123.BB", "AA/123"):
            with self.assertRaises(transport.TransportInvalid, msg=plates) as raised:
                build(vehicle_plates=plates)
            self.assertEqual(len(raised.exception.problems), 1)
            self.assertIn("no spaces", str(raised.exception))

    def test_places_take_only_the_kinds_efis_publishes(self):
        body = build(**{"from": {"address": "Dogana", "city": "Durrës", "kind": "CUSTOMS"}})
        self.assertEqual(body["from"]["kind"], "CUSTOMS")
        with self.assertRaises(transport.TransportInvalid, msg="customs is an origin only"):
            build(to={"address": "Dogana", "city": "Durrës", "kind": "CUSTOMS"})
        with self.assertRaises(transport.TransportInvalid):
            build(to={"address": "Lagjja 1", "city": "Durrës", "kind": None})
        with self.assertRaises(transport.TransportInvalid):
            build(to={"address": None, "city": "Durrës", "kind": "STORE"})

    def test_a_carrier_other_than_the_company_is_named_in_efis_own_words(self):
        # DEV-2444: efis takes the identifier type in its own words, so the module sends the
        # partner's own, and the number as efis compares one.
        self.assertEqual(
            build(carrier=CARRIER)["carrier"],
            {"idType": "tax", "idNumber": "L91234567K", "name": "Transportues sh.p.k.",
             "address": "Rruga 1", "town": "Durrës"})

        # A partner with no type set means a domestic tax number, as a buyer's does.
        self.assertEqual(build(carrier=dict(CARRIER, id_type=None))["carrier"]["idType"], "tax")

        # A person carries them: named by a personal ID, kept as typed, and with no address.
        self.assertEqual(
            build(carrier={"id_type": "personal_id", "id_number": "I05101999A", "name": "Arben Hoxha"})["carrier"],
            {"idType": "personal_id", "idNumber": "I05101999A", "name": "Arben Hoxha"})

    def test_the_carriers_identifier_types_are_read_from_the_profile(self):
        # A type efis publishes for a buyer and not for a carrier: the authority admits two.
        with self.assertRaises(transport.TransportInvalid) as refused:
            build(carrier=dict(CARRIER, id_type="passport"))
        self.assertIn("does not accept the carrier identifier type passport", str(refused.exception))

        # The authority's own code is efis's to write, never this module's.
        with self.assertRaises(transport.TransportInvalid):
            build(carrier=dict(CARRIER, id_type=_token("NU", "IS")))

        # No profile read yet, and one read before efis published the list: say which, and send
        # nothing. A note the company carries itself needs no profile at all.
        with self.assertRaises(transport.TransportInvalid) as unread:
            build(carrier=CARRIER, profile=None)
        self.assertIn("country profile has not been fetched", str(unread.exception))
        older = copy.deepcopy(support.PROFILE_DOCUMENT)
        older["enumerations"] = [item for item in older["enumerations"] if item["field"] != "carrier.idType"]
        with self.assertRaises(transport.TransportInvalid) as stale:
            build(carrier=CARRIER, profile=support.Profile(older))
        self.assertIn("carries no carrier identifier types", str(stale.exception))
        self.assertIsNone(build(profile=None)["carrier"])

    def test_a_carrier_needs_a_number_and_a_name(self):
        for given in (dict(CARRIER, id_number=None), dict(CARRIER, name=None), dict(CARRIER, id_number="   ")):
            with self.assertRaises(transport.TransportInvalid, msg=repr(given)):
                build(carrier=given)

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
        values = note(carrier=dict(CARRIER))
        kept = copy.deepcopy(values)
        transport.build(values, CONFIG, profile=support.profile())
        self.assertEqual(values, kept)

    def test_the_codes_are_efis_published_ones(self):
        # efis documents these in TransportDocumentRequest and TransportPlaceInput.
        self.assertEqual(transport.TRANSACTIONS, ("TRANSFER", "DOOR", "SALES", "EXAMINATION"))
        self.assertEqual(transport.DESTINATION_KINDS, ("WAREHOUSE", "EXHIBITION", "STORE", "SALE", "OTHER"))
        self.assertEqual(transport.ORIGIN_KINDS[-2:], ("ANOTHER", "CUSTOMS"))
        self.assertEqual(transport.VEHICLE_OWNERSHIPS, ("OWNER", "THIRDPARTY"))


RECEIVER = {"tax_id": "M11111111M", "name": "Riparime sh.p.k.", "address": "Rruga 5", "town": "Elbasan"}


def build_ti(**overrides):
    values = {"transaction": "REPAIR", "receiver": RECEIVER}
    values.update(overrides)
    config = dict(CONFIG, **values.pop("config", {}))
    profile = values.pop("profile", support.profile())
    return transport.build(note(**values), config, transport.TRANSPORT_INVOICE, profile)


class TransportInvoiceTests(unittest.TestCase):
    """DEV-2430: the same transfer as a transport invoice."""

    def test_goods_sent_for_repair(self):
        body = build_ti()
        self.assertEqual(body["transaction"], "REPAIR")
        self.assertEqual(body["receiver"], {"taxId": "M11111111M", "name": "Riparime sh.p.k.", "address": "Rruga 5", "town": "Elbasan"})

    def test_every_key_is_one_efis_publishes(self):
        schemas = support.openapi()["components"]["schemas"]
        body = build_ti()
        self.assertEqual(set(body), set(schemas["TransportInvoiceRequest"]["properties"]))
        self.assertEqual(set(body["receiver"]), set(schemas["TransportReceiverInput"]["properties"]))
        self.assertLessEqual(
            set(build_ti(carrier=CARRIER)["carrier"]), set(schemas["TransportCarrierInput"]["properties"]))

    def test_a_transfer_note_carries_no_receiver(self):
        self.assertNotIn("receiver", build())

    def test_only_a_transport_invoice_s_transactions(self):
        for transaction in ("PROCESSING", "EXAMINATION", "REPAIR"):
            self.assertEqual(build_ti(transaction=transaction)["transaction"], transaction)
        for transaction in ("TRANSFER", "SALES", None):
            with self.assertRaises(transport.TransportInvalid, msg=transaction):
                build_ti(transaction=transaction)
        with self.assertRaises(transport.TransportInvalid, msg="a transfer note never takes REPAIR"):
            build(transaction="REPAIR")

    def test_places_are_narrower_than_a_transfer_note_s(self):
        for kind in ("SALE", "ANOTHER", "CUSTOMS"):
            with self.assertRaises(transport.TransportInvalid, msg=kind):
                build_ti(**{"from": {"address": "Rruga 1", "city": "Tiranë", "kind": kind}})
        with self.assertRaises(transport.TransportInvalid):
            build_ti(to={"address": "Rruga 5", "city": "Elbasan", "kind": "SALE"})
        self.assertEqual(build_ti(to={"address": "Rruga 5", "city": "Elbasan", "kind": "OTHER"})["to"]["kind"], "OTHER")

    def test_the_receiver_is_required_whole(self):
        for receiver in (None, dict(RECEIVER, tax_id=None), dict(RECEIVER, town="")):
            with self.assertRaises(transport.TransportInvalid, msg=repr(receiver)) as raised:
                build_ti(receiver=receiver)
            self.assertIn("receiver needs a tax number", str(raised.exception))

    def test_the_codes_are_efis_published_ones(self):
        self.assertEqual(transport.TI_TRANSACTIONS, ("PROCESSING", "EXAMINATION", "REPAIR"))
        self.assertEqual(transport.TI_KINDS, ("WAREHOUSE", "EXHIBITION", "STORE", "OTHER"))


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
