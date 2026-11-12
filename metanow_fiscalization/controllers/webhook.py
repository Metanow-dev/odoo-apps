import logging

from odoo import http
from odoo.http import request

from ..efis import webhooks as efis_webhooks

_logger = logging.getLogger(__name__)


class EfisWebhook(http.Controller):
    """DEV-1704: where efis posts its events for one company.

    Public, because efis is not an Odoo user; the post's signature is what proves it came from
    efis. Nothing in the body is stored: a checked event only marks what to read again from
    efis, and the module's jobs do the reading. So the answer is quick, and a post that fails the
    check changes nothing.
    """

    @http.route("/metanow_fiscalization/efis/webhook/<int:company_id>", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def receive(self, company_id, **kwargs):
        body = request.httprequest.get_data(cache=False)
        company = request.env["res.company"].sudo().browse(company_id).exists()
        secret = company.metanow_fiscal_webhook_secret if company else None
        header = request.httprequest.headers.get(efis_webhooks.SIGNATURE_HEADER)
        if not secret or not efis_webhooks.verified(secret, header, body):
            _logger.warning("efis webhook for company %s refused: signature not verified", company_id)
            return request.make_response("", status=401)
        message = efis_webhooks.event(body)
        if message is None:
            return request.make_response("", status=400)
        company._metanow_fiscal_webhook_received(message)
        return request.make_response("", status=204)
