from decimal import Decimal

import pytest

from app.money import MoneyError, cents, fmt, to_decimal, percent
from app.invoices import compute_line, compute_totals, vat_breakdown


def test_float_trap_is_avoided():
    # 0.1 + 0.2 is 0.30000000000000004 in float; Decimal must give exactly 0.30
    assert cents(to_decimal("0.1") + to_decimal("0.2")) == Decimal("0.30")


def test_rounding_is_half_up_to_cents():
    assert cents(Decimal("2.345")) == Decimal("2.35")
    assert cents(Decimal("2.344")) == Decimal("2.34")


def test_line_with_discount_and_vat():
    line = compute_line({"quantity": "3", "unit_price": "33.33", "discount_pct": "10", "vat_rate": "21"})
    assert line["line_net"] == "89.99"        # 99.99 gross - 10% (10.00 rounded from 9.999)
    assert line["line_discount"] == "10.00"
    assert line["line_vat"] == "18.90"        # 89.99 * 21% = 18.8979 -> 18.90
    assert line["line_total"] == "108.89"


def test_totals_sum_from_lines_not_from_invoice_level_rounding():
    lines = [compute_line({"quantity": "1", "unit_price": "0.10", "vat_rate": "21"}) for _ in range(3)]
    totals = compute_totals(lines)
    assert totals["subtotal"] == "0.30"
    assert totals["vat_total"] == "0.06"       # 3 * 0.021 rounded per line = 3 * 0.02
    assert totals["total"] == "0.36"


def test_vat_breakdown_groups_by_rate():
    lines = [compute_line({"quantity": "1", "unit_price": "100", "vat_rate": "21"}),
             compute_line({"quantity": "1", "unit_price": "50", "vat_rate": "0"}),
             compute_line({"quantity": "2", "unit_price": "10", "vat_rate": "21"})]
    groups = vat_breakdown(lines)
    assert [g["rate"] for g in groups] == ["0", "21"]
    assert groups[1]["net"] == "120.00"


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "Infinity", "-5"])
def test_bad_money_is_rejected(bad):
    with pytest.raises(MoneyError):
        to_decimal(bad, "Amount")


def test_quantity_must_be_positive():
    with pytest.raises(MoneyError):
        compute_line({"quantity": "0", "unit_price": "10", "vat_rate": "21"})


def test_percent_bounds():
    with pytest.raises(MoneyError):
        percent("101")


def test_format_uses_grouping_and_currency():
    assert fmt("1234.5", "EUR") == "1,234.50 EUR"
