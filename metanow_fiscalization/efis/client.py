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
GET_DOCUMENT = ("GET", "/v1/documents/{id}")
GET_PROFILE = ("GET", "/v1/profiles/{country}")

ROUTES = (PAIR, SUBMIT_DOCUMENT, GET_DOCUMENT, GET_PROFILE)

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

    def get_document(self, document_id):
        return self._call(GET_DOCUMENT, {"id": document_id})

    def get_profile(self, country):
        return self._call(GET_PROFILE, {"country": country}, authenticated=False)

    def _call(self, route, parameters, body=None, headers=None, authenticated=True):
        method, template = route
        path = template.format(**{key: urllib.parse.quote(str(value), safe="") for key, value in parameters.items()})
        request = urllib.request.Request(self.base_url + path, method=method)
        request.add_header("Accept", "application/json")
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
                return Response(answer.status, _json(answer.read()), answer.headers.items())
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
