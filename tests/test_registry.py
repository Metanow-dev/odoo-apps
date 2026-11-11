"""DEV-2436: fiscal codes are chosen from the codes efis has confirmed, never typed by hand."""

import json
import unittest
from types import SimpleNamespace

import fake_odoo
import support
from metanow_fiscalization.efis import client as efis_client
from metanow_fiscalization.efis import outcome
from metanow_fiscalization.efis import registry as efis_registry
from metanow_fiscalization.efis.registry import Registry
from metanow_fiscalization.models.account_journal import AccountJournal
from metanow_fiscalization.models.res_config_settings import ResConfigSettings
from metanow_fiscalization.models.res_users import ResUsers
from test_account_move import company, environment, move


def paired(env, opener=None, **overrides):
    """A paired company that has read its registry from efis."""
    return company(env, opener=opener, metanow_fiscal_registry=support.registry().to_json(), **overrides)


class RegistryTests(unittest.TestCase):
    def test_the_codes_are_read_as_efis_serves_them(self):
        registry = support.registry()
        self.assertTrue(registry.confirms(efis_registry.BUSINESS_UNIT, "bb456bb456"))
        self.assertFalse(registry.confirms(efis_registry.BUSINESS_UNIT, "oo123oo123"), "each kind is its own list")
        self.assertFalse(registry.confirms(efis_registry.OPERATOR, ""))
        self.assertEqual(registry.choices(efis_registry.DEVICE),
                         [("dd123dd123", "dd123dd123"), ("dd456dd456", "dd456dd456")])

    def test_the_stored_registry_reads_back_the_same(self):
        registry = support.registry()
        self.assertEqual(Registry.from_json(registry.to_json()).codes, registry.codes)
        self.assertIsNone(Registry.from_json(False), "a company that has not read it yet has none")

    def test_an_unreadable_registry_is_refused(self):
        for document in (None, [], {"businessUnits": ["bb123bb123"]},
                         dict(support.REGISTRY_DOCUMENT, operators="oo123oo123"),
                         dict(support.REGISTRY_DOCUMENT, devices=[""])):
            with self.assertRaises(ValueError, msg=repr(document)):
                Registry(document)

    def test_only_unconfirmed_codes_in_the_body_are_named(self):
        registry = support.registry()
        body = {"businessUnitCode": "bb123bb123", "operatorCode": "typo", "softwareCode": "ss123ss123"}
        problems = registry.unconfirmed(body)
        self.assertEqual(len(problems), 1)
        self.assertIn("typo", problems[0])
        self.assertEqual(registry.unconfirmed({"deviceCode": "dd999dd999"})[0].count("dd999dd999"), 2)
        self.assertEqual(registry.unconfirmed({}), [])

    def test_the_kinds_are_the_ones_efis_publishes(self):
        schema = support.openapi()["components"]["schemas"]["RegistryCodesResponse"]
        self.assertEqual(set(schema["required"]), set(efis_registry.KINDS))

    def test_the_body_fields_are_the_ones_efis_reads(self):
        properties = support.openapi()["components"]["schemas"]["SubmitDocumentRequest"]["properties"]
        for name, _ in efis_registry.BODY_FIELDS:
            self.assertIn(name, properties)


class ClientTests(unittest.TestCase):
    def test_the_registry_read_carries_the_credential_and_the_tenant(self):
        opener = support.FakeOpener((200, support.REGISTRY_DOCUMENT))
        client = efis_client.EfisClient("https://efis.example.invalid", credential="c-1", tenant="t-1", opener=opener)
        response = client.get_registry()
        self.assertEqual(response.body, support.REGISTRY_DOCUMENT)
        request = opener.requests[0]
        self.assertEqual((request["method"], request["url"]), ("GET", "https://efis.example.invalid/v1/registry"))
        self.assertEqual((request["headers"]["x-credential"], request["headers"]["x-tenant-id"]), ("c-1", "t-1"))


class CompanyTests(unittest.TestCase):
    def test_refreshing_stores_the_codes_in_one_write(self):
        env = environment()
        record = company(env, opener=support.FakeOpener((200, support.REGISTRY_DOCUMENT)))
        record._metanow_fiscal_refresh_registry()
        self.assertEqual(list(record.writes[0]), ["metanow_fiscal_registry"])
        self.assertEqual(record._metanow_fiscal_registry().codes, support.registry().codes)

    def test_an_error_or_an_unreadable_answer_stores_nothing(self):
        for answer in ((403, {"code": "forbidden", "retryable": False, "authorityCode": None}),
                       (200, {"businessUnits": "x"})):
            env = environment()
            record = company(env, opener=support.FakeOpener(answer))
            with self.assertRaises(fake_odoo.UserError):
                record._metanow_fiscal_refresh_registry()
            self.assertEqual(record.writes, [])

    def test_no_answer_stores_nothing(self):
        env = environment()
        record = company(env, opener=support.FakeOpener(OSError("refused")))
        with self.assertRaises(fake_odoo.UserError):
            record._metanow_fiscal_refresh_registry()
        self.assertEqual(record.writes, [])

    def test_a_company_code_must_be_confirmed(self):
        env = environment()
        paired(env)._check_metanow_fiscal_codes()
        with self.assertRaises(fake_odoo.ValidationError):
            paired(env, metanow_fiscal_operator_code="typo")._check_metanow_fiscal_codes()

    def test_a_code_stored_before_the_registry_was_read_is_left_alone(self):
        env = environment()
        company(env, metanow_fiscal_operator_code="typed")._check_metanow_fiscal_codes()

    def test_the_choices_cover_every_open_company(self):
        env = environment()
        other = company(env, metanow_fiscal_registry=Registry(
            dict(support.REGISTRY_DOCUMENT, operators=["oo789oo789"])).to_json())
        env.companies = [paired(env), other, company(env)]
        codes = [code for code, _ in paired(env)._metanow_fiscal_choices(efis_registry.OPERATOR)]
        self.assertEqual(codes, ["oo123oo123", "oo456oo456", "oo789oo789"])


class SettingsTests(unittest.TestCase):
    def settings(self, env, record, **values):
        env.company = record
        return ResConfigSettings(env, company_id=record, **values)

    def test_a_confirmed_stored_code_is_shown_and_an_unconfirmed_one_is_not(self):
        env = environment()
        record = paired(env, metanow_fiscal_operator_code="typo")
        values = self.settings(env, record).get_values()
        self.assertEqual(values["metanow_fiscal_business_unit_code_pick"], "bb123bb123")
        self.assertFalse(values["metanow_fiscal_operator_code_pick"])

    def test_the_lists_are_the_current_companys_codes(self):
        env = environment()
        settings = self.settings(env, paired(env))
        self.assertEqual(settings._metanow_fiscal_business_unit_choices(), support.registry().choices("businessUnits"))
        self.assertEqual(self.settings(env, company(env))._metanow_fiscal_operator_choices(), [])

    def test_saving_writes_only_the_code_that_was_changed(self):
        env = environment()
        record = paired(env)
        settings = self.settings(env, record, **record_picks(record))
        settings.metanow_fiscal_operator_code_pick = "oo456oo456"
        settings.set_values()
        self.assertEqual(record.writes, [{"metanow_fiscal_operator_code": "oo456oo456"}])

    def test_saving_never_clears_an_unconfirmed_code_nobody_chose_to_change(self):
        # A code typed before DEV-2436, or one efis has since retired, shows as an empty list.
        env = environment()
        record = paired(env, metanow_fiscal_operator_code="typo")
        settings = self.settings(env, record, **record_picks(record))
        self.assertFalse(settings.metanow_fiscal_operator_code_pick)
        settings.set_values()
        self.assertEqual(record.writes, [])
        self.assertEqual(record.metanow_fiscal_operator_code, "typo")

    def test_a_confirmed_code_may_be_cleared(self):
        env = environment()
        record = paired(env, metanow_fiscal_device_code="dd123dd123")
        settings = self.settings(env, record, **record_picks(record))
        settings.metanow_fiscal_device_code_pick = False
        settings.set_values()
        self.assertEqual(record.writes, [{"metanow_fiscal_device_code": False}])

    def test_before_the_registry_is_read_no_code_is_written(self):
        env = environment()
        record = company(env)
        self.settings(env, record).set_values()
        self.assertEqual(record.writes, [])
        self.assertEqual(record.metanow_fiscal_business_unit_code, "bb123bb123")

    def test_the_notice_says_what_is_missing(self):
        env = environment()
        self.assertIn("Check the configuration", self.settings(env, company(env))._metanow_fiscal_registry_notice())
        notice = self.settings(env, paired(env, metanow_fiscal_operator_code="typo"))._metanow_fiscal_registry_notice()
        self.assertIn("typo", notice)
        self.assertNotIn("bb123bb123", notice)
        self.assertIsNone(self.settings(env, paired(env))._metanow_fiscal_registry_notice())
        self.assertIsNone(self.settings(env, company(env, metanow_fiscal_tenant=False))._metanow_fiscal_registry_notice(),
                          "nothing to read before pairing")

    def test_check_the_configuration_reads_the_codes_and_reloads(self):
        env = environment()
        opener = support.FakeOpener((200, support.PROFILE_DOCUMENT), (200, support.REGISTRY_DOCUMENT))
        record = company(env, opener=opener)
        action = self.settings(env, record).action_metanow_fiscal_refresh_profile()
        self.assertEqual(opener.requests[1]["url"], "https://efis.example.invalid/v1/registry")
        self.assertEqual(action, {"type": "ir.actions.client", "tag": "reload"})
        self.assertIsNotNone(record._metanow_fiscal_registry())

    def test_the_settings_page_offers_pick_lists_not_text_fields(self):
        view = (support.MODULE_DIR / "views" / "res_config_settings_views.xml").read_text(encoding="utf-8")
        for name in ("business_unit", "operator", "software", "device"):
            self.assertIn(f'<field name="metanow_fiscal_{name}_code_pick"/>', view)
            self.assertNotIn(f'<field name="metanow_fiscal_{name}_code"/>', view)


def record_picks(record):
    """The pick values the settings page loads with, as get_values gives them."""
    registry = record._metanow_fiscal_registry()
    return {
        f"{name}_pick": record[name] if registry.confirms(kind, record[name]) else False
        for name, kind in (
            ("metanow_fiscal_business_unit_code", "businessUnits"),
            ("metanow_fiscal_operator_code", "operators"),
            ("metanow_fiscal_software_code", "software"),
            ("metanow_fiscal_device_code", "devices"),
        )
    }


class UserAndJournalTests(unittest.TestCase):
    def test_a_person_issues_under_their_own_code_or_the_companys(self):
        env = environment()
        record = paired(env)
        person = ResUsers(env, id=7, company_values={id(record): {"metanow_fiscal_operator_code": "oo456oo456"}})
        self.assertEqual(person._metanow_fiscal_operator_for(record), "oo456oo456")
        self.assertEqual(ResUsers(env, id=8)._metanow_fiscal_operator_for(record), "oo123oo123")

    def test_a_persons_code_must_be_confirmed_for_the_current_company(self):
        env = environment()
        env.company = paired(env)
        ResUsers(env, metanow_fiscal_operator_code="oo456oo456")._check_metanow_fiscal_operator_code()
        with self.assertRaises(fake_odoo.ValidationError):
            ResUsers(env, metanow_fiscal_operator_code="typo")._check_metanow_fiscal_operator_code()

    def test_a_journals_device_must_be_confirmed_for_its_company(self):
        env = environment()
        AccountJournal(env, company_id=paired(env), metanow_fiscal_device_code="dd456dd456")._check_metanow_fiscal_device_code()
        with self.assertRaises(fake_odoo.ValidationError):
            AccountJournal(env, company_id=paired(env), metanow_fiscal_device_code="typo")._check_metanow_fiscal_device_code()
        AccountJournal(env, company_id=company(env), metanow_fiscal_device_code="typo")._check_metanow_fiscal_device_code()


class PostingTests(unittest.TestCase):
    def send(self, record, env, *answers):
        opener = support.FakeOpener(*answers or ((202, support.document_response()),))
        owner = record.company_id
        owner._metanow_fiscal_client = lambda authenticated=True: efis_client.EfisClient(
            owner.metanow_fiscal_api_url, credential="c-1", tenant="shembull", opener=opener)
        record._metanow_fiscal_send()
        return opener

    def test_the_posting_persons_operator_code_is_kept_and_sent(self):
        env = environment()
        record = move(env, paired(env))
        env.user = ResUsers(env, id=7, company_values={id(record.company_id): {"metanow_fiscal_operator_code": "oo456oo456"}})
        record._post()
        self.assertEqual(len(record.writes), 1, "taken in the one posting write")
        self.assertEqual(record.writes[0]["metanow_fiscal_operator_code"], "oo456oo456")
        # The send job runs as the cron's user, who has no code: the posted code still goes.
        env.user = ResUsers(env, id=1)
        opener = self.send(record, env)
        self.assertEqual(opener.requests[0]["body"]["operatorCode"], "oo456oo456")

    def test_without_a_code_of_their_own_the_companys_is_used(self):
        env = environment()
        record = move(env, paired(env))
        record._post()
        self.assertEqual(record.metanow_fiscal_operator_code, "oo123oo123")
        self.assertEqual(self.send(record, env).requests[0]["body"]["operatorCode"], "oo123oo123")

    def test_an_invoice_queued_before_dev_2436_sends_the_companys_code(self):
        env = environment()
        record = move(env, paired(env), metanow_fiscal_state=outcome.TO_SEND,
                      metanow_fiscal_idempotency_key="odoo-db-0001-42-1")
        self.assertEqual(self.send(record, env).requests[0]["body"]["operatorCode"], "oo123oo123")

    def test_a_cash_invoice_carries_its_journals_device(self):
        env = environment()
        record = move(env, paired(env, metanow_fiscal_device_code="dd123dd123"), journal_id=SimpleNamespace(
            metanow_fiscal_payment_method="BANKNOTE", metanow_fiscal_device_code="dd456dd456"))
        record._post()
        self.assertEqual(self.send(record, env).requests[0]["body"]["deviceCode"], "dd456dd456")

    def test_a_cash_invoice_falls_back_to_the_companys_device(self):
        env = environment()
        record = move(env, paired(env, metanow_fiscal_device_code="dd123dd123"), journal_id=SimpleNamespace(
            metanow_fiscal_payment_method="BANKNOTE", metanow_fiscal_device_code=False))
        record._post()
        self.assertEqual(self.send(record, env).requests[0]["body"]["deviceCode"], "dd123dd123")

    def test_an_unconfirmed_code_is_refused_before_posting(self):
        env = environment()
        record = move(env, paired(env, metanow_fiscal_business_unit_code="bb999bb999"))
        with self.assertRaises(fake_odoo.UserError) as raised:
            record._post()
        self.assertIn("bb999bb999", str(raised.exception))
        self.assertEqual(record.writes, [], "nothing queued, nothing numbered")

    def test_a_code_retired_after_posting_parks_the_invoice_unsent(self):
        env = environment()
        record = move(env, paired(env))
        record._post()
        record.company_id.metanow_fiscal_registry = Registry(
            dict(support.REGISTRY_DOCUMENT, businessUnits=["bb456bb456"])).to_json()
        opener = self.send(record, env)
        self.assertEqual(opener.requests, [])
        self.assertEqual(record.metanow_fiscal_state, outcome.PARKED)

    def test_a_company_that_has_not_read_its_codes_is_checked_by_efis_alone(self):
        env = environment()
        record = move(env, company(env, metanow_fiscal_operator_code="typed"))
        record._post()
        self.assertEqual(self.send(record, env).requests[0]["body"]["operatorCode"], "typed")


if __name__ == "__main__":
    unittest.main()
