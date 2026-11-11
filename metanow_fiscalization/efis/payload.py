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

import re
from datetime import date
from decimal import Decimal

from . import money
from .profile import (
    COUNTRY_CODES,
    EXEMPTIONS,
    IDENTIFIER_TYPES,
    PAYMENT_METHODS,
)


# PartyInput.kind's values. The module only ever sends BUSINESS.
BUSINESS = "business"
BUYER_KINDS = (BUSINESS,)

# CorrectionInput.kind for an Odoo credit note (DEV-2431). The person chooses: a mistake on the
# invoice is a corrective, a return or a discount is a credit. efis also accepts "debit", which
# an Odoo credit note never is.
CORRECTIVE = "corrective"
CREDIT = "credit"
CORRECTION_KINDS = (CORRECTIVE, CREDIT)

# The national currency, which efis and the authority's request leave implicit.
NATIONAL_CURRENCY = "ALL"

# The payment methods efis reads as a credit transfer (UNCL 4461 code 30), the only means an
# account to pay into belongs to (efis UblInvoice.PaymentMeansCode; EN 16931 BG-17). efis refuses
# an IBAN on an e-invoice paid any other way (einvoice_account_requires_transfer).
CREDIT_TRANSFER_METHODS = ("ACCOUNT", "TRANSFER")

# efis's own shapes for the account to pay into (EinvoiceDraft.IbanShape, BicShape).
_IBAN_SHAPE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")
_BIC_SHAPE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")


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

    # DEV-2431: a credit note goes as a correction of the invoice it reverses. Odoo keeps its
    # quantities and amounts positive and reads the sign from the move type; efis wants them
    # negative, through the quantity, never the unit price (CanonicalDraft: negative_unit_price).
    correction, correction_problems = _correction(invoice.get("corrects"))
    problems.extend(correction_problems)
    sign = -1 if invoice.get("corrects") is not None else 1

    # The profile's currency list is the authority's foreign-currency codes and does not contain
    # the lek: a lek invoice carries no Currency element, and efis reads an absent payment
    # currency as the national one (DocumentContracts.PaymentInput, CanonicalDraft.NationalCurrency).
    currency = _text(invoice.get("currency"))
    if currency != _text(invoice.get("company_currency")) or currency != NATIONAL_CURRENCY:
        problems.append(
            "Faturat në monedhë të huaj nuk fiskalizohen ende nga ky modul. "
            "/ Invoices in a foreign currency are not fiscalised by this module yet.")

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
        built, line_problems, amounts = _line(number, line, issuer_in_vat, rates, exemptions, sign)
        problems.extend(line_problems)
        if built is not None:
            lines.append(built)
            untaxed_sum += amounts[0]
            tax_sum += amounts[1]
            total_sum += amounts[2]

    try:
        total = sign * money.cents(invoice.get("amount_total"))
        untaxed = sign * money.cents(invoice.get("amount_untaxed"))
        tax = sign * money.cents(invoice.get("amount_tax"))
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
        if total < 0 and correction is None:
            problems.append(
                "Një faturë nuk mund të jetë negative. / An invoice total cannot be negative.")

    buyer, buyer_problems = _buyer(invoice.get("buyer"), profile)
    problems.extend(buyer_problems)

    if invoice.get("corrects") is not None:
        # DEV-2431: a credit note is an e-invoice exactly when the invoice it corrects was one.
        electronic = bool(invoice["corrects"].get("electronic"))
        if electronic and cash:
            problems.append(
                "Fatura që korrigjohet ishte faturë elektronike, prandaj edhe nota e kreditit duhet të jetë, "
                "dhe efis nuk pranon faturë elektronike me para në dorë; zgjidhni një ditar pa para në dorë. "
                "/ The invoice this corrects was an e-invoice, so the credit note must be one too, and efis "
                "refuses an e-invoice paid in cash; choose a non-cash journal.")
    else:
        electronic = _electronic(config, method, cash, invoice.get("buyer"))
    due_date = einvoice = None
    if electronic:
        due_date, einvoice, einvoice_problems = _einvoice_fields(invoice, method)
        problems.extend(einvoice_problems)
    elif not cash and invoice.get("corrects") is None:
        # DEV-2432 (Gleni, 11 Nov 2026): every non-cash invoice carries Odoo's due date (the
        # authority's optional PayDeadline), because efis tracks payments only on an invoice that
        # has one. A cash invoice is paid on the spot; a correction is never a receivable.
        due_date, due_problems = _due_date(invoice)
        problems.extend(due_problems)

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
        }],
        "total": money.format_money(total),
    }
    if buyer is not None:
        body["buyer"] = buyer
    _put(body, "corrects", correction)
    _put(body, "businessUnitCode", _text(config.get("business_unit_code")))
    _put(body, "operatorCode", _text(config.get("operator_code")))
    _put(body, "softwareCode", _text(config.get("software_code")))
    if cash:
        body["deviceCode"] = device_code
    _put(body, "paymentDueDate", due_date)
    if electronic:
        body["electronic"] = True
        # DEV-2358: what an e-invoice carries beyond the fiscal record (efis DEV-2354).
        _put(body, "einvoice", einvoice)
    # issuedAt is left out on purpose: efis stamps the moment it receives the document, which
    # is on time by construction (builder-log 75). Odoo's invoice_date is a date, not a moment.
    return body


def _line(number, line, issuer_in_vat, rates, exemptions, sign=1):
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
        "quantity": money.format_plain(sign * quantity),
        "unitPriceBeforeTax": money.format_plain(before),
        "unitPriceAfterTax": money.format_plain(after),
        "taxRate": money.format_plain(rate),
    }
    _put(built, "exemptionReason", exemption)
    _put(built, "itemCode", _text(line.get("item_code")))
    return built, problems, (sign * untaxed, sign * (total - untaxed), sign * total)


def _correction(corrects):
    """The reference a credit note carries to the invoice it reverses (CorrectionInput).

    The security code and the issue time are the original's, as efis returned them; the kind is
    the one the person chose.
    """
    if corrects is None:
        return None, []
    problems = []
    kind = _text(corrects.get("kind"))
    if kind not in CORRECTION_KINDS:
        problems.append(
            "Zgjidhni nëse nota e kreditit korrigjon një gabim në faturë apo regjistron një kthim ose zbritje. "
            "/ Choose whether the credit note corrects a mistake on the invoice or records a return or discount.")
    code = _text(corrects.get("security_code"))
    issued_at = _text(corrects.get("issued_at"))
    if not code or not issued_at:
        problems.append(
            "Fatura që kthehet nuk është fiskalizuar nga efis. / The invoice being reversed was not fiscalised by efis.")
    if problems:
        return None, problems
    return {"securityCode": code, "issuedAt": issued_at, "kind": kind}, []


def _electronic(config, method, cash, buyer):
    """Whether this invoice goes to the buyer as an e-invoice (SubmitDocumentRequest.electronic).

    Yes for a non-cash invoice to a buyer named by a tax number, while the company has e-invoices
    on. A tax number here is the identifier efis reads by default, with no identifier type set on
    the partner: a domestic taxpayer number. A buyer with another identifier (a passport, a foreign
    VAT number) or none gets a plain fiscal invoice. efis refuses an e-invoice paid in cash, so a
    cash invoice is never one.
    """
    if config.get("einvoice") is not True or not method or cash or not buyer:
        return False
    return bool(_text(buyer.get("identifier"))) and _text(buyer.get("identifier_type")) is None


def _einvoice_fields(invoice, method):
    """The payment due date and the e-invoice part, from what Odoo has on the invoice.

    Only what Odoo holds without guessing is sent: the due date, the company's bank account the
    invoice is paid into (an IBAN, with its BIC and holder), the customer's reference (BT-10) and
    the delivery date. Each is left out when Odoo has none. A value efis would refuse is refused
    here, before posting, so no number is spent on it.
    """
    due_date, problems = _due_date(invoice)

    part = {}
    bank = invoice.get("bank") or {}
    iban = _text(bank.get("iban"))
    if iban is not None and method in CREDIT_TRANSFER_METHODS:
        iban = "".join(iban.split()).upper()
        bic = _text(bank.get("bic"))
        bic = bic.upper() if bic else None
        if not _is_iban(iban):
            problems.append(
                f"Llogaria bankare {iban} nuk është IBAN i vlefshëm. / The bank account {iban} is not a valid IBAN.")
        if bic is not None and not _BIC_SHAPE.match(bic):
            problems.append(f"Kodi BIC {bic} nuk është i vlefshëm. / The BIC {bic} is not valid.")
        payment = {"accountId": iban}
        _put(payment, "serviceProvider", bic)
        _put(payment, "accountName", _text(bank.get("holder")))
        part["payment"] = payment

    _put(part, "buyerReference", _text(invoice.get("customer_reference")))

    delivered = _text(invoice.get("delivery_date"))
    if delivered is not None:
        try:
            date.fromisoformat(delivered)
            part["delivery"] = {"date": delivered}
        except ValueError:
            problems.append(
                f"Data e dorëzimit {delivered} nuk është datë. / The delivery date {delivered} is not a date.")

    return due_date, (part or None), problems


def _due_date(invoice):
    """Odoo's due date as efis's paymentDueDate, or None when Odoo has none."""
    due_date = _text(invoice.get("due_date"))
    if due_date is None:
        return None, []
    try:
        date.fromisoformat(due_date)
    except ValueError:
        return None, [f"Afati i pagesës {due_date} nuk është datë. / The payment due date {due_date} is not a date."]
    return due_date, []


def _is_iban(iban):
    """ISO 13616: efis's shape, then the mod-97 check, as EinvoiceDraft.IsIban does."""
    if not _IBAN_SHAPE.match(iban):
        return False
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(char, 36)) for char in moved)
    return int(digits) % 97 == 1


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
    # PartyInput.kind (DEV-2028). Only "business" is sent: it tells efis that a buyer with no tax
    # or VAT number, such as a foreign company, is not an individual. A person is left to efis's
    # own default, read from the identifier, so a sole trader with a tax number is not called an
    # individual.
    kind = _text(buyer.get("kind"))
    if kind is not None:
        if kind not in BUYER_KINDS:
            problems.append(f"Lloji i blerësit {kind} nuk njihet. / The buyer kind {kind} is not known.")
        party["kind"] = kind
    _put(party, "address", _text(buyer.get("address")))
    _put(party, "town", _text(buyer.get("town")))
    _put(party, "countryCode", country)
    return party, problems
