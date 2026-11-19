# DEV-2348: run inside `odoo shell` after metanow_fiscalization_pos is installed and the company
# is paired (ci/post_invoice.py ran first). Walks a till's cash through efis as a customer would:
# the day's opening float declared when the session is opened, cash put in and taken out while it
# is open, one float a day per device however many sessions open, and a declared movement that is
# never deleted (DEV-2437).
#
# Prints ODOO-POS-PASSED on success; odoo-install.sh fails the job without that line.
import os
import time

from odoo.exceptions import UserError

company = env.company  # noqa: F821 - provided by odoo shell
assert company.metanow_fiscal_credential, "the company is not paired with efis; post_invoice.py runs first"
device = os.environ["EFIS_DEVICE_CODE"]


def till(name):
    """A point of sale declaring its cash for the device efis confirmed."""
    config = env["pos.config"].create({"name": name, "company_id": company.id})  # noqa: F821
    config.metanow_fiscal_device_code = device
    assert config._metanow_fiscal_declares(), name
    return config


def followed(record, prefix, what):
    """Send what is queued and follow it until the authority has registered it."""
    env["pos.session"]._cron_metanow_fiscal_pos_send()  # noqa: F821
    env.cr.commit()  # noqa: F821
    record.invalidate_recordset()
    assert record[f"{prefix}_document_id"], (what, record[f"{prefix}_error"])
    deadline = time.time() + 180
    while time.time() < deadline and not record[f"{prefix}_registration_code"]:
        env["pos.session"]._cron_metanow_fiscal_pos_poll()  # noqa: F821
        env.cr.commit()  # noqa: F821
        record.invalidate_recordset()
        if record[f"{prefix}_state"] in ("rejected", "refused"):
            break
        time.sleep(3)
    assert record[f"{prefix}_state"] == "registered", (what, record[f"{prefix}_state"], record[f"{prefix}_error"])
    return record


# ------------------------------------------------------------------ the day's opening float

shop = till("Arka 1 / Till 1")
session = env["pos.session"].create({"config_id": shop.id, "user_id": env.uid})  # noqa: F821
assert session.state == "opening_control", session.state
session.set_opening_control(300.0, "")
env.cr.commit()  # noqa: F821
assert session.state == "opened", session.state
assert session.metanow_fiscal_initial_state == "to_send", session.metanow_fiscal_initial_error
assert '"amount": "300.00"' in session.metanow_fiscal_initial_request, session.metanow_fiscal_initial_request
assert session.metanow_fiscal_initial_day, "the Tirana day the float is for is not recorded"
followed(session, "metanow_fiscal_initial", "the opening float")

# efis takes one opening float a day for a device, so a second till on the same device declares
# none and says why. It is not an error, and nothing was sent.
second = till("Arka 2 / Till 2")
later = env["pos.session"].create({"config_id": second.id, "user_id": env.uid})  # noqa: F821
later.set_opening_control(50.0, "")
env.cr.commit()  # noqa: F821
assert later.metanow_fiscal_initial_state == "skipped", later.metanow_fiscal_initial_state
assert "deklaruar" in (later.metanow_fiscal_initial_error or ""), later.metanow_fiscal_initial_error
assert not later.metanow_fiscal_initial_document_id, "a second float reached efis"

# ------------------------------------------------------------------ cash in and out

lines = {}
for kind, amount, reason in (("in", 500.0, "Futje parash / Cash in"), ("out", 120.0, "Tërheqje / Cash out")):
    before = set(session.bank_statement_line_ids.ids)
    session.try_cash_in_out(kind, amount, reason, False)
    env.cr.commit()  # noqa: F821
    session.invalidate_recordset()
    line = session.bank_statement_line_ids.filtered(lambda row: row.id not in before)
    assert len(line) == 1, (kind, line)
    assert line.metanow_fiscal_deposit_state == "to_send", line.metanow_fiscal_deposit_error
    body = line.metanow_fiscal_deposit_request
    assert f'"amount": "{amount:.2f}"' in body, body
    assert f'"operation": "{"DEPOSIT" if kind == "in" else "WITHDRAW"}"' in body, body
    lines[kind] = followed(line, "metanow_fiscal_deposit", f"cash {kind}")

assert lines["in"].metanow_fiscal_deposit_document_id != lines["out"].metanow_fiscal_deposit_document_id

# What the authority holds is never deleted: the opposite movement is recorded instead.
try:
    session.delete_cash_in_out(lines["in"].id, False)
except UserError as error:
    assert "fshihet" in str(error) or "never deleted" in str(error), str(error)
else:
    raise AssertionError("a cash movement declared to the authority was deleted")
env.cr.rollback()  # noqa: F821

# The session shows the person what efis holds, movement by movement.
session.invalidate_recordset()
summary = session.metanow_fiscal_cash_summary or ""
assert "+500.00" in summary and "-120.00" in summary, summary

print("ODOO-POS-PASSED", session.metanow_fiscal_initial_registration_code)
