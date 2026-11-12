"""DEV-1704: efis's webhook posts, checked and acted on by reading efis again."""

import hmac
import json
import unittest
from datetime import date
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.controllers import webhook as controller_module
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis import webhooks
from metanow_fiscalization.models import res_company as res_company_module
from metanow_fiscalization.models.account_move import AccountMove
from test_account_move import company, environment, move

SECRET = "whsec_test-secret"
NOW = 1_800_000_000
DOCUMENT = support.document_response()["id"]


def signature(body, secret=SECRET, at=NOW):
    mac = hmac.new(secret.encode(), f"{at}.".encode() + body, "sha256").hexdigest()
    return f"t={at},v1={mac}"


def message(kind="document.superseded", object_id=DOCUMENT):
    return json.dumps({"id": "e-1", "type": kind, "occurredAt": "2026-11-12T10:00:00Z", "tenantId": "shembull",
                       "correlationId": "c-1", "objectKind": "document", "objectId": object_id,
                       "summaries": {}, "data": {"anything": "ignored"}}).encode()


class SignatureTests(unittest.TestCase):
    def test_a_post_signed_with_the_secret_is_accepted(self):
        body = message()
        self.assertTrue(webhooks.verified(SECRET, signature(body), body, now=NOW + 10))

    def test_another_secret_a_changed_body_or_an_old_post_is_refused(self):
        body = message()
        self.assertFalse(webhooks.verified(SECRET, signature(body, secret="whsec_other"), body, now=NOW))
        self.assertFalse(webhooks.verified(SECRET, signature(body), body + b" ", now=NOW))
        self.assertFalse(webhooks.verified(SECRET, signature(body), body, now=NOW + webhooks.TOLERANCE_SECONDS + 1))
        self.assertFalse(webhooks.verified(SECRET, signature(body), body, now=NOW - webhooks.TOLERANCE_SECONDS - 1))

    def test_a_missing_or_broken_header_is_refused(self):
        body = message()
        for header in (None, "", "v1=abc", "t=abc,v1=abc", f"t={NOW}"):
            self.assertFalse(webhooks.verified(SECRET, header, body, now=NOW), header)
        self.assertFalse(webhooks.verified("", signature(body), body, now=NOW))

    def test_the_header_is_read_as_efis_writes_it(self):
        # efis: "t=<unix seconds>,v1=<hex>", lower-case hex of HMAC-SHA256 over "<t>.<body>".
        body = b'{"type":"document.fiscalised"}'
        header = signature(body)
        self.assertRegex(header, r"^t=\d+,v1=[0-9a-f]{64}$")
        self.assertTrue(webhooks.verified(SECRET, header, body, now=NOW))


class EventTests(unittest.TestCase):
    def test_only_the_type_and_the_object_id_are_read(self):
        self.assertEqual(webhooks.event(message()), {"type": "document.superseded", "object_id": DOCUMENT})

    def test_a_body_that_is_not_an_event_is_none(self):
        for body in (b"", b"not json", b"[]", b'{"objectId": "x"}'):
            self.assertIsNone(webhooks.event(body), body)


class AddressTests(unittest.TestCase):
    def test_a_public_https_odoo_gets_one_url_per_company(self):
        self.assertEqual(webhooks.address("https://erp.example.al/", 3),
                         "https://erp.example.al/metanow_fiscalization/efis/webhook/3")

    def test_an_address_efis_would_refuse_gives_none(self):
        for base in ("http://erp.example.al", "https://localhost:8069", "https://odoo", "https://odoo.local",
                     "https://10.0.0.5", "https://192.168.1.2:8069", "https://user:pw@erp.example.al", "", None):
            self.assertIsNone(webhooks.address(base, 1), base)

    def test_every_topic_is_one_efis_accepts(self):
        description = support.openapi()["paths"]["/v1/webhooks"]["post"]["description"]
        for topic in webhooks.TOPICS:
            self.assertIn(topic, description)


def events_env(base_url="https://erp.example.al"):
    env = environment()
    env.models["ir.config_parameter"].values["web.base.url"] = base_url
    for name in ("inbound", "health"):
        env.refs[f"metanow_fiscalization.ir_cron_metanow_fiscal_{name}"] = fake_odoo.Trigger()
    return env


class RegistrationTests(unittest.TestCase):
    def test_pairing_registers_the_company_url_for_every_topic(self):
        env = events_env()
        opener = support.FakeOpener((200, {"endpoints": []}), (201, {"id": "w-1", "secret": SECRET, "url": "u",
                                                                     "events": [], "createdAt": "x", "disabledAt": None}))
        owner = company(env, opener=opener, id=3)
        self.assertTrue(owner._metanow_fiscal_register_webhook())
        create = opener.requests[1]
        self.assertEqual(create["method"], "POST")
        self.assertTrue(create["url"].endswith("/v1/webhooks"))
        self.assertEqual(create["body"], {"url": "https://erp.example.al/metanow_fiscalization/efis/webhook/3",
                                          "events": list(webhooks.TOPICS)})
        self.assertEqual((owner.metanow_fiscal_webhook_id, owner.metanow_fiscal_webhook_secret), ("w-1", SECRET))

    def test_an_endpoint_efis_already_has_for_this_url_is_disabled_first(self):
        env = events_env()
        url = "https://erp.example.al/metanow_fiscalization/efis/webhook/3"
        opener = support.FakeOpener(
            (200, {"endpoints": [{"id": "old", "url": url, "disabledAt": None},
                                 {"id": "gone", "url": url, "disabledAt": "2026-11-01T00:00:00Z"},
                                 {"id": "other", "url": "https://other.example.al/x", "disabledAt": None}]}),
            (200, {"id": "old"}), (201, {"id": "w-2", "secret": SECRET}))
        owner = company(env, opener=opener, id=3)
        owner._metanow_fiscal_register_webhook()
        self.assertTrue(opener.requests[1]["url"].endswith("/v1/webhooks/old/disable"))
        self.assertEqual(len(opener.requests), 3)

    def test_an_odoo_efis_cannot_reach_is_not_registered_and_says_so(self):
        env = events_env("http://localhost:8069")
        opener = support.FakeOpener()
        owner = company(env, opener=opener)
        self.assertFalse(owner._metanow_fiscal_register_webhook())
        self.assertEqual(opener.requests, [])
        self.assertIn("public https", owner.metanow_fiscal_webhook_status)
        self.assertFalse(owner.metanow_fiscal_webhook_secret)

    def test_a_refusal_leaves_polling_on_and_names_the_reason(self):
        env = events_env()
        owner = company(env, opener=support.FakeOpener((200, {"endpoints": []}), (409, {"code": "webhook_limit_reached"})))
        self.assertFalse(owner._metanow_fiscal_register_webhook())
        self.assertIn("webhook_limit_reached", owner.metanow_fiscal_webhook_status)
        self.assertFalse(owner.metanow_fiscal_webhook_id)

    def test_efis_not_answering_never_undoes_the_pairing(self):
        env = events_env()
        owner = company(env, opener=support.FakeOpener(OSError("down")))
        self.assertFalse(owner._metanow_fiscal_register_webhook())
        self.assertIn("did not answer", owner.metanow_fiscal_webhook_status)


class Records(list):
    def write(self, values):
        for record in self:
            record.write(values)
        return True


class ReceivedTests(unittest.TestCase):
    def setUp(self):
        self.env = events_env()
        self.owner = company(self.env)
        self.found = Records()
        self.domains = []
        moves = AccountMove(self.env)
        moves.search = lambda domain, order=None, limit=None: self.domains.append(domain) or self.found
        self.env.models["account.move"] = moves

    def trigger(self, name):
        return self.env.refs[f"metanow_fiscalization.ir_cron_metanow_fiscal_{name}"].count

    def test_a_document_event_marks_the_invoice_to_be_read_again(self):
        invoice = move(self.env, self.owner, metanow_fiscal_document_id=DOCUMENT, metanow_fiscal_state=outcome.REGISTERED)
        self.found.append(invoice)
        self.owner._metanow_fiscal_webhook_received({"type": "document.superseded", "object_id": DOCUMENT})
        self.assertEqual(invoice.writes, [{"metanow_fiscal_refresh": True}])
        self.assertIn(("metanow_fiscal_document_id", "=", DOCUMENT), self.domains[0])
        self.assertEqual(self.trigger("poll"), 1)
        # Nothing from the post itself is stored.
        self.assertEqual(invoice.metanow_fiscal_state, outcome.REGISTERED)

    def test_a_document_odoo_does_not_have_changes_nothing(self):
        self.owner._metanow_fiscal_webhook_received({"type": "document.corrected", "object_id": "unknown"})
        self.assertEqual(self.trigger("poll"), 0)

    def test_a_received_e_invoice_runs_the_inbound_job(self):
        self.owner._metanow_fiscal_webhook_received({"type": "einvoice.received", "object_id": "x"})
        self.assertEqual(self.trigger("inbound"), 1)

    def test_an_expiring_certificate_runs_the_health_job(self):
        self.owner._metanow_fiscal_webhook_received({"type": "certificate.expiring", "object_id": "cert-1"})
        self.assertEqual(self.trigger("health"), 1)

    def test_a_changed_profile_is_read_again_in_the_health_job(self):
        self.owner._metanow_fiscal_webhook_received({"type": "profile.updated", "object_id": None})
        self.assertTrue(self.owner.metanow_fiscal_profile_stale)
        self.assertEqual(self.trigger("health"), 1)

    def test_any_other_event_is_ignored(self):
        self.owner._metanow_fiscal_webhook_received({"type": "grant.created", "object_id": "g"})
        self.assertEqual(self.owner.writes, [])
        self.assertEqual(self.trigger("poll") + self.trigger("health") + self.trigger("inbound"), 0)


class RefreshPollTests(unittest.TestCase):
    def test_a_registered_invoice_is_read_again_and_becomes_superseded(self):
        env = events_env()
        opener = support.FakeOpener((200, support.document_response(state="superseded", registrationCode="r-1")))
        invoice = move(env, company(env, opener=opener), metanow_fiscal_document_id=DOCUMENT,
                       metanow_fiscal_state=outcome.REGISTERED, metanow_fiscal_security_code=support.SECURITY_CODE,
                       metanow_fiscal_number="1/2026/bb123bb123", metanow_fiscal_registration_code="r-1",
                       metanow_fiscal_noted=True, metanow_fiscal_refresh=True)
        calls = []
        invoice.search = lambda domain, order=None, limit=None: calls.append(domain) or ([invoice] if len(calls) == 1 else [])
        invoice._cron_metanow_fiscal_poll()
        self.assertEqual(invoice.metanow_fiscal_state, outcome.SUPERSEDED)
        self.assertIs(invoice.metanow_fiscal_refresh, False)

    def test_a_failed_read_keeps_the_mark_for_the_next_run(self):
        env = events_env()
        invoice = move(env, company(env, opener=support.FakeOpener(OSError("down"))), metanow_fiscal_document_id=DOCUMENT,
                       metanow_fiscal_state=outcome.REGISTERED, metanow_fiscal_refresh=True)
        calls = []
        invoice.search = lambda domain, order=None, limit=None: calls.append(domain) or ([invoice] if len(calls) == 1 else [])
        invoice._cron_metanow_fiscal_poll()
        self.assertIs(invoice.metanow_fiscal_refresh, True)


class Activities(list):
    def __init__(self, items=(), removed=None):
        super().__init__(items)
        self.removed = removed if removed is not None else []

    def unlink(self):
        self.removed.extend(self)
        return True

    def mapped(self, name):
        return [getattr(item, name) for item in self]


class Users(list):
    def filtered(self, predicate):
        return Users(user for user in self if predicate(user))

    def __sub__(self, other):
        return Users(user for user in self if user not in other)


class HealthTests(unittest.TestCase):
    def health_env(self, existing=()):
        env = events_env()
        removed = []
        self.searched = []
        activity = SimpleNamespace(search=lambda domain: self.searched.append(domain) or Activities(existing, removed),
                                   sudo=lambda: activity)
        env.models["mail.activity"] = activity
        return env, removed

    def owner(self, env, answer):
        scheduled = []
        partner = SimpleNamespace(id=7)
        partner.sudo = lambda: partner
        partner.activity_schedule = lambda xml_id, **values: scheduled.append(dict(values, xml_id=xml_id))
        record = company(env, opener=support.FakeOpener(answer), partner_id=partner)
        self.admin = SimpleNamespace(id=2, share=False, company_ids=[record])
        portal = SimpleNamespace(id=9, share=True, company_ids=[record])
        group = SimpleNamespace(all_user_ids=Users([self.admin, portal]))
        group.sudo = lambda: group
        env.refs["base.group_system"] = group
        return record, scheduled

    def health(self, status, until="2026-12-01T00:00:00Z"):
        return (200, {"tenantId": "shembull", "status": "attention", "attention": [],
                      "certificate": {"status": status, "notAfter": until, "daysLeft": 19}})

    def test_an_expiring_certificate_is_shown_and_each_admin_gets_one_to_do(self):
        env, _ = self.health_env()
        owner, scheduled = self.owner(env, self.health("expiring"))
        owner._metanow_fiscal_refresh_health()
        self.assertEqual(owner.metanow_fiscal_certificate_status, "expiring")
        self.assertEqual(owner.metanow_fiscal_certificate_until, "2026-12-01")
        self.assertEqual([(item["user_id"], item["summary"]) for item in scheduled],
                         [(2, res_company_module.CERTIFICATE_ACTIVITY)])
        self.assertEqual(scheduled[0]["xml_id"], "mail.mail_activity_data_todo")
        owner.metanow_fiscal_certificate_until = date(2026, 12, 1)
        self.assertIn("2026-12-01", owner._metanow_fiscal_certificate_notice())

    def test_an_admin_who_has_the_to_do_already_gets_no_second_one(self):
        env, _ = self.health_env()
        owner, scheduled = self.owner(env, self.health("expired"))
        env.models["mail.activity"].search = lambda domain: Activities([SimpleNamespace(user_id=self.admin)])
        owner._metanow_fiscal_refresh_health()
        self.assertEqual(scheduled, [])

    def test_a_valid_certificate_clears_the_to_do_and_the_warning(self):
        existing = [SimpleNamespace(user_id=SimpleNamespace(id=2))]
        env, removed = self.health_env(existing)
        owner, scheduled = self.owner(env, self.health("valid", "2027-11-01T00:00:00Z"))
        owner._metanow_fiscal_refresh_health()
        self.assertEqual(removed, existing)
        self.assertEqual(scheduled, [])
        self.assertIsNone(owner._metanow_fiscal_certificate_notice())

    def test_a_missing_certificate_is_shown_but_sets_no_to_do(self):
        env, _ = self.health_env()
        owner, scheduled = self.owner(env, self.health("missing", None))
        owner._metanow_fiscal_refresh_health()
        self.assertIn("no signing certificate", owner._metanow_fiscal_certificate_notice())
        self.assertEqual(scheduled, [])

    def test_the_health_read_names_the_tenant(self):
        env, _ = self.health_env()
        owner, _ = self.owner(env, self.health("valid"))
        opener = support.FakeOpener(self.health("valid"))
        owner._metanow_fiscal_client = lambda authenticated=True: __import__(
            "metanow_fiscalization.efis.client", fromlist=["EfisClient"]).EfisClient(
                owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        owner._metanow_fiscal_refresh_health()
        self.assertTrue(opener.requests[0]["url"].endswith("/v1/tenants/shembull/health"))


class Response:
    def __init__(self, status):
        self.status = status


class ControllerTests(unittest.TestCase):
    def post(self, body, header, secret=SECRET):
        received = []
        owner = SimpleNamespace(metanow_fiscal_webhook_secret=secret,
                                _metanow_fiscal_webhook_received=received.append)
        owner.exists = lambda: owner
        companies = SimpleNamespace(browse=lambda company_id: owner)
        companies.sudo = lambda: companies
        request = SimpleNamespace(
            httprequest=SimpleNamespace(get_data=lambda cache=False: body, headers={webhooks.SIGNATURE_HEADER: header}),
            env={"res.company": companies},
            make_response=lambda data, status=200: Response(status))
        original = controller_module.request
        controller_module.request = request
        try:
            verified = webhooks.verified
            webhooks.verified = lambda s, h, b, now=None: verified(s, h, b, now=NOW)
            try:
                answer = controller_module.EfisWebhook().receive(3)
            finally:
                webhooks.verified = verified
        finally:
            controller_module.request = original
        return answer.status, received

    def test_a_signed_post_is_acted_on_and_answered_at_once(self):
        body = message()
        status, received = self.post(body, signature(body))
        self.assertEqual(status, 204)
        self.assertEqual(received, [{"type": "document.superseded", "object_id": DOCUMENT}])

    def test_an_unsigned_or_wrongly_signed_post_changes_nothing(self):
        body = message()
        for header in (None, signature(body, secret="whsec_other"), signature(b"other body")):
            status, received = self.post(body, header)
            self.assertEqual((status, received), (401, []))

    def test_a_company_with_no_secret_refuses_every_post(self):
        body = message()
        self.assertEqual(self.post(body, signature(body), secret=False), (401, []))

    def test_a_signed_body_that_is_not_an_event_is_refused(self):
        body = b"not json"
        self.assertEqual(self.post(body, signature(body)), (400, []))


if __name__ == "__main__":
    unittest.main()
