"""DEV-2443: an invoice's fiscal values, found again in efis after the module was reinstalled.

Uninstalling the module removes the fields it added to account.move, and with them every value
efis returned. efis keeps every document, so the values are read back from GET /v1/documents and
matched to the Odoo invoices that lost them. Nothing is sent: a restore only reads.

A document is matched to an invoice only when the match is certain: the same reference (efis's
clientReference is the Odoo invoice number, payload.build), the same total with two decimals and
its sign, the same currency and the same buyer identifier. Several Odoo databases can share one
efis business, and invoice numbers repeat across them, which is why the reference alone is not
enough. Anything that is not exactly one document for one invoice is left alone and listed, with
the reason, for a person to look at. The values go through outcome.document_values like a polled
answer, so a stored value is never replaced.
"""

from . import money
from . import outcome
from .payload import NATIONAL_CURRENCY

# Why an invoice was not restored. Each reason is a code here and a sentence in REASONS.
NO_MATCH = "no_match"
SEVERAL = "several"
TOTAL_DIFFERS = "total_differs"
CURRENCY_DIFFERS = "currency_differs"
BUYER_DIFFERS = "buyer_differs"
NO_TOTAL = "no_total"
TAKEN = "taken"
EINVOICE_UNREAD = "einvoice_unread"
CONFLICT = "conflict"

REASONS = {
    NO_MATCH: "efis nuk ka asnjë dokument me këtë numër fature. Nëse fatura duhej fiskalizuar, kontrollojeni në efis. "
              "/ efis has no document with this invoice number. If the invoice should have been fiscalised, check it in efis.",
    SEVERAL: "efis ka më shumë se një dokument që përputhet me këtë faturë; asnjë nuk u zgjodh. "
             "/ efis has more than one document that matches this invoice; none was chosen.",
    TOTAL_DIFFERS: "efis ka një dokument me këtë numër, por me total tjetër. "
                   "/ efis has a document with this number, but with a different total.",
    CURRENCY_DIFFERS: "efis ka një dokument me këtë numër, por në monedhë tjetër. "
                      "/ efis has a document with this number, but in a different currency.",
    BUYER_DIFFERS: "efis ka një dokument me këtë numër, por për një blerës tjetër. "
                   "/ efis has a document with this number, but for a different buyer.",
    NO_TOTAL: "efis ka një dokument me këtë numër pa total, prandaj nuk mund të krahasohet. "
              "/ efis has a document with this number but no total, so it cannot be compared.",
    TAKEN: "Dokumenti në efis që përputhet është lidhur tashmë me një faturë tjetër. "
           "/ The matching document in efis is already linked to another invoice.",
    EINVOICE_UNREAD: "efis nuk dha gjendjen e faturës elektronike; provoni përsëri. "
                     "/ efis did not return the e-invoice state; try again.",
    CONFLICT: "Një vlerë e ruajtur ndryshon nga ajo e efis; vlera e ruajtur u mbajt. "
              "/ A stored value differs from efis's; the stored value was kept.",
}


def expected(invoice):
    """What efis's list shows for this invoice, if efis holds it.

    invoice: {"key", "reference", "amount_total", "credit_note", "currency", "buyer_identifier"}.
    The total is signed as payload.build sends it: a credit note's is negative. A buyer
    identifier is compared without spaces, as payload.build sends it.
    """
    total = money.cents(invoice["amount_total"])
    if invoice.get("credit_note"):
        total = -total
    return {
        "reference": (invoice.get("reference") or "").strip(),
        "total": money.format_money(total),
        "currency": invoice.get("currency") or NATIONAL_CURRENCY,
        "buyer": _identifier(invoice.get("buyer_identifier")),
    }


def _identifier(value):
    return "".join(str(value or "").split())


def _difference(wanted, document):
    """Why a document with the right reference is not this invoice, or None if it is."""
    total = document.get("total")
    if total in (None, ""):
        return NO_TOTAL
    if str(total) != wanted["total"]:
        return TOTAL_DIFFERS
    if (document.get("currency") or NATIONAL_CURRENCY) != wanted["currency"]:
        return CURRENCY_DIFFERS
    buyer = document.get("buyer") or {}
    if _identifier(buyer.get("identifier")) != wanted["buyer"]:
        return BUYER_DIFFERS
    return None


def match(invoices, documents, linked=()):
    """Pair each invoice with the one efis document that is certainly it.

    invoices: plain values (see expected), each with a "key" the caller uses to find it again.
    documents: DocumentListItem bodies from every page of GET /v1/documents.
    linked: efis document ids already stored on an Odoo invoice; never matched again.
    Returns (matches, skipped): matches is [(key, document)], skipped is [(key, reason)].
    """
    linked = set(linked)
    by_reference = {}
    for document in documents:
        reference = (document.get("clientReference") or "").strip()
        if reference and document.get("id"):
            by_reference.setdefault(reference, []).append(document)

    chosen = {}
    skipped = []
    for invoice in invoices:
        wanted = expected(invoice)
        candidates = by_reference.get(wanted["reference"], [])
        if not candidates:
            skipped.append((invoice["key"], NO_MATCH))
            continue
        differences = [_difference(wanted, document) for document in candidates]
        agreeing = [document for document, difference in zip(candidates, differences) if difference is None]
        if len(agreeing) > 1:
            skipped.append((invoice["key"], SEVERAL))
        elif not agreeing:
            # One document: say what differs. Several, none agreeing: the first one's reason.
            skipped.append((invoice["key"], differences[0]))
        elif agreeing[0]["id"] in linked:
            skipped.append((invoice["key"], TAKEN))
        else:
            chosen.setdefault(agreeing[0]["id"], []).append((invoice["key"], agreeing[0]))

    matches = []
    for pairs in chosen.values():
        if len(pairs) == 1:
            matches.append(pairs[0])
        else:
            # Two Odoo invoices claim one document: neither is certainly it.
            skipped.extend((key, SEVERAL) for key, _ in pairs)
    return matches, skipped


def pages(read_page, limit=500):
    """Every document efis holds, read page by page with efis's cursor.

    read_page(after, limit) returns the page's body (DocumentListResponse with next), or raises.
    Each document comes once: efis returns every document exactly once across the pages.
    """
    documents = []
    after = None
    seen = set()
    while True:
        body = read_page(after, limit) or {}
        documents.extend(body.get("documents") or [])
        after = body.get("next")
        if not after or after in seen:
            return documents
        seen.add(after)


def note(values):
    """The chatter note's text: what efis returned for the invoice, readable without the module.

    values: the invoice's stored fiscal values, keyed by Odoo field name (outcome's names).
    """
    rows = (
        ("Numri fiskal / Fiscal number", outcome.FISCAL_NUMBER),
        ("Kodi i sigurisë së lëshuesit / Issuer security code", outcome.SECURITY_CODE),
        ("Kodi i regjistrimit / Registration code", outcome.REGISTRATION_CODE),
        ("Lëshuar më / Issued at", outcome.ISSUED_AT),
        ("Adresa e verifikimit / Verification URL", outcome.VERIFICATION_URL),
        ("Dokumenti në efis / efis document", outcome.DOCUMENT_ID),
    )
    return "\n".join(f"{label}: {values.get(field)}" for label, field in rows if values.get(field))
