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


if __name__ == "__main__":
    unittest.main()
