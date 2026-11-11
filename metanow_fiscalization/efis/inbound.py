"""E-invoices suppliers send to the business, as Odoo vendor bills (DEV-2433).

efis lists what the authority holds for the business as buyer (GET /v1/einvoices/inbound): the
seller's tax number, the document's number, the amount, the authority's due date and status, and
the day of the book review it is answered before (answerBy, the 11th of the following month). It
carries no lines and no seller name, so a draft bill gets only what efis states; the lines are
entered by a person from the PDF. Nothing here is guessed.
"""

import re
from datetime import date

ACCEPT = "accept"
REFUSE = "refuse"

# An Albanian tax number: a letter, eight digits, a letter (K12345678L). Odoo may keep it with
# the country prefix in front.
_PREFIXED_TAX_NUMBER = re.compile(r"^AL([A-Z][0-9]{8}[A-Z])$")


def tax_id(text):
    """A tax number as compared: no spaces, upper case, without an AL prefix in front of it."""
    compact = "".join(str(text or "").split()).upper()
    match = _PREFIXED_TAX_NUMBER.match(compact)
    return match.group(1) if match else compact


def _day(text):
    """The calendar day of an ISO date or date-time, as efis wrote it."""
    if not text:
        return None
    return date.fromisoformat(str(text)[:10])


def received_on(view):
    return _day(view.get("receivedAt"))


def decision_text(latest):
    """efis's latest decision on the e-invoice, as the bill shows it: "accept: queued"."""
    if not latest:
        return None
    return f"{latest.get('decision')}: {latest.get('state')}"


def values(view):
    """What a bill keeps from efis's view (EinvoiceInboundView), in plain values."""
    amount = str(view.get("amount") or "")
    currency = view.get("currency") or None
    return {
        "id": view["id"],
        "eic": view.get("eic"),
        "seller": tax_id(view.get("sellerTaxId")),
        "number": view.get("docNumber"),
        "amount": f"{amount} {currency}".strip() if currency else amount,
        "currency": currency,
        "due_on": _day(view.get("dueAt")),
        "answer_by": _day(view.get("answerBy")),
        "status": view.get("authorityStatus"),
        "waiting": bool(view.get("waiting")),
        "decision": decision_text(view.get("latestDecision")),
        "has_pdf": bool(view.get("hasPdf")),
        "fiscalised": bool(view.get("registrationCode")),
    }
