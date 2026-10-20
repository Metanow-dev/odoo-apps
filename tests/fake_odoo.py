"""A stand-in for the few pieces of Odoo the module imports, so its models load without a server.

It is the ORM boundary, mocked: a model instance is one record, write() records each call it
receives, and super()._post() and friends stand for what Odoo itself would do. The real ORM is
exercised by the odoo-install CI job (DEV-1731), not here.
"""

import sys
import types


class Field:
    default_value = False

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class Integer(Field):
    default_value = 0


class UserError(Exception):
    pass


class ValidationError(Exception):
    pass


class Cursor:
    def __init__(self):
        self.statements = []
        self.locked = set()
        self._last = None

    def execute(self, query, params=None):
        self.statements.append((query, params))
        self._last = params[0] if params else None

    def fetchone(self):
        if self._last in self.locked:
            return None
        return (self._last,)


class Trigger:
    def __init__(self):
        self.count = 0

    def _trigger(self):
        self.count += 1


class Parameters:
    def __init__(self, values):
        self.values = values

    def sudo(self):
        return self

    # Odoo 20's typed readers; it has no get_param, so a call to it fails here as it does there.
    def get_str(self, name, default=""):
        return self.values.get(name, default)


class Env:
    def __init__(self):
        self.cr = Cursor()
        self.refs = {}
        self.models = {"ir.config_parameter": Parameters({"database.uuid": "db-0001"})}
        self.search_results = []

    def __getitem__(self, name):
        return self.models[name]

    def ref(self, xml_id, raise_if_not_found=True):
        if xml_id not in self.refs and raise_if_not_found:
            raise ValueError(xml_id)
        return self.refs.get(xml_id)


class Model:
    """One record. Unset fields read as Odoo's False (0 for integers)."""

    _inherit = None

    def __init__(self, env=None, **values):
        for name in dir(type(self)):
            attribute = getattr(type(self), name, None)
            if isinstance(attribute, Field):
                object.__setattr__(self, name, attribute.default_value)
        self.env = env or Env()
        self.writes = []
        self.base_calls = []
        self.id = values.pop("id", 1)
        for name, value in values.items():
            setattr(self, name, value)

    def __iter__(self):
        yield self

    def __len__(self):
        return 1

    def __getitem__(self, name):
        return getattr(self, name)

    def ensure_one(self):
        return self

    def sudo(self):
        return self

    def invalidate_recordset(self, fnames=None):
        self.base_calls.append("invalidate_recordset")

    def write(self, vals):
        self.writes.append(dict(vals))
        for name, value in vals.items():
            setattr(self, name, value)
        return True

    def search(self, domain, order=None, limit=None):
        self.env.last_domain = domain
        return list(self.env.search_results)

    # What Odoo itself does, recorded rather than performed.
    def _post(self, soft=True):
        self.base_calls.append("_post")
        return self

    def button_draft(self):
        self.base_calls.append("button_draft")
        return True

    def button_cancel(self):
        self.base_calls.append("button_cancel")
        return True

    def unlink(self):
        self.base_calls.append("unlink")
        return True


class TransientModel(Model):
    pass


def _identity_decorator(*args, **kwargs):
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return args[0]

    def wrap(function):
        return function

    return wrap


def install():
    """Register the stand-in as 'odoo' unless it is already there."""
    if "odoo" in sys.modules and getattr(sys.modules["odoo"], "__fake__", False):
        return sys.modules["odoo"]
    odoo = types.ModuleType("odoo")
    odoo.__fake__ = True
    fields = types.ModuleType("odoo.fields")
    for name in ("Char", "Text", "Selection", "Boolean", "Many2one", "Datetime", "Float", "Monetary"):
        setattr(fields, name, type(name, (Field,), {}))
    fields.Integer = Integer
    models = types.ModuleType("odoo.models")
    models.Model = Model
    models.TransientModel = TransientModel
    api = types.ModuleType("odoo.api")
    api.model = _identity_decorator
    api.depends = _identity_decorator
    api.constrains = _identity_decorator
    exceptions = types.ModuleType("odoo.exceptions")
    exceptions.UserError = UserError
    exceptions.ValidationError = ValidationError
    odoo.fields = fields
    odoo.models = models
    odoo.api = api
    odoo.exceptions = exceptions
    sys.modules.update({
        "odoo": odoo,
        "odoo.fields": fields,
        "odoo.models": models,
        "odoo.api": api,
        "odoo.exceptions": exceptions,
    })
    return odoo
