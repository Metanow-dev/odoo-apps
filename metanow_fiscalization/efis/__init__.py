"""The module's conversation with efis, in efis's own words.

Nothing in this package imports Odoo. It turns plain values read from an invoice into the
request body efis publishes in spec/openapi/v1.json, sends it, and turns efis's answer into
the one set of values Odoo stores. It never signs, numbers or computes a security code: efis
does all three, and this package only carries what efis returns.
"""
