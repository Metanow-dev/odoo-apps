"""An Odoo customer invoice as efis's SubmitDocumentRequest.

The input is plain values the model layer reads from account.move, so this file can be tested
without Odoo. The output uses only property names published in spec/openapi/v1.json
(SubmitDocumentRequest and its nested inputs); a test checks every key against that file.

Validate before issuing: everything efis would refuse for a reason this module can see is
refused here, while the invoice is still being posted, so no number is spent on it. The checks
reproduce efis's own arithmetic (one rounding rule, half away from zero, line total first) and
refuse an invoice whose totals efis would compute differently from Odoo, rather than send a
fiscal record that disagrees with the books.
"""

from decimal import Decimal

from . import money
from .profile import (
    COUNTRY_CODES,
    CURRENCIES,
    EXEMPTIONS,
    IDENTIFIER_TYPES,
    PAYMENT_METHODS,
)


class PayloadInvalid(ValueError):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = list(problems)


def _text(value):
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None


def _put(target, key, value):
    if value is not None:
        target[key] = value


def build(invoice, config, profile):
    """Return the request body, or raise PayloadInvalid with every problem found."""
    problems = []
    if profile is None:
        raise PayloadInvalid([
            "Profili i vendit nga efis nuk është marrë ende; kontrolloni konfigurimin. "
            "/ efis's country profile has not been fetched yet; run the configuration check.",
        ])

    client_system = _text(config.get("client_system"))
    tax_id = _text(config.get("tax_id"))
    if not client_system:
        problems.append("Kompania nuk është çiftuar me efis. / The company is not paired with efis.")
    if not tax_id:
        problems.append("Kompania nuk ka numër tatimor. / The company has no tax number.")

    issuer_in_vat = config.get("issuer_in_vat")
    if issuer_in_vat not in (True, False):
        problems.append(
            "Zgjidhni nëse kompania është në TVSH. / Choose whether the company is registered for VAT.")

    reference = _text(invoice.get("reference"))
    if not reference:
        problems.append("Fatura nuk ka numër. / The invoice has no number.")

    currency = _text(invoice.get("currency"))
    if currency != _text(invoice.get("company_currency")):
        problems.append(
            "Faturat në monedhë të huaj nuk fiskalizohen ende nga ky modul. "
            "/ Invoices in a foreign currency are not fiscalised by this module yet.")
    elif currency not in profile.codes(CURRENCIES):
        problems.append(f"Monedha {currency} nuk njihet nga efis. / efis does not know the currency {currency}.")

    method = _text(invoice.get("payment_method"))
    if not method:
        problems.append(
            "Ditari nuk ka mënyrë pagese për efis. / The journal has no efis payment method.")
    elif method not in profile.codes(PAYMENT_METHODS):
        problems.append(
            f"Mënyra e pagesës {method} nuk njihet nga efis. / efis does not know the payment method {method}.")
    cash = bool(method) and profile.flag(PAYMENT_METHODS, method, "cash")
    device_code = _text(config.get("device_code"))
    if cash and not device_code:
        problems.append(
            "Një faturë me para në dorë ka nevojë për kodin e pajisjes. "
            "/ A cash invoice needs the device code.")

    rates = profile.tax_rates()
    exemptions = profile.codes(EXEMPTIONS)
    lines = []
    untaxed_sum = Decimal(0)
    tax_sum = Decimal(0)
    total_sum = Decimal(0)
    source_lines = invoice.get("lines") or []
    if not source_lines:
        problems.append("Fatura nuk ka rreshta. / The invoice has no lines.")
    for number, line in enumerate(source_lines, start=1):
        built, line_problems, amounts = _line(number, line, issuer_in_vat, rates, exemptions)
        problems.extend(line_problems)
        if built is not None:
            lines.append(built)
            untaxed_sum += amounts[0]
            tax_sum += amounts[1]
            total_sum += amounts[2]

    try:
        total = money.cents(invoice.get("amount_total"))
        untaxed = money.cents(invoice.get("amount_untaxed"))
        tax = money.cents(invoice.get("amount_tax"))
    except (money.NotExact, TypeError):
        problems.append("Totalet e faturës nuk janë në qindarka. / The invoice totals are not whole cents.")
        total = untaxed = tax = None

    if total is not None and not problems:
        if total_sum != total or untaxed_sum != untaxed or tax_sum != tax:
            problems.append(
                "efis do të llogariste totale të ndryshme nga Odoo "
                f"(pa TVSH {money.format_money(untaxed_sum)}, TVSH {money.format_money(tax_sum)}, "
                f"gjithsej {money.format_money(total_sum)}). Përdorni rrumbullakimin e taksave për rresht. "
                "/ efis would compute totals that differ from Odoo's "
                f"(untaxed {money.format_money(untaxed_sum)}, VAT {money.format_money(tax_sum)}, "
                f"total {money.format_money(total_sum)}). Use per-line tax rounding.")
        if total < 0:
            problems.append(
                "Një faturë nuk mund të jetë negative. / An invoice total cannot be negative.")

    buyer, buyer_problems = _buyer(invoice.get("buyer"), profile)
    problems.extend(buyer_problems)

    if problems:
        raise PayloadInvalid(problems)

    body = {
        "clientSystem": client_system,
        "clientReference": reference,
        "taxId": tax_id,
        "issuerInVat": issuer_in_vat,
        "seller": _seller(config),
        "lines": lines,
        "payments": [{
            "method": method,
            "amount": money.format_money(total),
            "currency": currency,
        }],
        "total": money.format_money(total),
    }
    if buyer is not None:
        body["buyer"] = buyer
    _put(body, "businessUnitCode", _text(config.get("business_unit_code")))
    _put(body, "operatorCode", _text(config.get("operator_code")))
    _put(body, "softwareCode", _text(config.get("software_code")))
    if cash:
        body["deviceCode"] = device_code
    # issuedAt is left out on purpose: efis stamps the moment it receives the document, which
    # is on time by construction (builder-log 75). Odoo's invoice_date is a date, not a moment.
    return body


def _line(number, line, issuer_in_vat, rates, exemptions):
    problems = []
    label = f"Rreshti {number} / Line {number}"
    description = _text(line.get("description"))
    unit = _text(line.get("unit"))
    if not description:
        problems.append(f"{label}: mungon përshkrimi. / the description is missing.")
    if not unit:
        problems.append(f"{label}: mungon njësia. / the unit of measure is missing.")

    try:
        quantity = money.to_decimal(line.get("quantity"))
        if not money.fits_ten_places(quantity):
            raise money.NotExact("quantity")
        total = money.cents(line.get("price_total"))
        untaxed = money.cents(line.get("price_subtotal"))
    except (money.NotExact, TypeError):
        problems.append(f"{label}: sasia ose shuma nuk mund të dërgohen saktë. / the quantity or amount cannot be sent exactly.")
        return None, problems, None

    if quantity <= 0:
        problems.append(
            f"{label}: sasia duhet të jetë pozitive; korrigjimet bëhen me dokument korrigjues. "
            "/ the quantity must be positive; corrections are made with a corrective document.")
        return None, problems, None

    taxes = line.get("taxes") or []
    rate = Decimal(0)
    exemption = None
    if len(taxes) > 1:
        problems.append(f"{label}: ka më shumë se një taksë. / carries more than one tax.")
    elif len(taxes) == 1:
        tax = taxes[0]
        if tax.get("amount_type") != "percent":
            problems.append(f"{label}: taksa nuk është përqindje. / the tax is not a percentage.")
        else:
            rate = money.to_decimal(tax.get("amount"))
            if rate not in rates:
                problems.append(
                    f"{label}: norma e TVSH {money.format_plain(rate)} nuk njihet nga efis. "
                    f"/ efis does not know the VAT rate {money.format_plain(rate)}.")
        exemption = _text(tax.get("exemption"))
        if exemption is not None and exemption not in exemptions:
            problems.append(
                f"{label}: përjashtimi {exemption} nuk njihet nga efis. / efis does not know the exemption {exemption}.")
    if issuer_in_vat is False and rate != 0:
        problems.append(
            f"{label}: ka TVSH, por kompania nuk është në TVSH. / carries VAT, but the company is not registered for VAT.")

    try:
        after = money.unit_price(total, quantity)
        before = money.unit_price(untaxed, quantity)
    except money.NotExact:
        problems.append(
            f"{label}: çmimi për njësi nuk mund të shprehet saktë me dhjetë shifra dhjetore. "
            "/ the unit price cannot be expressed exactly with ten decimals.")
        return None, problems, None

    efis_untaxed = money.untaxed_from_total(total, rate)
    if efis_untaxed != untaxed:
        problems.append(
            f"{label}: efis do të llogariste {money.format_money(efis_untaxed)} pa TVSH, Odoo ka "
            f"{money.format_money(untaxed)}. / efis would compute {money.format_money(efis_untaxed)} untaxed, "
            f"Odoo has {money.format_money(untaxed)}.")

    if problems:
        return None, problems, None

    built = {
        "description": description,
        "unit": unit,
        "quantity": money.format_plain(quantity),
        "unitPriceBeforeTax": money.format_plain(before),
        "unitPriceAfterTax": money.format_plain(after),
        "taxRate": money.format_plain(rate),
    }
    _put(built, "exemptionReason", exemption)
    _put(built, "itemCode", _text(line.get("item_code")))
    return built, problems, (untaxed, total - untaxed, total)


def _seller(config):
    seller = {"name": _text(config.get("seller_name")) or ""}
    _put(seller, "address", _text(config.get("seller_address")))
    _put(seller, "town", _text(config.get("seller_town")))
    _put(seller, "countryCode", _text(config.get("seller_country_code")))
    return seller


def _buyer(buyer, profile):
    if not buyer:
        return None, []
    problems = []
    name = _text(buyer.get("name"))
    if not name:
        return None, ["Blerësi nuk ka emër. / The buyer has no name."]
    party = {"name": name}
    identifier = _text(buyer.get("identifier"))
    if identifier is not None:
        party["identifier"] = "".join(identifier.split())
    identifier_type = _text(buyer.get("identifier_type"))
    if identifier_type is not None:
        if identifier_type not in profile.codes(IDENTIFIER_TYPES):
            problems.append(
                f"Lloji i identifikuesit {identifier_type} nuk njihet nga efis. "
                f"/ efis does not know the identifier type {identifier_type}.")
        elif identifier is None:
            problems.append(
                "Blerësi ka lloj identifikuesi, por jo identifikues. / The buyer has an identifier type but no identifier.")
        party["identifierType"] = identifier_type
    country = _text(buyer.get("country_code"))
    if country is not None and country not in profile.codes(COUNTRY_CODES):
        problems.append(f"Kodi i vendit {country} nuk njihet nga efis. / efis does not know the country code {country}.")
    _put(party, "address", _text(buyer.get("address")))
    _put(party, "town", _text(buyer.get("town")))
    _put(party, "countryCode", country)
    return party, problems
