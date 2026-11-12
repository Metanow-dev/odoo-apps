"""DEV-2437: Point of Sale cash as efis's cash deposit request."""

import importlib.util
import unittest

import support  # registers the main module under its Odoo name


def _load_deposits():
    # The request builder alone, without the add-on's models (and so without Odoo).
    spec = importlib.util.spec_from_file_location("metanow_fiscalization_pos_deposits", support.POS_DIR / "efis" / "deposits.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deposits = _load_deposits()
AT = "2026-11-11T08:00:05+01:00"


class BuildTests(unittest.TestCase):
    def test_the_opening_float(self):
        self.assertEqual(deposits.build("INITIAL", 1500, "dd123dd123", AT),
                         {"operation": "INITIAL", "amount": "1500.00", "deviceCode": "dd123dd123", "changedAt": AT})

    def test_an_empty_register_opens_at_zero(self):
        self.assertEqual(deposits.build("INITIAL", 0, "dd123dd123", AT)["amount"], "0.00")

    def test_cash_in_and_out_move_some_cash(self):
        self.assertEqual(deposits.build("WITHDRAW", 50.5, "dd123dd123", AT)["amount"], "50.50")
        for operation in ("DEPOSIT", "WITHDRAW"):
            with self.assertRaises(deposits.DepositInvalid, msg=operation) as raised:
                deposits.build(operation, 0, "dd123dd123", AT)
            self.assertIn("refuses zero", str(raised.exception))

    def test_an_amount_is_never_negative(self):
        with self.assertRaises(deposits.DepositInvalid):
            deposits.build("INITIAL", -1, "dd123dd123", AT)

    def test_money_carries_exactly_two_decimals(self):
        with self.assertRaises(deposits.DepositInvalid):
            deposits.build("DEPOSIT", 1.005, "dd123dd123", AT)

    def test_the_device_is_required(self):
        for device in (None, "", "  "):
            with self.assertRaises(deposits.DepositInvalid, msg=repr(device)):
                deposits.build("DEPOSIT", 10, device, AT)

    def test_the_time_has_an_offset_and_whole_seconds(self):
        for bad in ("2026-11-11T08:00:05", "2026-11-11T08:00:05.120+01:00", None):
            with self.assertRaises(deposits.DepositInvalid, msg=bad):
                deposits.build("DEPOSIT", 10, "dd123dd123", bad)

    def test_only_efis_operations(self):
        with self.assertRaises(deposits.DepositInvalid):
            deposits.build("CLOSE", 10, "dd123dd123", AT)
        self.assertEqual(deposits.OPERATIONS, ("INITIAL", "DEPOSIT", "WITHDRAW"))

    def test_the_body_is_efis_published_shape(self):
        schema = support.openapi()["components"]["schemas"]["CashDepositRequest"]
        self.assertEqual(set(deposits.build("DEPOSIT", 10, "dd123dd123", AT)), set(schema["properties"]))


class AnswerTests(unittest.TestCase):
    def test_efis_answer_under_the_record_s_prefix(self):
        answer = {"id": "c-1", "state": "queued", "registrationCode": None}
        self.assertEqual(deposits.answer_values(answer, "metanow_fiscal_initial"), {
            "metanow_fiscal_initial_document_id": "c-1",
            "metanow_fiscal_initial_state": "sent",
            "metanow_fiscal_initial_registration_code": False,
        })
        registered = deposits.answer_values(dict(answer, state="accepted", registrationCode="r"), "metanow_fiscal_deposit")
        self.assertEqual(registered["metanow_fiscal_deposit_state"], "registered")

    def test_the_answer_is_efis_published_shape(self):
        properties = set(support.openapi()["components"]["schemas"]["CashDepositResponse"]["properties"])
        self.assertLessEqual({"id", "state", "registrationCode"}, properties)


if __name__ == "__main__":
    unittest.main()
