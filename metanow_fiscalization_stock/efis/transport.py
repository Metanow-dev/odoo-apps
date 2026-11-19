"""An Odoo stock transfer as efis's TransportDocumentRequest: the warehouse transfer note (DEV-2429).

The faturë shoqëruese (Law 87/2019 Arts. 39-42) goes with goods that move inside Albania with no
change of owner. It is issued today, immediately before the goods move (Art. 39(3)), and numbered
from the sequence of the business unit the goods leave from (Udhëzim 20 Art. 26). efis numbers
it, computes its security code and registers it; this file only builds the request from plain
values the model reads, and refuses here, before anything is sent, what efis would refuse.

DEV-2430: the same transfer may instead go as a transport invoice (registerTI), for goods sent to
another person for processing, examination or repair with no change of owner. It is the same
request with a receiver, its own transactions, and narrower kinds of place.

The codes below are efis's published contract (TransportDocumentRequest, TransportInvoiceRequest,
TransportPlaceInput), not a list chosen here. Which Odoo transfer gets which document and
transaction is the business's choice, on each operation type, with no default: it is still a
working guess for the accountant (DEV-2441) and the helpdesk (DEV-2036).
"""

import re

from odoo.addons.metanow_fiscalization.efis import inbound as efis_inbound
from odoo.addons.metanow_fiscalization.efis import money
from odoo.addons.metanow_fiscalization.efis.profile import CARRIER_ID_TYPES

# The two documents a transfer can issue.
TRANSFER_NOTE = "wtn"
TRANSPORT_INVOICE = "ti"

TRANSACTIONS = ("TRANSFER", "DOOR", "SALES", "EXAMINATION")
# Where goods may go; where they may leave from adds another person's warehouse and customs.
DESTINATION_KINDS = ("WAREHOUSE", "EXHIBITION", "STORE", "SALE", "OTHER")
ORIGIN_KINDS = DESTINATION_KINDS + ("ANOTHER", "CUSTOMS")
VEHICLE_OWNERSHIPS = ("OWNER", "THIRDPARTY")

# DEV-2430: a transport invoice's (TransportInvoiceRequest; TIStartPointSType, TIDestinPointSType).
TI_TRANSACTIONS = ("PROCESSING", "EXAMINATION", "REPAIR")
TI_KINDS = ("WAREHOUSE", "EXHIBITION", "STORE", "OTHER")

RULES = {
    TRANSFER_NOTE: {"transactions": TRANSACTIONS, "origins": ORIGIN_KINDS, "destinations": DESTINATION_KINDS},
    TRANSPORT_INVOICE: {"transactions": TI_TRANSACTIONS, "origins": TI_KINDS, "destinations": TI_KINDS},
}

# CLAUDE.md: an explicit numeric offset and whole seconds, never milliseconds.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
# DEV-2640: the authority's own VehPlatesSType, which efis checks before it numbers anything.
# "AA 123 BB", the way plates are written here, is not it, and a note carrying one can never be
# accepted -- so it is refused here, where the person can still correct it, and never rewritten.
_PLATES = re.compile(r"^[A-Za-z0-9-]+$")


class TransportInvalid(ValueError):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = list(problems)


def _text(value):
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None


def build(note, config, kind=TRANSFER_NOTE, profile=None):
    """Return the request body, or raise TransportInvalid with every problem found."""
    rules = RULES[kind]
    problems = []

    transaction = _text(note.get("transaction"))
    if transaction not in rules["transactions"]:
        problems.append(
            "Lloji i operacionit nuk ka transaksion për këtë dokument; zgjidheni te lloji i operacionit. "
            "/ The operation type has no transaction for this document; choose it on the operation type.")

    for key, label in (
        ("business_unit_code", "Magazina nuk ka kod të njësisë së biznesit. / The warehouse has no business unit code."),
        ("operator_code", "Mungon kodi i operatorit. / The operator code is missing."),
        ("software_code", "Kompania nuk ka kod programi. / The company has no software code."),
    ):
        if not _text(config.get(key)):
            problems.append(label)

    issued_at = _text(note.get("issued_at"))
    if not issued_at or not _TIMESTAMP.match(issued_at):
        problems.append(f"Koha e lëshimit {issued_at} nuk është e saktë. / The issue time {issued_at} is not exact.")

    try:
        value = money.cents(note.get("value_of_goods"))
        if value < 0:
            raise money.NotExact("negative")
    except (money.NotExact, TypeError):
        problems.append("Vlera e mallit nuk jepet saktë në qindarka. / The value of the goods is not exact in cents.")
        value = None

    plates = _text(note.get("vehicle_plates"))
    if not plates:
        problems.append("Mungojnë targat e mjetit. / The vehicle plates are missing.")
    elif not _PLATES.match(plates):
        problems.append(
            "Targat e mjetit shkruhen vetëm me shkronja, numra dhe vija, pa hapësira: AA123BB. "
            "/ The vehicle plates are letters, digits and hyphens only, with no spaces: AA123BB.")
    ownership = _text(note.get("vehicle_ownership"))
    if ownership not in VEHICLE_OWNERSHIPS:
        problems.append("Zgjidhni kujt i përket mjeti. / Choose whom the vehicle belongs to.")

    origin, origin_problems = _place(note.get("from"), rules["origins"], "Nisja / Origin")
    destination, destination_problems = _place(note.get("to"), rules["destinations"], "Destinacioni / Destination")
    problems.extend(origin_problems + destination_problems)

    receiver = None
    if kind == TRANSPORT_INVOICE:
        # Optional in efis's contract; required here, as efis's console and Self-Care require it:
        # a transport invoice names whom the goods go to.
        given = note.get("receiver") or {}
        receiver = {"taxId": _text(given.get("tax_id")), "name": _text(given.get("name")),
                    "address": _text(given.get("address")), "town": _text(given.get("town"))}
        if not all(receiver.values()):
            problems.append(
                "Marrësi ka nevojë për numër tatimor, emër, adresë dhe qytet. "
                "/ The receiver needs a tax number, a name, an address and a town.")

    issuer = note.get("issuer") or {}
    issuer_body = {"name": _text(issuer.get("name")), "address": _text(issuer.get("address")), "town": _text(issuer.get("town"))}
    if not all(issuer_body.values()):
        problems.append("Kompania ka nevojë për emër, adresë dhe qytet. / The company needs a name, an address and a town.")

    carrier, carrier_problems = _carrier(note.get("carrier"), profile)
    problems.extend(carrier_problems)

    goods = []
    for number, item in enumerate(note.get("goods") or [], start=1):
        label = f"Rreshti {number} / Line {number}"
        name, unit = _text(item.get("name")), _text(item.get("unit"))
        try:
            quantity = money.to_decimal(item.get("quantity"))
            exact = money.fits_ten_places(quantity)
        except (money.NotExact, TypeError):
            quantity, exact = None, False
        if not name or not unit:
            problems.append(f"{label}: mungon emri ose njësia. / the name or the unit is missing.")
        if quantity is None or not exact or quantity <= 0:
            problems.append(f"{label}: sasia duhet të jetë pozitive. / the quantity must be positive.")
            continue
        line = {"name": name, "unit": unit, "quantity": money.format_plain(quantity)}
        code = _text(item.get("code"))
        if code:
            line["code"] = code
        goods.append(line)
    if not note.get("goods"):
        problems.append("Transferimi nuk ka mallra. / The transfer moves no goods.")

    if problems:
        raise TransportInvalid(problems)

    body = {
        "transaction": transaction,
        "businessUnitCode": _text(config.get("business_unit_code")),
        "operatorCode": _text(config.get("operator_code")),
        "softwareCode": _text(config.get("software_code")),
        "issuedAt": issued_at,
        "valueOfGoods": money.format_money(value),
        "vehicleOwnership": ownership,
        "vehiclePlates": plates,
        "from": origin,
        "to": destination,
        "goodsFlammable": bool(note.get("flammable")),
        "escortRequired": bool(note.get("escort")),
        "issuer": issuer_body,
        "carrier": carrier,
        "goods": goods,
    }
    if kind == TRANSPORT_INVOICE:
        body["receiver"] = receiver
    return body


def _carrier(given, profile):
    """Whoever carries the goods when the company does not (Law 87/2019 Art. 40(1)(h)).

    DEV-2444: efis names the carrier's identifier type in its own words and publishes the ones it
    accepts under carrier.idType in the country profile, so the list is read here, never written.
    A partner with no type set means efis's default, a domestic tax number, as it does for a buyer
    (res.partner.metanow_fiscal_identifier_type). A carrier efis cannot be told about -- a foreign
    firm named by its VAT number, say -- is refused here, before anything is sent.
    """
    if not given:
        return None, []
    if profile is None:
        return None, [
            "Profili i vendit nga efis nuk është marrë ende; kontrolloni konfigurimin. "
            "/ efis's country profile has not been fetched yet; run the configuration check.",
        ]
    accepted = profile.codes(CARRIER_ID_TYPES)
    if not accepted:
        return None, [
            "Profili i vendit nga efis nuk ka llojet e identifikuesit të transportuesit; përditësojeni profilin. "
            "/ efis's country profile carries no carrier identifier types; refresh it.",
        ]

    problems = []
    id_type = _text(given.get("id_type")) or "tax"
    if id_type not in accepted:
        problems.append(
            f"efis nuk e pranon llojin e identifikuesit {id_type} për transportuesin. "
            f"/ efis does not accept the carrier identifier type {id_type}.")
    number = _text(given.get("id_number"))
    if not number:
        problems.append(
            "Transportuesi ka nevojë për numër identifikimi. / The carrier needs an identification number.")
    name = _text(given.get("name"))
    if not name:
        problems.append("Transportuesi ka nevojë për emër. / The carrier needs a name.")
    if problems:
        return None, problems

    # The number as efis compares one: without spaces, and a tax number upper case and without a
    # country prefix in front of it, as the receiver's is.
    compact = "".join(number.split())
    body = {
        "idType": id_type,
        "idNumber": efis_inbound.tax_id(compact) if id_type == "tax" else compact,
        "name": name,
    }
    for key, value in (("address", _text(given.get("address"))), ("town", _text(given.get("town")))):
        if value is not None:
            body[key] = value
    return body, []


def _place(place, kinds, label):
    place = place or {}
    body = {"address": _text(place.get("address")), "city": _text(place.get("city")), "kind": _text(place.get("kind"))}
    problems = []
    if not body["address"] or not body["city"]:
        problems.append(f"{label}: mungon adresa ose qyteti. / the address or the city is missing.")
    if body["kind"] not in kinds:
        problems.append(f"{label}: zgjidhni llojin e vendit. / choose the kind of place.")
    return body, problems


# efis's answer (TransportDocumentResponse.state) as the transfer shows it.
QUEUED, DEFERRED, ACCEPTED, REJECTED = "queued", "deferred", "accepted", "rejected"
ODOO_STATES = {QUEUED: "sent", DEFERRED: "deferred", ACCEPTED: "registered", REJECTED: "rejected"}


def answer_values(body):
    """The transfer's fields from efis's TransportDocumentResponse."""
    return {
        "metanow_fiscal_wtn_document_id": body.get("id"),
        "metanow_fiscal_wtn_number": body.get("number"),
        "metanow_fiscal_wtn_security_code": body.get("securityCode"),
        "metanow_fiscal_wtn_registration_code": body.get("registrationCode") or False,
        "metanow_fiscal_wtn_state": ODOO_STATES.get((body.get("state") or "").lower(), "sent"),
    }
