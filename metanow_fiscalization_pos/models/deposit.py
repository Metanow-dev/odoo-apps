"""Sending one cash deposit to efis, shared by the session's opening float and its cash moves.

Each record keeps its efis fields under one prefix: <prefix>_state, _document_id,
_registration_code, _key, _request (the request as it was sent) and _error. A resend carries the
same body under the same key, so efis returns the same deposit, never a second one.
"""

import json
import logging
from datetime import datetime

import pytz

from odoo.exceptions import UserError

from odoo.addons.metanow_fiscalization.efis import client as efis_client

from ..efis import deposits

_logger = logging.getLogger(__name__)

TIRANA = pytz.timezone("Europe/Tirane")

STATES = [
    ("to_send", "Në radhë për efis / Queued for efis"),
    ("sent", "Dërguar te efis / Sent to efis"),
    ("deferred", "Shtyrë / Deferred"),
    ("registered", "Regjistruar / Registered"),
    ("rejected", "Refuzuar nga autoriteti / Rejected by the authority"),
    ("refused", "Refuzuar nga efis / Refused by efis"),
    ("skipped", "Pa dërguar / Not sent"),
]
# efis holds the deposit (or will): it is never sent again and never undone.
HELD = ("to_send", "sent", "deferred", "registered", "rejected")
POLLED = ("sent", "deferred")


def tirana_time(moment=None):
    """A moment in Tirana, to the second, with its offset (CLAUDE.md: explicit offset, no milliseconds)."""
    moment = moment or datetime.now(pytz.utc)
    if moment.tzinfo is None:  # Odoo keeps datetimes naive, in UTC.
        moment = pytz.utc.localize(moment)
    return moment.astimezone(TIRANA).replace(microsecond=0).isoformat()


def queue(record, prefix, operation, amount, device_code, changed_at, key):
    """Validate, then keep the request and its key on the record, for the send job."""
    try:
        body = deposits.build(operation, amount, device_code, changed_at)
    except deposits.DepositInvalid as error:
        raise UserError("efis nuk do ta pranonte këtë lëvizje parash. / efis would not accept this cash movement.\n"
                        + "\n".join(error.problems)) from error
    record.write({
        f"{prefix}_state": "to_send",
        f"{prefix}_key": key,
        f"{prefix}_request": json.dumps(body, sort_keys=True),
        f"{prefix}_error": False,
    })


def lock(record, table):
    record.env.cr.execute(f"SELECT id FROM {table} WHERE id = %s FOR UPDATE SKIP LOCKED", [record.id])
    if not record.env.cr.fetchone():
        return False
    record.invalidate_recordset()
    return True


def send(record, prefix, table, company):
    if not lock(record, table) or record[f"{prefix}_state"] != "to_send":
        return
    try:
        answer = company._metanow_fiscal_client().submit_cash_deposit(
            json.loads(record[f"{prefix}_request"]), record[f"{prefix}_key"])
    except efis_client.EfisUnavailable as error:
        record.write({f"{prefix}_error": f"efis_unavailable: {error}"})  # sent again on the next pass
        return
    except UserError as error:
        record.write({f"{prefix}_state": "refused", f"{prefix}_error": str(error)})
        return
    body = answer.body if isinstance(answer.body, dict) else {}
    if answer.status in (200, 202):
        record.write(dict(deposits.answer_values(body, prefix), **{f"{prefix}_error": False}))
        return
    code = body.get("code") or str(answer.status)
    if code == "initial_already_registered":
        # The device's opening float for the day is in efis already (another session, efis's
        # console or an edge agent declared it). Nothing more is owed.
        record.write({f"{prefix}_state": "skipped", f"{prefix}_error":
                      "efis e ka tashmë gjendjen fillestare të sotme për këtë pajisje. "
                      "/ efis already has today's opening float for this device."})
    elif answer.status >= 500 or answer.status == 429:
        record.write({f"{prefix}_error": f"efis: {code}"})
    else:
        # efis refused it as the request's fault: never resent as it is.
        record.write({f"{prefix}_state": "refused", f"{prefix}_error": f"efis: {code}"})


def follow(records, prefix, company):
    """Read efis's deposits once and bring the records' states up to date."""
    try:
        listing = company._metanow_fiscal_client().list_cash_deposits()
    except (efis_client.EfisUnavailable, UserError) as error:
        _logger.info("efis cash deposits not read for company %s: %s", company.id, error)
        return
    if listing.status != 200:
        return
    held = {item.get("id"): item for item in (listing.body or {}).get("deposits") or []}
    for record in records:
        item = held.get(record[f"{prefix}_document_id"])
        if not item:
            continue
        values = {name: value for name, value in deposits.answer_values(item, prefix).items()
                  if (record[name] or False) != (value or False)}
        if values:
            record.write(values)
