import unittest

import support
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis.client import Response

EMPTY = {}


def error(status, code, retryable=False):
    return Response(status, {"code": code, "retryable": retryable, "authorityCode": None})


class SubmissionOutcomeTests(unittest.TestCase):
    def test_an_accepted_submission_is_one_set_of_values(self):
        values = outcome.submission_values(EMPTY, Response(202, support.document_response()))
        self.assertEqual(values[outcome.DOCUMENT_ID], "7d4c2b9e-5b8f-4f7e-9a51-3c2d1e0f9a88")
        self.assertEqual(values[outcome.SECURITY_CODE], support.SECURITY_CODE)
        self.assertEqual(values[outcome.FISCAL_NUMBER], "1/2026/bb123bb123")
        self.assertEqual(values[outcome.STATE], outcome.SENT)
        self.assertEqual(values[outcome.EFIS_STATE], "numbered")
        self.assertIs(values[outcome.ERROR], False)
        self.assertNotIn(outcome.REGISTRATION_CODE, values, "an absent code is not written as empty")

    def test_a_refusal_is_terminal(self):
        values = outcome.submission_values(EMPTY, error(422, "payments_do_not_match_total"))
        self.assertEqual(values[outcome.STATE], outcome.REFUSED)
        self.assertIn("payments_do_not_match_total", values[outcome.ERROR])

    def test_the_message_comes_first_and_the_code_after_it(self):
        answer = Response(422, {"code": "cash_over_ceiling", "detail": "A cash invoice is at most 100000.00 ALL.",
                                "retryable": False, "authorityCode": None, "correlationId": "c0ffee"})
        values = outcome.submission_values(EMPTY, answer)
        self.assertEqual(values[outcome.ERROR],
                         "A cash invoice is at most 100000.00 ALL.\nKodi / Code: cash_over_ceiling · c0ffee")

    def test_a_refusal_without_a_sentence_still_names_its_code(self):
        values = outcome.submission_values(EMPTY, error(422, "buyer_personal_id_required"))
        self.assertEqual(values[outcome.ERROR], "Kodi / Code: buyer_personal_id_required")

    def test_only_a_refusal_before_numbering_returns_to_draft(self):
        self.assertTrue(outcome.returns_to_draft(error(422, "cash_over_ceiling")))
        self.assertTrue(outcome.returns_to_draft(error(400, "invalid_document")))
        for status in (401, 403, 404, 409, 429, 502):
            self.assertFalse(outcome.returns_to_draft(error(status, "x")), status)
        self.assertFalse(outcome.returns_to_draft(None))

    def test_a_retryable_flag_on_a_4xx_is_honoured(self):
        values = outcome.submission_values(EMPTY, error(409, "in_progress", retryable=True))
        self.assertNotIn(outcome.STATE, values)

    def test_lost_credential_parks_until_paired_again(self):
        for status in (401, 403):
            values = outcome.submission_values(EMPTY, error(status, "unauthorised"))
            self.assertEqual(values[outcome.STATE], outcome.PARKED)

    def test_rate_limit_and_unavailable_leave_it_queued(self):
        for answer in (error(429, "rate_limited", True), error(503, "unavailable", True), Response(502, None)):
            values = outcome.submission_values(EMPTY, answer)
            self.assertNotIn(outcome.STATE, values, answer)

    def test_a_5xx_efis_marks_final_is_parked_not_retried(self):
        values = outcome.submission_values(EMPTY, error(502, "authority_refused", retryable=False))
        self.assertEqual(values[outcome.STATE], outcome.PARKED)


class WriteOnceTests(unittest.TestCase):
    def test_a_stored_security_code_is_never_replaced(self):
        current = {outcome.SECURITY_CODE: support.SECURITY_CODE}
        other = support.document_response(securityCode="f" * 32)
        with self.assertRaises(outcome.SecurityCodeConflict):
            outcome.document_values(current, other)

    def test_the_same_security_code_is_not_written_again(self):
        current = {outcome.SECURITY_CODE: support.SECURITY_CODE, outcome.DOCUMENT_ID: support.document_response()["id"]}
        values = outcome.document_values(current, support.document_response())
        self.assertNotIn(outcome.SECURITY_CODE, values)
        self.assertNotIn(outcome.DOCUMENT_ID, values)

    def test_a_registration_answer_adds_the_registration_code(self):
        first = outcome.document_values(EMPTY, support.document_response())
        current = {key: value for key, value in first.items()}
        registered = support.document_response(state="accepted", registrationCode="d9b0b4a1-0000-4000-8000-000000000001",
                                               issuedAt="2026-10-20T10:15:00+02:00")
        values = outcome.poll_values(current, Response(200, registered))
        self.assertEqual(values[outcome.STATE], outcome.REGISTERED)
        self.assertEqual(values[outcome.REGISTRATION_CODE], "d9b0b4a1-0000-4000-8000-000000000001")
        self.assertEqual(values[outcome.ISSUED_AT], "2026-10-20T10:15:00+02:00")

    def test_accepted_is_registered_and_not_settled(self):
        self.assertEqual(outcome.odoo_state("accepted"), outcome.REGISTERED)
        self.assertNotEqual(outcome.odoo_state("accepted"), "settled")

    def test_an_unchanged_answer_writes_nothing(self):
        current = outcome.document_values(EMPTY, support.document_response())
        current.update({outcome.DOCUMENT_ID: support.document_response()["id"]})
        stored = dict(current)
        stored.update({
            outcome.FISCAL_NUMBER: "1/2026/bb123bb123",
            outcome.SECURITY_CODE: support.SECURITY_CODE,
            outcome.VERIFICATION_URL: support.document_response()["verificationUrl"],
        })
        self.assertIsNone(outcome.poll_values(stored, Response(200, support.document_response())))

    def test_an_unknown_efis_state_keeps_being_read(self):
        self.assertIn(outcome.odoo_state("something_new"), outcome.POLLED)


class EfisStateTests(unittest.TestCase):
    def test_every_state_efis_publishes_is_mapped_on_purpose(self):
        schemas = support.openapi()["components"]["schemas"]
        for schema in ("DocumentResponse", "DocumentDetailResponse"):
            published = set(schemas[schema]["properties"]["state"]["enum"])
            self.assertEqual(published - outcome.KNOWN_EFIS_STATES, set(), schema)

    def test_a_draft_in_efis_is_not_numbered_and_is_read_again(self):
        self.assertEqual(outcome.odoo_state("draft"), outcome.SENT)
        self.assertIn(outcome.odoo_state("draft"), outcome.POLLED)
        values = outcome.document_values(EMPTY, support.document_response(
            state="draft", fiscalNumber=None, securityCode=None, verificationUrl=None))
        self.assertEqual(values[outcome.EFIS_STATE], "draft")
        self.assertNotIn(outcome.SECURITY_CODE, values)


EIC = "5e1a0c3d-0000-4000-8000-00000000e1c0"


def einvoice(state="registered", eic=EIC, fault_text=None):
    return {
        "id": "a1b2c3d4-0000-4000-8000-000000000001",
        "documentId": support.document_response()["id"],
        "kind": "invoice", "state": state, "eic": eic, "faultCode": None, "faultText": fault_text,
        "warnings": [], "reconciled": False, "authorityStatus": None, "statusSeenAt": None,
        "createdAt": "2026-11-09T10:00:00+01:00", "answeredAt": None,
    }


class EinvoiceTests(unittest.TestCase):
    def test_not_built_yet_is_nothing_to_store(self):
        self.assertIsNone(outcome.einvoice_values(EMPTY, error(404, "not_found")))
        self.assertIsNone(outcome.einvoice_values(EMPTY, None))

    def test_a_registered_e_invoice_stores_its_state_and_eic(self):
        values = outcome.einvoice_values(EMPTY, Response(200, einvoice()))
        self.assertEqual(values, {outcome.EINVOICE_STATE: "registered", outcome.EINVOICE_EIC: EIC})

    def test_a_refused_e_invoice_stores_efis_sentence(self):
        values = outcome.einvoice_values(EMPTY, Response(200, einvoice("refused", None, "The buyer is not registered.")))
        self.assertEqual(values[outcome.EINVOICE_STATE], "refused")
        self.assertEqual(values[outcome.EINVOICE_ERROR], "The buyer is not registered.")
        self.assertNotIn(outcome.EINVOICE_EIC, values)

    def test_a_stored_eic_is_never_replaced(self):
        current = {outcome.EINVOICE_EIC: EIC}
        with self.assertRaises(outcome.SecurityCodeConflict):
            outcome.einvoice_values(current, Response(200, einvoice(eic="5e1a0c3d-0000-4000-8000-0000000000ff")))

    def test_an_unchanged_answer_is_nothing_to_store(self):
        current = {outcome.EINVOICE_STATE: "registered", outcome.EINVOICE_EIC: EIC}
        self.assertIsNone(outcome.einvoice_values(current, Response(200, einvoice())))

    def test_pending_until_registered_or_refused(self):
        for state in (None, "queued", "sending", "unanswered"):
            self.assertTrue(outcome.einvoice_pending({outcome.ELECTRONIC: True, outcome.EINVOICE_STATE: state}), state)
        for state in ("registered", "refused"):
            self.assertFalse(outcome.einvoice_pending({outcome.ELECTRONIC: True, outcome.EINVOICE_STATE: state}))
        self.assertFalse(outcome.einvoice_pending({outcome.ELECTRONIC: False}))

    def test_the_e_invoice_states_are_efis_published_ones(self):
        properties = support.openapi()["components"]["schemas"]["EinvoiceOutboundView"]["properties"]
        self.assertIn("state", properties)
        self.assertIn("eic", properties)
        self.assertIn("faultText", properties)


class WarningTests(unittest.TestCase):
    def stored(self):
        current = outcome.document_values(EMPTY, support.document_response())
        current.update({
            outcome.DOCUMENT_ID: support.document_response()["id"],
            outcome.FISCAL_NUMBER: "1/2026/bb123bb123",
            outcome.SECURITY_CODE: support.SECURITY_CODE,
            outcome.VERIFICATION_URL: support.document_response()["verificationUrl"],
        })
        return current

    def test_a_read_stores_efis_warnings(self):
        answer = support.document_response(warnings=["buyer_identified_by_passport"])
        values = outcome.poll_values(self.stored(), Response(200, answer))
        self.assertEqual(values[outcome.WARNINGS], "buyer_identified_by_passport")

    def test_several_warnings_are_one_per_line(self):
        answer = support.document_response(warnings=["first", "second"])
        values = outcome.document_values(EMPTY, answer)
        self.assertEqual(values[outcome.WARNINGS], "first\nsecond")

    def test_an_answer_without_the_list_leaves_stored_warnings_alone(self):
        values = outcome.submission_values(EMPTY, Response(202, support.document_response()))
        self.assertNotIn(outcome.WARNINGS, values)

    def test_an_empty_list_clears_them(self):
        current = self.stored()
        current[outcome.WARNINGS] = "buyer_identified_by_passport"
        values = outcome.poll_values(current, Response(200, support.document_response(warnings=[])))
        self.assertIs(values[outcome.WARNINGS], False)


if __name__ == "__main__":
    unittest.main()
