"""The printed QR: Odoo's own barcode route, error correction M.

BUILD_SPEC 10.4: /report/barcode is auth='public', so the image renders in an emailed or
portal PDF too, and barLevel must be at least M (Odoo's default is L). The value is efis's
verification URL exactly as efis returned it; this module never builds that URL.
"""

import urllib.parse

ERROR_CORRECTION = "M"

# Pixels of the rendered PNG. A presentation choice, not a legal value.
SIZE = 200


def barcode_src(verification_url):
    if not verification_url:
        return False
    query = urllib.parse.urlencode({
        "barcode_type": "QR",
        "value": verification_url,
        "width": SIZE,
        "height": SIZE,
        "barLevel": ERROR_CORRECTION,
    })
    return "/report/barcode/?" + query
