"""Odoo's payments on an invoice, kept in step with efis's receivables (DEV-2432).

efis records a payment the buyer made (POST /v1/receivables/{documentId}/payments) and withdraws
one recorded in error, never deleting it. The record route has no idempotency key, so a resend
after a lost answer would count the payment twice. This module therefore never trusts its own
memory of what it sent: each pass reads what efis holds for the invoice, and compares it with
what Odoo's reconciliations call for. Every payment carries a reference made from the Odoo
record it comes from, so the comparison is exact, and a pass that stopped halfway is finished by
the next one.

Only payments whose reference this database made are ever withdrawn. A payment a person recorded
in efis itself is left alone.
"""

from datetime import date, timedelta
from decimal import Decimal

# efis's own limit (PaymentReceivedRecord.MaxReferenceLength).
MAX_REFERENCE_LENGTH = 100


def prefix(database):
    """The start of every reference this Odoo database gives a payment."""
    return f"odoo-{database}-"


def reference(database, kind, record_id):
    text = f"{prefix(database)}{kind}-{record_id}"
    if len(text) > MAX_REFERENCE_LENGTH:
        raise ValueError(f"the payment reference {text} is longer than efis keeps")
    return text


def _same(held, wanted):
    try:
        amount = Decimal(str(held.get("amount")))
    except ArithmeticError:
        return False
    return (amount == Decimal(wanted["amount"])
            and held.get("paidOn") == wanted["paidOn"]
            and (held.get("method") or "").upper() == wanted["method"])


def plan(wanted, listing, database, today):
    """What one pass does: the efis payments to withdraw, the ones to record, and whether any is
    still waiting for its date.

    wanted:  {reference: {"amount": "120.00", "paidOn": "2026-11-11", "method": "ACCOUNT"}}, from Odoo.
    listing: efis's payments for the invoice (PaymentReceivedView), withdrawn ones included.

    A payment held under a wanted reference with the same amount, date and method is kept. One
    that differs is withdrawn and recorded again. One under this database's prefix that Odoo no
    longer calls for (a payment unreconciled or cancelled) is withdrawn. efis refuses a payment
    dated after tomorrow, so such a payment waits.
    """
    mine = prefix(database)
    kept = set()
    withdraw = []
    for held in listing:
        held_reference = held.get("reference") or ""
        if held.get("withdrawnAt") or not held_reference.startswith(mine):
            continue
        want = wanted.get(held_reference)
        if want is not None and held_reference not in kept and _same(held, want):
            kept.add(held_reference)
        else:
            withdraw.append(held["id"])
    record = []
    waiting = False
    latest = today + timedelta(days=1)
    for wanted_reference in sorted(wanted):
        if wanted_reference in kept:
            continue
        want = wanted[wanted_reference]
        if date.fromisoformat(want["paidOn"]) > latest:
            waiting = True
            continue
        record.append(dict(want, reference=wanted_reference))
    return withdraw, record, waiting
