from decimal import Decimal

from runpeek.money import format_usd, nanos_to_usd, tokens_cost_nanos, usd_string


def test_fractional_cent_exact() -> None:
    # 7 tokens at $0.15 / M = $0.00000105 exactly = 1050 nanodollars
    assert tokens_cost_nanos(7, "0.15") == 1050
    assert usd_string(1050) == "0.000001050"
    assert nanos_to_usd(1050) == Decimal("0.00000105")


def test_one_token_smallest_list_rate() -> None:
    assert tokens_cost_nanos(1, "0.005") == 5  # gpt-5-nano cached input


def test_thousand_and_five_hundred() -> None:
    assert tokens_cost_nanos(1000, "0.40") + tokens_cost_nanos(500, "1.60") == 1_200_000
    assert usd_string(1_200_000) == "0.001200000"
    assert format_usd(1_200_000) == "$0.0012"


def test_format_keeps_two_decimals_and_sign() -> None:
    assert format_usd(2_500_000_000) == "$2.50"
    assert format_usd(0) == "$0.00"
    assert format_usd(-1050) == "-$0.00000105"
    assert format_usd(None) == "—"


def test_half_even_rounding_only_at_nanodollar() -> None:
    # 1 token at $0.0005 per M = 0.5 nanodollars → rounds half-even to 0
    assert tokens_cost_nanos(1, "0.0005") == 0
    assert tokens_cost_nanos(1, "0.0015") == 2  # 1.5 → 2
    assert tokens_cost_nanos(1, "0.0025") == 2  # 2.5 → 2
