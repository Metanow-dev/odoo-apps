"""Static checks on the module's source and its package. CLAUDE.md invariants live here."""

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

import support

MODULE = support.MODULE_DIR
STOCK = support.STOCK_DIR


def _token(*parts):
    return "".join(parts)


def module_files(module, *suffixes):
    return sorted(path for path in module.rglob("*") if path.is_file() and path.suffix in suffixes
                  and "__pycache__" not in path.parts)


# CLAUDE.md: "CIS field names never cross the inbound API". The authority's element names and
# abbreviations, assembled so this file does not trip its own check.
AUTHORITY_VOCABULARY = [
    _token("n", "ipt"), _token("nu", "is"), _token("II", "C"), _token("F", "IC"),
    _token("NS", "LF"), _token("NI", "VF"), _token("Type", "OfInv"), _token("Busin", "UnitCode"),
    _token("Soft", "Code"), _token("TCR", "Code"), _token("Issuer", "NUIS"), _token("Pay", "Method"),
    _token("fault", "Code"), _token("Is", "Subseq", "Deliv"),
]

GOVERNMENT_HOSTS = [
    _token("tatime", ".gov.", "al"), _token("akshi", ".gov"), _token("e-", "albania"), _token("efis", "kalizimi"),
]

# BUILD_SPEC 10.1 and ARCHITECTURE 39: a module's XML IDs are frozen for its life. Renaming one
# orphans the record it was meant to replace. Adding one means adding it here, in a change a
# human merges.
FROZEN_XML_IDS = {
    "ir_cron_metanow_fiscal_send",
    "ir_cron_metanow_fiscal_poll",
    "view_move_form_metanow_fiscal",
    "view_account_invoice_filter_metanow_fiscal",
    "res_config_settings_view_form_metanow_fiscal",
    "view_account_journal_form_metanow_fiscal",
    "view_tax_form_metanow_fiscal",
    "view_partner_form_metanow_fiscal",
    "view_country_form_metanow_fiscal",
    "report_invoice_document_metanow_fiscal",
    # DEV-2436: the operator code on the user form.
    "view_users_form_metanow_fiscal",
    # DEV-2431: the credit note's kind in the reversal wizard.
    "view_account_move_reversal_metanow_fiscal",
    # DEV-2432: the job that sends invoice payments to efis's receivables.
    "ir_cron_metanow_fiscal_payments",
    # DEV-2433: the job that reads received e-invoices.
    "ir_cron_metanow_fiscal_inbound",
}


# DEV-2429: the stock add-on's, frozen the same way.
FROZEN_STOCK_XML_IDS = {
    "ir_cron_metanow_fiscal_wtn_send",
    "ir_cron_metanow_fiscal_wtn_poll",
    "view_warehouse_metanow_fiscal",
    "view_picking_type_form_metanow_fiscal",
    "view_picking_form_metanow_fiscal",
    "report_delivery_document_metanow_fiscal",
}


def manifest(module=MODULE):
    return ast.literal_eval((module / "__manifest__.py").read_text(encoding="utf-8"))


class InvariantTests(unittest.TestCase):
    MODULE = MODULE
    REPORT = "report_invoice.xml"

    def test_skip_readonly_check_is_never_used(self):
        banned = _token("skip_", "readonly_", "check")
        offenders = [str(path) for path in module_files(self.MODULE, ".py", ".xml", ".csv", ".js")
                     if banned in path.read_text(encoding="utf-8")]
        ci = support.ODOO_DIR / "ci"
        offenders += [str(path) for path in ci.rglob("*") if path.is_file() and banned in path.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])

    def test_the_authority_vocabulary_stays_out_of_the_module_code(self):
        offences = []
        for path in module_files(self.MODULE, ".py"):
            source = path.read_text(encoding="utf-8")
            for word in AUTHORITY_VOCABULARY:
                if re.search(rf"\b{re.escape(word)}\b", source, re.IGNORECASE):
                    offences.append(f"{path.name}: {word}")
        self.assertEqual(offences, [])

    def test_the_printed_labels_are_the_only_authority_words_and_only_in_the_report(self):
        report = (self.MODULE / "report" / self.REPORT).read_text(encoding="utf-8")
        for path in module_files(self.MODULE, ".xml"):
            source = path.read_text(encoding="utf-8")
            for word in AUTHORITY_VOCABULARY:
                for match in re.finditer(rf"\b{re.escape(word)}\b", source, re.IGNORECASE):
                    self.assertEqual(path.name, self.REPORT, f"{path.name}: {word}")
                    self.assertIn(f"<strong>{match.group(0)}:</strong>", report, "only as a printed label")

    def test_no_government_host_appears_in_the_module(self):
        offences = []
        for path in module_files(self.MODULE, ".py", ".xml", ".csv"):
            source = path.read_text(encoding="utf-8")
            for host in GOVERNMENT_HOSTS:
                if host.lower() in source.lower():
                    offences.append(f"{path.name}: {host}")
        self.assertEqual(offences, [])

    def test_the_module_never_signs_and_never_holds_a_key(self):
        words = ("pkcs12", "private_key", "load_pem", "load_key_and_certificates", "hashlib", "md5", "sign(")
        offences = [f"{path.name}: {word}" for path in module_files(self.MODULE, ".py")
                    for word in words if word in path.read_text(encoding="utf-8").lower()]
        self.assertEqual(offences, [])

    def test_the_odoo_free_layer_imports_without_odoo(self):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "import efis.payload, efis.client, efis.outcome, efis.profile, efis.qr, efis.registry, efis.receivables, efis.payment_records, efis.inbound;"
            "assert 'odoo' not in sys.modules"
        )
        subprocess.run([sys.executable, "-I", "-c", script, str(self.MODULE)], check=True)


class ManifestTests(unittest.TestCase):
    MODULE = MODULE
    FROZEN = FROZEN_XML_IDS

    def test_one_major_version(self):
        self.assertTrue(manifest(self.MODULE)["version"].startswith("20.0."), "this tree builds the Odoo 20 module only")

    def test_every_data_file_exists_and_parses(self):
        for name in manifest(self.MODULE)["data"]:
            path = self.MODULE / name
            self.assertTrue(path.is_file(), name)
            if path.suffix == ".xml":
                ElementTree.parse(path)

    def test_no_access_file_is_needed(self):
        # The module adds fields to existing models and no model of its own, so it ships no
        # access CSV, whose file and model were renamed between Odoo 19 and 20.
        self.assertFalse([name for name in manifest(self.MODULE)["data"] if name.endswith(".csv")])
        source = "\n".join(path.read_text(encoding="utf-8") for path in module_files(self.MODULE, ".py"))
        self.assertNotRegex(source, r"^\s*_name\s*=", "a new model needs an access rule")

    def test_xml_ids_are_frozen(self):
        found = set()
        for path in module_files(self.MODULE, ".xml"):
            for element in ElementTree.parse(path).iter():
                if element.tag in ("record", "template", "menuitem") and element.get("id"):
                    found.add(element.get("id"))
        self.assertEqual(found, self.FROZEN)

    def test_every_python_file_compiles(self):
        for path in module_files(self.MODULE, ".py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")


class AppsStoreTests(unittest.TestCase):
    """The Odoo Apps store lists the module from branch 20.0 (README.md). It reads the icon and
    the description page from static/description, and a manifest with a price is a paid app."""

    MODULE = MODULE

    def test_the_store_icon_is_a_png(self):
        icon = self.MODULE / "static" / "description" / "icon.png"
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_the_store_description_page_parses(self):
        page = self.MODULE / "static" / "description" / "index.html"
        ElementTree.fromstring("<root>" + page.read_text(encoding="utf-8") + "</root>")

    def test_the_store_name_fits(self):
        # Vendor guidelines: explicit, 25 characters or fewer, no company name.
        name = manifest(self.MODULE)["name"]
        self.assertLessEqual(len(name), 25)
        self.assertNotIn("Metanow", name)

    def test_the_description_page_is_english_with_no_external_link_or_script(self):
        # Vendor guidelines: English, no JavaScript, links only into static/description,
        # YouTube, Teams, mailto: and skype:. Any other external link is invalidated.
        page = (self.MODULE / "static" / "description" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", page.lower())
        self.assertNotRegex(page, r"(?i)(href|src)\s*=\s*[\"']https?://")
        self.assertNotRegex(page, r"[ëç]", "the store page must be in English")
        # The store does not read the page as UTF-8 (the first listing showed "klientÃ«ve").
        self.assertTrue(page.isascii(), "the store page must be plain ASCII")

    def test_every_store_image_exists_and_is_a_png(self):
        # A missing image in the manifest is a scan error, which unpublishes the repository.
        for name in manifest(self.MODULE)["images"]:
            path = self.MODULE / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.read_bytes()[:4], b"\x89PNG", name)
        page = (self.MODULE / "static" / "description" / "index.html").read_text(encoding="utf-8")
        for src in re.findall(r'src="([^"]+)"', page):
            self.assertTrue((self.MODULE / "static" / "description" / src).is_file(), src)

    def test_the_license_file_matches_the_manifest(self):
        self.assertEqual(manifest(self.MODULE)["license"], "OPL-1")
        self.assertIn("Odoo Proprietary License v1.0", (self.MODULE / "LICENSE").read_text(encoding="utf-8"))

    def test_the_module_is_free_and_licensed(self):
        self.assertNotIn("price", manifest(self.MODULE))
        self.assertNotIn("currency", manifest(self.MODULE))
        self.assertTrue(manifest(self.MODULE).get("license"))


# DEV-2429: the stock add-on is held to the same invariants, manifest checks and store rules. It
# ships in the same repository and branch 20.0, where the store reads every module folder.
class StockInvariantTests(InvariantTests):
    MODULE = STOCK
    REPORT = "report_deliveryslip.xml"

    def test_the_odoo_free_layer_imports_without_odoo(self):
        # The add-on's request builder uses the main module's money formatting, which Odoo knows
        # as odoo.addons.metanow_fiscalization.efis. Nothing of Odoo's ORM is loaded.
        script = (
            "import sys, types, importlib.util; sys.path.insert(0, sys.argv[1]);"
            "import efis, efis.money;"
            "addons = types.ModuleType('odoo.addons'); main = types.ModuleType('odoo.addons.metanow_fiscalization');"
            "sys.modules.update({'odoo': types.ModuleType('odoo'), 'odoo.addons': addons,"
            " 'odoo.addons.metanow_fiscalization': main, 'odoo.addons.metanow_fiscalization.efis': efis,"
            " 'odoo.addons.metanow_fiscalization.efis.money': efis.money});"
            "spec = importlib.util.spec_from_file_location('transport', sys.argv[2]);"
            "spec.loader.exec_module(importlib.util.module_from_spec(spec));"
            "assert not [name for name in sys.modules if name in ('odoo.models', 'odoo.fields', 'odoo.api')]"
        )
        subprocess.run([sys.executable, "-I", "-c", script, str(MODULE), str(STOCK / "efis" / "transport.py")], check=True)


class StockManifestTests(ManifestTests):
    MODULE = STOCK
    FROZEN = FROZEN_STOCK_XML_IDS

    def test_it_is_an_add_on_to_the_main_module_and_stock(self):
        self.assertEqual(manifest(STOCK)["depends"], ["metanow_fiscalization", "stock"])
        self.assertNotIn("stock", manifest(MODULE)["depends"], "the main module never makes a customer install Inventory")


class StockAppsStoreTests(AppsStoreTests):
    MODULE = STOCK


def _package_tool():
    spec = importlib.util.spec_from_file_location("odoo_package", support.ODOO_DIR / "tools" / "package.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    return package


class PackageTests(unittest.TestCase):
    def build(self, out, **options):
        return _package_tool().build(out, **options)

    def test_the_zip_is_reproducible_and_checksummed(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            path_a, digest_a = self.build(first)
            path_b, digest_b = self.build(second)
            self.assertEqual(digest_a, digest_b)
            self.assertEqual(hashlib.sha256(path_a.read_bytes()).hexdigest(), digest_a)
            checksum = Path(first, path_a.name + ".sha256").read_text(encoding="ascii")
            self.assertEqual(checksum, f"{digest_a}  {path_a.name}\n")

    def test_the_zip_holds_the_module_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as out:
            path, _ = self.build(out)
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        self.assertIn("metanow_fiscalization/__manifest__.py", names)
        self.assertTrue(all(name.startswith("metanow_fiscalization/") for name in names))
        self.assertFalse([name for name in names if "__pycache__" in name or "/tests/" in name])
        self.assertFalse([name for name in names if Path(name).suffix in (".pem", ".key", ".p12", ".pfx")])

    def test_the_stock_add_on_is_its_own_reproducible_zip(self):
        # DEV-2429: each module ships alone, so a customer without Inventory never gets the add-on.
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            path, digest = self.build(first, module="metanow_fiscalization_stock", manifest=True)
            _, again = self.build(second, module="metanow_fiscalization_stock")
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
            facts = json.loads(Path(first, "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(digest, again)
        self.assertEqual(path.name, f"metanow_fiscalization_stock-{manifest(STOCK)['version']}.zip")
        self.assertTrue(all(name.startswith("metanow_fiscalization_stock/") for name in names))
        self.assertFalse([name for name in names if "__pycache__" in name])
        self.assertEqual(facts["module"], "metanow_fiscalization_stock")

    def test_only_this_repository_s_modules_are_packaged(self):
        with tempfile.TemporaryDirectory() as out, self.assertRaises(ValueError):
            self.build(out, module="sale")

    def test_no_manifest_is_written_unless_asked(self):
        with tempfile.TemporaryDirectory() as out:
            self.build(out)
            self.assertFalse(Path(out, "manifest.json").exists())

    def test_the_manifest_states_the_zip_it_sits_beside(self):
        # The web app's Plugins page shows these facts as they are; a wrong size or checksum
        # here is a wrong statement to the customer about the file they install.
        with tempfile.TemporaryDirectory() as out:
            path, digest = self.build(out, manifest=True)
            facts = json.loads(Path(out, "manifest.json").read_text(encoding="utf-8"))
            size = len(path.read_bytes())
        self.assertEqual(set(facts), {"module", "fileName", "version", "size", "sha256"})
        self.assertEqual(facts["module"], "metanow_fiscalization")
        self.assertEqual(facts["fileName"], path.name)
        self.assertEqual(facts["version"], manifest()["version"])
        self.assertEqual(facts["fileName"], f"metanow_fiscalization-{manifest()['version']}.zip")
        self.assertEqual(facts["size"], size)
        self.assertEqual(facts["sha256"], digest)
        self.assertRegex(facts["sha256"], r"^[0-9a-f]{64}$")

    def test_the_manifest_is_reproducible(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.build(first, manifest=True)
            self.build(second, manifest=True)
            self.assertEqual(Path(first, "manifest.json").read_bytes(), Path(second, "manifest.json").read_bytes())

    def test_the_command_line_writes_the_manifest(self):
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run(
                [sys.executable, str(support.ODOO_DIR / "tools" / "package.py"), "--out", out, "--manifest"],
                capture_output=True, text=True, check=True)
            zip_path, digest = result.stdout.split()
            facts = json.loads(Path(out, "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(facts["fileName"], Path(zip_path).name)
        self.assertEqual(facts["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
