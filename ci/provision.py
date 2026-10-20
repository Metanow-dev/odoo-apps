#!/usr/bin/env python3
"""The efis side of the odoo-install job: everything a business owner does in the web app.

Provisions a fresh business, signs in its owner, confirms registry codes, uploads a generated
test certificate (to efis, never to Odoo), and issues a pairing code for the channel "odoo".
Prints one JSON line for odoo-install.sh: the code, the tax id and the registry codes.

It runs only inside the job's compose network, against the api service, whose worker sends to
the mock authority. It refuses any other base URL.
"""

import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

BASE = os.environ.get("EFIS_BASE_URL", "http://api:8080").rstrip("/")
if urllib.parse.urlparse(BASE).hostname not in ("api", "127.0.0.1", "localhost"):
    sys.exit("provision.py runs against the job's own efis api only.")

# The registry codes the mock accepts, in the shape BUILD_SPEC 4.7 records (bb123bb123). Test data.
CODES = {
    "business-unit": "bb123bb123",
    "operator": "oo123oo123",
    "software": "ss123ss123",
    "device": "cc123cc123",
}


def call(method, path, body=None, headers=None, expect=(200, 201)):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("content-type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            status, raw = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    if status not in expect:
        sys.exit(f"{method} {path} -> {status}: {raw[:500]!r}")
    return json.loads(raw) if raw else None


def session(email, password):
    answer = call("POST", "/v1/sessions", {"email": email, "password": password})
    return answer["session"] if "session" in answer else answer["token"]


def main():
    slug = "odoo-ci-" + uuid.uuid4().hex[:6]
    tax_id = "K" + str(uuid.uuid4().int)[:8] + "S"
    owner = (f"owner-{slug}@example.invalid", "owner-password-" + uuid.uuid4().hex)

    admin = session(os.environ["EFIS_ADMIN_EMAIL"], os.environ["EFIS_ADMIN_PASSWORD"])
    tenant = call("POST", "/v1/tenants",
                  {"slug": slug, "taxId": tax_id, "country": "AL", "numberingModel": "per_business_unit"},
                  {"X-Provisioning-Key": os.environ["EFIS_PROVISIONING_KEY"]}, expect=(201,))
    tenant_id = tenant.get("id") or tenant.get("tenantId")
    call("POST", f"/v1/admin/businesses/{tenant_id}/people",
         {"email": owner[0], "name": "Owner", "password": owner[1], "role": "owner"}, {"X-Session": admin})
    headers = {"X-Session": session(*owner), "X-Tenant-Id": slug}
    for kind, code in CODES.items():
        call("POST", "/v1/registry", {"kind": kind, "code": code}, headers)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "odoo-ci " + tax_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    phrase = uuid.uuid4().hex.encode()
    bundle = pkcs12.serialize_key_and_certificates(
        b"odoo-ci", key, certificate, None, serialization.BestAvailableEncryption(phrase))
    call("POST", "/v1/certificates/pkcs12",
         {"pkcs12": base64.b64encode(bundle).decode(), "passphrase": phrase.decode(), "currentPassword": owner[1]},
         headers)

    pairing = call("POST", "/v1/pairing-codes", {"channel": "odoo"}, headers)
    print(json.dumps({
        "code": pairing["code"],
        "taxId": tax_id,
        "businessUnitCode": CODES["business-unit"],
        "operatorCode": CODES["operator"],
        "softwareCode": CODES["software"],
    }))


if __name__ == "__main__":
    main()
