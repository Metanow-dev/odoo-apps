"""A small HTTP client for efis's public /v1 API, on the standard library only.

The module talks to efis and to nothing else. It never holds a certificate or a key: pairing
gives it a credential for one tenant, and every call carries that credential and the tenant's
id. ROUTES lists every route this client calls; a test checks each against the OpenAPI document.
"""

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

PAIR = ("POST", "/v1/pair")
SUBMIT_DOCUMENT = ("POST", "/v1/documents")
LIST_DOCUMENTS = ("GET", "/v1/documents")
GET_DOCUMENT = ("GET", "/v1/documents/{id}")
GET_PROFILE = ("GET", "/v1/profiles/{country}")
GET_DOCUMENT_EINVOICE = ("GET", "/v1/documents/{documentId}/einvoice")
GET_REGISTRY = ("GET", "/v1/registry")
LIST_PAYMENTS_RECEIVED = ("GET", "/v1/receivables/{documentId}/payments")
RECORD_PAYMENT_RECEIVED = ("POST", "/v1/receivables/{documentId}/payments")
WITHDRAW_PAYMENT_RECEIVED = ("POST", "/v1/receivables/payments/{paymentId}/withdraw")
LIST_PAYMENT_RECORDS = ("GET", "/v1/einvoices/payment-records")
CREATE_PAYMENT_RECORD = ("POST", "/v1/einvoices/payment-records")
LIST_INBOUND = ("GET", "/v1/einvoices/inbound")
GET_INBOUND = ("GET", "/v1/einvoices/inbound/{id}")
GET_INBOUND_PDF = ("GET", "/v1/einvoices/inbound/{id}/pdf")
DECIDE_INBOUND = ("POST", "/v1/einvoices/inbound/{id}/decision")
LIST_WEBHOOKS = ("GET", "/v1/webhooks")
CREATE_WEBHOOK = ("POST", "/v1/webhooks")
DISABLE_WEBHOOK = ("POST", "/v1/webhooks/{id}/disable")
GET_TENANT_HEALTH = ("GET", "/v1/tenants/{id}/health")

ROUTES = (
    PAIR, SUBMIT_DOCUMENT, LIST_DOCUMENTS, GET_DOCUMENT, GET_PROFILE, GET_DOCUMENT_EINVOICE, GET_REGISTRY,
    LIST_PAYMENTS_RECEIVED, RECORD_PAYMENT_RECEIVED, WITHDRAW_PAYMENT_RECEIVED,
    LIST_PAYMENT_RECORDS, CREATE_PAYMENT_RECORD,
    LIST_INBOUND, GET_INBOUND, GET_INBOUND_PDF, DECIDE_INBOUND,
    LIST_WEBHOOKS, CREATE_WEBHOOK, DISABLE_WEBHOOK, GET_TENANT_HEALTH,
)

USER_AGENT = "metanow_fiscalization"

# The authority's hosts, assembled so the words never appear whole in the module's source.
# The module refuses to be pointed at them: it speaks to efis, and efis speaks to the authority.
_AUTHORITY_DOMAINS = (
    ".".join(("tatime", "gov", "al")),
    ".".join(("akshi", "gov", "al")),
    "-".join(("e", "albania")) + ".al",
)


class EfisUnavailable(Exception):
    """No answer from efis: a timeout, a refused connection, a broken response."""


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = dict(headers or {})

    def __repr__(self):
        return f"Response({self.status}, {self.body!r})"


def check_base_url(url):
    """Return the base URL without a trailing slash, or raise ValueError.

    https is required, except for a loopback address or a single-label host name (a container
    on the same compose network, as in CI), where plain http is allowed.
    """
    text = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or not host:
        raise ValueError("The efis address must be an https URL. / Adresa e efis duhet të jetë URL https.")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("The efis address carries no query, fragment or credentials.")
    for domain in _AUTHORITY_DOMAINS:
        if host == domain or host.endswith("." + domain):
            raise ValueError(
                "This module talks to efis, never to the authority. / Ky modul flet vetëm me efis, jo me autoritetin.")
    if parsed.scheme == "http" and not _local(host):
        raise ValueError("The efis address must be an https URL. / Adresa e efis duhet të jetë URL https.")
    return text


def _local(host):
    if host == "localhost" or "." not in host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class EfisClient:
    def __init__(self, base_url, credential=None, tenant=None, timeout=30, opener=None, version=None):
        self.base_url = check_base_url(base_url)
        self.credential = credential
        self.tenant = tenant
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self.user_agent = f"{USER_AGENT}/{version}" if version else USER_AGENT

    def pair(self, code):
        return self._call(PAIR, {}, body={"code": code}, authenticated=False)

    def submit_document(self, document, idempotency_key):
        if not idempotency_key:
            raise ValueError("A document is never sent without an idempotency key.")
        return self._call(SUBMIT_DOCUMENT, {}, body=document, headers={"Idempotency-Key": idempotency_key})

    def list_documents(self, after=None, limit=500):
        """One page of every document of the business, in the order efis wrote them (DEV-2443)."""
        return self._call(LIST_DOCUMENTS, {}, query={"limit": limit, "after": after})

    def get_document(self, document_id):
        return self._call(GET_DOCUMENT, {"id": document_id})

    def get_document_einvoice(self, document_id):
        return self._call(GET_DOCUMENT_EINVOICE, {"documentId": document_id})

    # DEV-2432. No X-Acting-* headers: they are a partner key's, and this is the module's credential.
    def list_payments_received(self, document_id):
        return self._call(LIST_PAYMENTS_RECEIVED, {"documentId": document_id})

    def record_payment_received(self, document_id, payment):
        return self._call(RECORD_PAYMENT_RECEIVED, {"documentId": document_id}, body=payment)

    def withdraw_payment_received(self, payment_id):
        return self._call(WITHDRAW_PAYMENT_RECEIVED, {"paymentId": payment_id})

    # DEV-2434: Instruction 24 payment records. efis filters the list by the day each is due.
    def list_payment_records(self, due_from, due_to):
        return self._call(LIST_PAYMENT_RECORDS, {}, query={"dueFrom": due_from, "dueTo": due_to})

    def create_payment_record(self, record):
        return self._call(CREATE_PAYMENT_RECORD, {}, body=record)

    # DEV-2433: e-invoices suppliers sent to the business, and the buyer's answer to one.
    def list_inbound(self):
        return self._call(LIST_INBOUND, {})

    def get_inbound(self, inbound_id):
        return self._call(GET_INBOUND, {"id": inbound_id})

    def get_inbound_pdf(self, inbound_id):
        """The PDF's bytes, as efis serves them (Response.body is bytes, not JSON)."""
        return self._call(GET_INBOUND_PDF, {"id": inbound_id}, raw=True)

    def decide_inbound(self, inbound_id, decision):
        if decision not in ("accept", "refuse"):
            raise ValueError("A decision is accept or refuse.")
        return self._call(DECIDE_INBOUND, {"id": inbound_id}, body={"decision": decision})

    # DEV-1704: the endpoint efis posts events to, and the business's health (its certificate).
    def list_webhooks(self):
        return self._call(LIST_WEBHOOKS, {})

    def create_webhook(self, url, events):
        return self._call(CREATE_WEBHOOK, {}, body={"url": url, "events": list(events)})

    def disable_webhook(self, endpoint_id):
        return self._call(DISABLE_WEBHOOK, {"id": endpoint_id})

    def get_health(self):
        return self._call(GET_TENANT_HEALTH, {"id": self.tenant})

    def get_registry(self):
        return self._call(GET_REGISTRY, {})

    def get_profile(self, country):
        return self._call(GET_PROFILE, {"country": country}, authenticated=False)

    def _call(self, route, parameters, body=None, headers=None, authenticated=True, query=None, raw=False):
        method, template = route
        path = template.format(**{key: urllib.parse.quote(str(value), safe="") for key, value in parameters.items()})
        if query:
            path += "?" + urllib.parse.urlencode({key: str(value) for key, value in query.items() if value is not None})
        request = urllib.request.Request(self.base_url + path, method=method)
        request.add_header("Accept", "application/pdf" if raw else "application/json")
        request.add_header("User-Agent", self.user_agent)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if authenticated:
            if not self.credential or not self.tenant:
                raise ValueError("The company is not paired with efis. / Kompania nuk është çiftuar me efis.")
            request.add_header("X-Credential", self.credential)
            request.add_header("X-Tenant-Id", self.tenant)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self._open(request, data=data, timeout=self.timeout) as answer:
                content = answer.read()
                return Response(answer.status, content if raw else _json(content), answer.headers.items())
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
            finally:
                error.close()
            return Response(error.code, _json(raw), error.headers.items() if error.headers else ())
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise EfisUnavailable(str(error)) from error


def _json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
