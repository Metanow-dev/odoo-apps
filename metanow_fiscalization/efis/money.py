"""Decimal handling for amounts read from Odoo.

Odoo hands amounts over as floats. They are turned into Decimals once, here, and every amount
sent to efis is a string: money with exactly two decimals, quantities and unit prices with no
exponent and at most ten decimals. A float is never formatted with str() on the way out.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
TEN_PLACES = Decimal("1E-10")

# A float read back from a numeric column can differ from its cent value in the last binary
# digit. Anything further away than this is not a cent amount and is refused, never rounded.
_FLOAT_NOISE = Decimal("0.000001")


class NotExact(ValueError):
    """The value cannot be sent without changing it."""


def to_decimal(value):
    if isinstance(value, bool):
        raise TypeError("a boolean is not an amount")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # repr is the shortest string that round-trips, so 99.01 reads as 99.01.
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as error:
            raise NotExact(value) from error
    raise TypeError(f"not an amount: {value!r}")


def round_cents(amount):
    """Two decimals, half away from zero: efis's one rounding rule (LineAmounts.Round)."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def cents(value):
    """An Odoo monetary value as an exact cent amount, or NotExact."""
    amount = to_decimal(value)
    rounded = round_cents(amount)
    if abs(amount - rounded) > _FLOAT_NOISE:
        raise NotExact(f"{value!r} is not a whole number of cents")
    return rounded


def format_money(amount):
    """Exactly two decimals, never trimmed. Refuses an amount that is not whole cents."""
    amount = to_decimal(amount)
    if round_cents(amount) != amount:
        raise NotExact(f"{amount} is not a whole number of cents")
    return format(round_cents(amount), "f")


def fits_ten_places(amount):
    return amount == amount.quantize(TEN_PLACES, rounding=ROUND_HALF_UP)


def format_plain(amount):
    """A quantity or unit price: no exponent, no trailing zeros, at most ten decimals."""
    amount = to_decimal(amount)
    if not fits_ten_places(amount):
        raise NotExact(f"{amount} has more than ten decimals")
    text = format(amount.normalize(), "f")
    return "0" if text in ("-0", "") else text


def unit_price(line_amount, quantity):
    """The unit price that reproduces line_amount when efis multiplies it by quantity.

    Quantised to ten decimals. NotExact when no such price exists, so the line is refused in
    Odoo instead of fiscalised with a total the books do not carry.
    """
    if quantity == 0:
        raise NotExact("a line with no quantity has no unit price")
    price = (line_amount / quantity).quantize(TEN_PLACES, rounding=ROUND_HALF_UP)
    if round_cents(quantity * price) != line_amount:
        raise NotExact(f"{line_amount} is not {quantity} times a ten-decimal price")
    return price


def untaxed_from_total(total, rate_percent):
    """What efis derives as a line's untaxed amount (LineAmounts.FromPriceAfterTax)."""
    if rate_percent == 0:
        return total
    return round_cents(total / (Decimal(1) + rate_percent / Decimal(100)))
