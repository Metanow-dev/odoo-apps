"""Instruction 24 payment records for an e-invoice's cash payments (DEV-2434).

Law 87/2019 Art. 37(8)-(10) and Udhëzim 24: the issuer records on the central platform any
payment of an e-invoice that no bank reports, by the 10th of the following month. Cash is never
reported by a bank, so a cash payment on an e-invoice is recorded with the cause
bank_not_reported (Gleni, 11 Nov 2026). efis keeps the record (POST /v1/einvoices/payment-records),
and a person files it on the platform and marks it filed in efis.

efis's route has no idempotency key, no reference of ours and no withdrawal: a record, once made,
stays. So each pass reads the records efis holds for the invoice and makes only those Odoo's cash
payments call for that are not there yet. Payments are matched by amount and day, as many times
as each occurs. A record whose payment Odoo no longer has (a cash payment cancelled after it was
recorded) cannot be taken back; it is named, so nobody files it.
"""

from collections import Counter
from decimal import Decimal

CAUSE = "bank_not_reported"


def _key(record):
    return Decimal(str(record["amount"])), record["paidOn"]


def plan(wanted, held):
    """The records to make, and the held records Odoo no longer has a payment for.

    wanted: [{"amount": "120.00", "paidOn": "2026-11-10"}], Odoo's cash payments on the invoice.
    held:   efis's records for the invoice with this cause (PaymentRecordView).
    """
    available = Counter(_key(record) for record in held)
    create = []
    for payment in sorted(wanted, key=lambda payment: (payment["paidOn"], Decimal(payment["amount"]))):
        key = _key(payment)
        if available[key] > 0:
            available[key] -= 1
        else:
            create.append(payment)
    called_for = Counter(_key(payment) for payment in wanted)
    extra = []
    for record in sorted(held, key=lambda record: (record["paidOn"], record.get("recordedAt") or "")):
        key = _key(record)
        if called_for[key] > 0:
            called_for[key] -= 1
        else:
            extra.append(record)
    return create, extra
