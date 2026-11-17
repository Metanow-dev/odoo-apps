"""DEV-1704: efis's webhooks, received by Odoo instead of waiting for the next poll.

efis posts an event to the URL the module registered on pairing. The module checks the post's
Efis-Signature with the secret efis returned when the endpoint was created, and refuses an old
timestamp, so a captured post cannot be replayed. It then trusts nothing in the body but the
event's type and the id of the object it names: every value is read again from efis with the
module's own credential, exactly as the poll reads it (decided 12 Nov 2026). The module checks
a signature; it never makes one.

efis posts only to a public https address (efis WebhookTargets), so an Odoo that efis cannot
reach keeps polling as before. Polling also stays on for an Odoo that is reachable, as the fallback.
"""

import hmac
import ipaddress
import json
import time
import urllib.parse

# The topics the module subscribes to, each a name efis's POST /v1/webhooks accepts.
DOCUMENT_TOPICS = (
    "document.fiscalised",
    "document.rejected",
    "document.deferred",
    "document.repudiated",
    "document.superseded",
    "document.corrected",
    "einvoice.refused_by_buyer",
)
INBOUND_TOPICS = ("einvoice.received", "einvoice.answer_due")
CERTIFICATE_EXPIRING = "certificate.expiring"
PROFILE_UPDATED = "profile.updated"
TOPICS = DOCUMENT_TOPICS + INBOUND_TOPICS + (CERTIFICATE_EXPIRING, PROFILE_UPDATED)

SIGNATURE_HEADER = "Efis-Signature"
# How old a signed post may be. efis signs each attempt when it sends it, so a retry carries a
# fresh timestamp.
TOLERANCE_SECONDS = 300

# The path efis posts to, one per Odoo company.
PATH = "/metanow_fiscalization/efis/webhook/{company}"


def _parts(header):
    parts = {}
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key and value:
            parts.setdefault(key, value)
    return parts


def verified(secret, header, body, now=None):
    """True when the post was signed by efis with this secret, recently.

    header: Efis-Signature, "t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>">".
    body: the request body exactly as received, in bytes.
    """
    if not secret or not header or not isinstance(body, (bytes, bytearray)):
        return False
    parts = _parts(header)
    stamp, given = parts.get("t"), parts.get("v1")
    if not stamp or not given or not stamp.isdigit():
        return False
    now = time.time() if now is None else now
    if abs(now - int(stamp)) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode("utf-8"), stamp.encode("ascii") + b"." + bytes(body), "sha256").hexdigest()
    return hmac.compare_digest(expected, given.lower())


def event(body):
    """The parts of a verified post the module acts on: its type and the id it names.
    Everything else in the body is efis's, and is read again from efis, not from here."""
    try:
        message = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        return None
    object_id = message.get("objectId")
    return {"type": message["type"], "object_id": str(object_id) if object_id else None}


def address(base_url, company_id):
    """The URL efis posts to for this company, or None when efis could not reach it.

    Mirrors efis's own check (WebhookTargets): https, a public host, no credentials.
    """
    text = (base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return None
    if host == "localhost" or host.endswith((".localhost", ".internal", ".local")):
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return None
    else:
        if not literal.is_global:
            return None
    return text + PATH.format(company=int(company_id))
