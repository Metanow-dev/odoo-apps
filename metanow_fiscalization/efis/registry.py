"""The codes efis has confirmed for the business: business units, operators, devices, software.

DEV-2436. Read from efis's GET /v1/registry and kept on the company, so Odoo offers only these
codes to choose from, and refuses before anything is sent an invoice that names any other. efis
is the source. Nothing here is a list of codes written into the module.
"""

import json

BUSINESS_UNIT = "businessUnits"
OPERATOR = "operators"
DEVICE = "devices"
SOFTWARE = "software"
KINDS = (BUSINESS_UNIT, OPERATOR, DEVICE, SOFTWARE)

# The request body fields that carry a registered code, and the kind each must be.
BODY_FIELDS = (
    ("businessUnitCode", BUSINESS_UNIT),
    ("operatorCode", OPERATOR),
    ("softwareCode", SOFTWARE),
    ("deviceCode", DEVICE),
)

LABELS = {
    BUSINESS_UNIT: "Kodi i njësisë së biznesit / Business unit code",
    OPERATOR: "Kodi i operatorit / Operator code",
    DEVICE: "Kodi i pajisjes / Device code",
    SOFTWARE: "Kodi i programit / Software code",
}


class Registry:
    def __init__(self, document):
        if not isinstance(document, dict):
            raise ValueError("the registry is not an object")
        self.codes = {}
        for kind in KINDS:
            values = document.get(kind)
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                raise ValueError(f"{kind} is not a list of codes")
            self.codes[kind] = tuple(sorted(set(values)))

    def confirms(self, kind, code):
        return bool(code) and code in self.codes[kind]

    def choices(self, kind):
        return [(code, code) for code in self.codes[kind]]

    def unconfirmed(self, body):
        """The problems with the codes a request body names, one line each. Empty when all are confirmed."""
        problems = []
        for name, kind in BODY_FIELDS:
            code = body.get(name)
            if code and not self.confirms(kind, code):
                problems.append(
                    f"{LABELS[kind]}: {code} nuk është konfirmuar në efis; zgjidhni një kod të konfirmuar. "
                    f"/ {code} is not confirmed in efis; choose a confirmed code.")
        return problems

    def to_json(self):
        return json.dumps(self.codes, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        """The stored registry, or None when the company has not read it from efis yet."""
        if not text:
            return None
        return cls(json.loads(text))
