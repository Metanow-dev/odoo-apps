"""efis's answer, as the one set of values Odoo writes back.

Every answer becomes exactly one dict, and the model layer passes it to exactly one write().
Each API call is its own transaction (docs/research/odoo_integration_surfaces.md), so three
writes would be three chances to leave an invoice with a security code and no state.

What is stored is what efis returned: its document id, the fiscal number, the security code,
the registration code and the verification URL. Raw request and response bytes are efis's to
keep, never this module's. A security code, once stored, is never replaced: an answer that
carries a different one is a conflict, raised, not written.
"""

# Odoo-side states. "accepted" is not "settled": the authority registering an invoice is the
# state "registered"; whether it is settled is efis's to say and is not mapped here.
TO_SEND = "to_send"
SENT = "sent"
REGISTERED = "registered"
DEFERRED = "deferred"
REJECTED = "rejected"
SUPERSEDED = "superseded"
REFUSED = "refused"
PARKED = "parked"

# Still to be read again from efis.
POLLED = (SENT, DEFERRED)

# A refused document was never numbered by efis, so the invoice may go back to draft.
RESETTABLE = (REFUSED,)

_EFIS_STATES = {
    "numbered": SENT,
    "signed": SENT,
    "submitted": SENT,
    "accepted": REGISTERED,
    "settled": REGISTERED,
    "deferred": DEFERRED,
    "rejected": REJECTED,
    "repudiated": REJECTED,
    "superseded": SUPERSEDED,
}

DOCUMENT_ID = "metanow_fiscal_document_id"
STATE = "metanow_fiscal_state"
EFIS_STATE = "metanow_fiscal_efis_state"
FISCAL_NUMBER = "metanow_fiscal_number"
SECURITY_CODE = "metanow_fiscal_security_code"
REGISTRATION_CODE = "metanow_fiscal_registration_code"
VERIFICATION_URL = "metanow_fiscal_verification_url"
ISSUED_AT = "metanow_fiscal_issued_at"
ERROR = "metanow_fiscal_error"

# Fields that, once they hold a value, never change.
WRITE_ONCE = (DOCUMENT_ID, FISCAL_NUMBER, SECURITY_CODE, REGISTRATION_CODE, ISSUED_AT)

# efis's DocumentResponse / DocumentDetailResponse property -> Odoo field.
_RESPONSE_FIELDS = (
    ("id", DOCUMENT_ID),
    ("fiscalNumber", FISCAL_NUMBER),
    ("securityCode", SECURITY_CODE),
    ("registrationCode", REGISTRATION_CODE),
    ("verificationUrl", VERIFICATION_URL),
    ("issuedAt", ISSUED_AT),
)

_RETRY_STATUSES = (429,)


class SecurityCodeConflict(Exception):
    """efis answered with a value different from one already stored on the invoice."""


def odoo_state(efis_state):
    return _EFIS_STATES.get((efis_state or "").strip().lower(), SENT)


def document_values(current, document):
    """One write's worth of values for an efis document, merged onto what is stored.

    current: the invoice's stored values, keyed by Odoo field name.
    document: efis's DocumentResponse or DocumentDetailResponse body.
    """
    if not isinstance(document, dict) or not document.get("id"):
        raise ValueError("efis answered without a document id")
    values = {}
    for prop, field in _RESPONSE_FIELDS:
        incoming = document.get(prop)
        stored = current.get(field) or None
        if incoming in (None, ""):
            continue
        incoming = str(incoming)
        if field in WRITE_ONCE and stored is not None:
            if stored != incoming:
                raise SecurityCodeConflict(
                    f"{field}: stored {stored!r}, efis answered {incoming!r}; the stored value is kept")
            continue
        if stored != incoming:
            values[field] = incoming
    efis_state = str(document.get("state") or "")
    values[EFIS_STATE] = efis_state
    values[STATE] = odoo_state(efis_state)
    values[ERROR] = False
    return values


def _error_text(response):
    body = response.body if isinstance(response.body, dict) else {}
    code = body.get("code") or f"http_{response.status}"
    detail = body.get("detail")
    correlation = body.get("correlationId")
    text = str(code)
    if detail:
        text += f": {detail}"
    if correlation:
        text += f" (correlation {correlation})"
    return text


def submission_values(current, response):
    """Values for one write after POST /v1/documents. None means: leave the invoice queued.

    A refusal (a 4xx other than 401, 403 and 429) is terminal: it is parked for correction and
    never retried, like a client fault. 401 and 403 park it until the company is paired again.
    No answer, 429, and a 5xx that efis does not mark as final are retried later with the same
    idempotency key and the same body, which efis answers with the same document.
    """
    if response is None:
        return None
    status = response.status
    if 200 <= status < 300:
        return document_values(current, response.body)
    body = response.body if isinstance(response.body, dict) else {}
    retryable = body.get("retryable")
    if status in (401, 403):
        return {STATE: PARKED, ERROR: _error_text(response)}
    if status in _RETRY_STATUSES or (status >= 500 and retryable is not False):
        return {ERROR: _error_text(response)}
    if 400 <= status < 500 and retryable is True:
        return {ERROR: _error_text(response)}
    if status >= 500:
        return {STATE: PARKED, ERROR: _error_text(response)}
    return {STATE: REFUSED, ERROR: _error_text(response)}


def poll_values(current, response):
    """Values for one write after GET /v1/documents/{id}, or None when nothing changed."""
    if response is None or not (200 <= response.status < 300):
        if response is not None and response.status in (401, 403):
            return {STATE: PARKED, ERROR: _error_text(response)}
        return None
    values = document_values(current, response.body)
    changed = {key: value for key, value in values.items() if current.get(key) != value}
    if not changed:
        return None
    # Write the whole set when anything changed, so the record never holds half an answer.
    return values
