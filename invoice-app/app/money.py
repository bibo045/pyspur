"""Decimal-safe money helpers. All amounts are Decimal, rounded to cents half-up."""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


class MoneyError(ValueError):
    pass


def to_decimal(value, field="amount", allow_negative=False):
    """Parse user input into a Decimal. Raises MoneyError on bad input."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise MoneyError(f"{field} is required.")
    try:
        d = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise MoneyError(f"{field} must be a number.")
    if not d.is_finite():
        raise MoneyError(f"{field} must be a finite number.")
    if not allow_negative and d < 0:
        raise MoneyError(f"{field} cannot be negative.")
    return d


def cents(value):
    """Round any Decimal-compatible value to cents, half-up."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def percent(value, field="percentage"):
    d = to_decimal(value, field)
    if d > 100:
        raise MoneyError(f"{field} cannot exceed 100.")
    return d


def fmt(value, currency="EUR"):
    """Human formatting, e.g. '1,234.50 EUR'. Used in UI and emails."""
    return f"{cents(value):,.2f} {currency}"


def dec_str(value):
    """Canonical string for storage in SQLite (TEXT column keeps exact decimals)."""
    return str(cents(value))
