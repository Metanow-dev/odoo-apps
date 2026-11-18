"""DEV-2462: a received e-invoice linked to its vendor bill again after a reinstall.

Uninstalling the module removes the fields it added to account.move, and with them the link
between a vendor bill and the e-invoice a supplier sent (DEV-2433). The bills stay and are not
duplicated, but they no longer refresh from efis and cannot be accepted or refused from Odoo.
After re-pairing, the day the import starts from is today again, so the e-invoices received
earlier are never imported a second time either. efis keeps every received e-invoice, so the link
is made again from what Odoo still holds. Nothing is sent: a re-link only reads, and it never
accepts or refuses on its own.

A bill is linked only where the e-invoice is certainly it (decided 18 Nov 2026):

* the bill's chatter names the e-invoice's EIC. The module writes it there when it makes the bill,
  and an uninstall does not empty the chatter, so this one is certain by itself. A bill whose
  chatter names an EIC is linked to that e-invoice or to none.
* otherwise, exactly one e-invoice efis lists has the same seller tax number, the same document
  number (the bill's reference) and the same currency, and either its amount is the bill's total
  or the bill has no lines at all, which is how the module leaves a bill for a person to fill in
  from the PDF.

Anything else is left alone and listed with its reason: a supplier can reuse a document number
across years, and two of its invoices can carry one amount, so a number and an amount are not
certainty. A bill efis holds nothing like is not listed at all — most vendor bills are ordinary
purchases that never were an e-invoice, and a note on each of those would say nothing.
"""

import re

from . import money
from .inbound import tax_id
from .payload import NATIONAL_CURRENCY

# Why a bill was not linked. Each reason is a code here and a sentence in REASONS.
NO_MATCH = "no_match"
SEVERAL = "several"
TOTAL_DIFFERS = "total_differs"
CURRENCY_DIFFERS = "currency_differs"
TAKEN = "taken"

REASONS = {
    NO_MATCH: "efis nuk ka më një e-faturë të marrë me EIC-un e shënuar në këtë faturë blerje. "
              "/ efis no longer has a received e-invoice with the EIC noted on this vendor bill.",
    SEVERAL: "efis ka më shumë se një e-faturë të marrë që përputhet me këtë faturë blerje; asnjëra nuk u zgjodh. "
             "/ efis has more than one received e-invoice that matches this vendor bill; none was chosen.",
    TOTAL_DIFFERS: "efis ka një e-faturë të marrë me këtë numër dokumenti, por me shumë tjetër. "
                   "/ efis has a received e-invoice with this document number, but with a different amount.",
    CURRENCY_DIFFERS: "efis ka një e-faturë të marrë me këtë numër dokumenti, por në monedhë tjetër. "
                      "/ efis has a received e-invoice with this document number, but in a different currency.",
    TAKEN: "E-fatura e marrë që përputhet është lidhur tashmë me një faturë blerje tjetër. "
           "/ The matching received e-invoice is already linked to another vendor bill.",
}

# The chatter line the EIC is written on and read back from. "EIC:" is the whole marker: it is
# ASCII, it is not translated, and plaintext2html leaves it as it is.
EIC_MARK = "EIC:"
_EIC_IN_NOTE = re.compile(r"(?:^|[\s>])EIC:\s*([^\s<&]+)")


def note(heading, values):
    """The chatter note that keeps a received e-invoice's identity without the module's fields.

    heading: the sentence that says why the note is there.
    values: the e-invoice as inbound.values read it.
    """
    rows = (
        ("EIC", values.get("eic")),
        ("Numri tatimor i shitësit / Seller's tax number", values.get("seller")),
        ("Numri i dokumentit / Document number", values.get("number")),
        ("Shuma sipas autoritetit / Amount the authority holds", values.get("amount")),
        ("E-fatura e marrë në efis / Received e-invoice in efis", values.get("id")),
    )
    lines = [heading] + [f"{label}: {value}" for label, value in rows if value]
    return "\n".join(lines)


def eic_in(body):
    """The EIC a chatter note names, read from its text or from the HTML Odoo stored it as."""
    found = _EIC_IN_NOTE.search(body or "")
    return found.group(1) if found else None


def _amount(text):
    """An amount as it is compared: two decimals, as efis and money both write them."""
    if text in (None, ""):
        return None
    try:
        return money.format_money(money.to_decimal(text))
    except (TypeError, ValueError):  # NotExact is a ValueError: an amount that is not whole cents.
        return None


def expected(bill):
    """What efis's list shows for this bill, if efis holds its e-invoice.

    bill: {"key", "eic", "seller", "number", "amount_total", "has_lines", "currency"}. The EIC is
    the one the bill's chatter names, or None. A tax number is compared as inbound.tax_id writes
    it, so a country prefix or a space never decides a link.
    """
    return {
        "eic": (bill.get("eic") or "").strip(),
        "seller": tax_id(bill.get("seller")),
        "number": (bill.get("number") or "").strip(),
        "total": _amount(bill.get("amount_total")),
        "has_lines": bool(bill.get("has_lines")),
        "currency": bill.get("currency") or NATIONAL_CURRENCY,
    }


def _difference(wanted, view):
    """Why an e-invoice with the right seller and number is not this bill, or None if it is."""
    if (view.get("currency") or NATIONAL_CURRENCY) != wanted["currency"]:
        return CURRENCY_DIFFERS
    # A bill the module made carries no lines until a person enters them from the PDF, so it has
    # no total to compare. One that does have lines must agree with the authority to the cent.
    if wanted["has_lines"] and _amount(view.get("amount")) != wanted["total"]:
        return TOTAL_DIFFERS
    return None


def match(bills, views, linked=()):
    """Pair each bill with the one received e-invoice that is certainly it.

    bills: plain values (see expected), each with a "key" the caller uses to find it again.
    views: EinvoiceInboundView bodies from GET /v1/einvoices/inbound.
    linked: efis e-invoice ids already stored on a vendor bill; never matched again.
    Returns (matches, skipped): matches is [(key, view)], skipped is [(key, reason)].
    """
    linked = set(linked)
    by_eic = {}
    by_document = {}
    for view in views:
        if not view.get("id"):
            continue
        eic = (view.get("eic") or "").strip()
        if eic:
            by_eic.setdefault(eic, []).append(view)
        document = (tax_id(view.get("sellerTaxId")), (view.get("docNumber") or "").strip())
        if all(document):
            by_document.setdefault(document, []).append(view)

    chosen = {}
    skipped = []
    for bill in bills:
        wanted = expected(bill)
        if wanted["eic"]:
            # The chatter names the e-invoice: that one or none, whatever else matches.
            candidates = by_eic.get(wanted["eic"], [])
            if len(candidates) != 1:
                skipped.append((bill["key"], SEVERAL if candidates else NO_MATCH))
                continue
            agreeing = candidates
        else:
            candidates = by_document.get((wanted["seller"], wanted["number"]), [])
            if not candidates:
                continue  # efis holds nothing like this bill; there is nothing to say about it.
            differences = [_difference(wanted, view) for view in candidates]
            agreeing = [view for view, difference in zip(candidates, differences) if difference is None]
            if len(agreeing) > 1:
                skipped.append((bill["key"], SEVERAL))
                continue
            if not agreeing:
                # One e-invoice: say what differs. Several, none agreeing: the first one's reason.
                skipped.append((bill["key"], differences[0]))
                continue
        if agreeing[0]["id"] in linked:
            skipped.append((bill["key"], TAKEN))
        else:
            chosen.setdefault(agreeing[0]["id"], []).append((bill["key"], agreeing[0]))

    matches = []
    for pairs in chosen.values():
        if len(pairs) == 1:
            matches.append(pairs[0])
        else:
            # Two bills claim one e-invoice: neither is certainly it.
            skipped.extend((key, SEVERAL) for key, _ in pairs)
    return matches, skipped
