"""Money.

Amounts are stored as integer **nanodollars** (1 USD = 1_000_000_000). Rates
are decimal strings per million tokens. Arithmetic is exact ``Decimal``; the
only rounding is the final quantisation to a whole nanodollar, half-even.

Nine decimal places hold every fractional-cent list price in use: a 1-token
call at $0.15 per million is exactly 150 nanodollars.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

NANOS_PER_USD = 1_000_000_000
_MILLION = Decimal(1_000_000)
_ONE = Decimal(1)


def tokens_cost_nanos(tokens: int, rate_per_million: str | Decimal) -> int:
    """Exact cost of ``tokens`` at ``rate_per_million`` USD, in nanodollars."""
    if tokens < 0:
        raise ValueError("token count cannot be negative")
    rate = Decimal(str(rate_per_million))
    usd = (Decimal(tokens) * rate) / _MILLION
    return int((usd * NANOS_PER_USD).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def nanos_to_usd(nanos: int) -> Decimal:
    return Decimal(nanos) / NANOS_PER_USD


def usd_string(nanos: int) -> str:
    """Lossless decimal string, e.g. ``0.001200000``."""
    sign = "-" if nanos < 0 else ""
    n = abs(nanos)
    return f"{sign}{n // NANOS_PER_USD}.{n % NANOS_PER_USD:09d}"


def format_usd(nanos: int | None) -> str:
    """Human display: ``$0.0012``, ``$4.813``, ``$2.50``. Never rounds away a
    non-zero digit; trims trailing zeros but keeps at least two decimals."""
    if nanos is None:
        return "—"
    s = usd_string(nanos)
    sign = ""
    if s.startswith("-"):
        sign, s = "-", s[1:]
    whole, frac = s.split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = (frac + "00")[:2]
    return f"{sign}${whole}.{frac}"
