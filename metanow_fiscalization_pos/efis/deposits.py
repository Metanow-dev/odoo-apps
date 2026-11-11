"""Odoo Point of Sale cash as efis's CashDepositRequest (DEV-2437).

The cash register (arka): the day's opening float (INITIAL), and cash put in (DEPOSIT) or taken
out (WITHDRAW), each per fiscal device and registered with the authority by efis. What the
authority refuses as the caller's fault is refused here first: a zero deposit or withdrawal, a
negative float, a device code missing, a change time without an offset or with milliseconds.
efis allows one INITIAL per device and day; the model decides which session's opening is it.
"""

import re

from odoo.addons.metanow_fiscalization.efis import money

INITIAL, DEPOSIT, WITHDRAW = "INITIAL", "DEPOSIT", "WITHDRAW"
OPERATIONS = (INITIAL, DEPOSIT, WITHDRAW)

# CLAUDE.md: an explicit numeric offset and whole seconds, never milliseconds.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


class DepositInvalid(ValueError):
    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = list(problems)


def build(operation, amount, device_code, changed_at):
    """Return the request body, or raise DepositInvalid with every problem found."""
    problems = []
    if operation not in OPERATIONS:
        problems.append(f"Veprimi {operation} nuk njihet. / The operation {operation} is not known.")
    device = str(device_code or "").strip()
    if not device:
        problems.append("Pika e shitjes nuk ka kod pajisjeje në efis. / The point of sale has no efis device code.")
    try:
        value = money.cents(amount)
    except (money.NotExact, TypeError):
        problems.append("Shuma nuk jepet saktë në qindarka. / The amount is not exact in cents.")
        value = None
    if value is not None:
        if value < 0:
            problems.append("Shuma nuk është negative; një tërheqje dërgohet si WITHDRAW. "
                            "/ The amount is never negative; a withdrawal goes as WITHDRAW.")
        elif value == 0 and operation in (DEPOSIT, WITHDRAW):
            problems.append("Një futje ose tërheqje lëviz para; autoriteti refuzon zeron. "
                            "/ A deposit or withdrawal moves some cash; the authority refuses zero.")
    if not changed_at or not _TIMESTAMP.match(str(changed_at)):
        problems.append(f"Koha {changed_at} nuk është e saktë. / The time {changed_at} is not exact.")
    if problems:
        raise DepositInvalid(problems)
    return {"operation": operation, "amount": money.format_money(value), "deviceCode": device, "changedAt": changed_at}


# efis's answer (CashDepositResponse.state) as Odoo shows it.
ODOO_STATES = {"queued": "sent", "deferred": "deferred", "accepted": "registered", "rejected": "rejected"}


def answer_values(body, prefix):
    """The record's fields from efis's CashDepositResponse, under the model's field prefix."""
    return {
        f"{prefix}_document_id": body.get("id"),
        f"{prefix}_state": ODOO_STATES.get((body.get("state") or "").lower(), "sent"),
        f"{prefix}_registration_code": body.get("registrationCode") or False,
    }
