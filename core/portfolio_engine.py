"""Exact cost-basis aggregation independent of storage, plotting, and market APIs."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Dict, Iterable, Mapping, Optional


class PortfolioError(ValueError):
    """Indicate unsupported or malformed holdings without leaking row values."""


@dataclass(frozen=True)
class PortfolioSummary:
    """Keep cost basis separate from market valuation, cash, and liabilities."""

    cost_by_currency: Mapping[str, Decimal]
    cost_by_asset_inr: Mapping[str, Decimal]
    total_cost_inr: Optional[Decimal]


def decimal_value(value: str) -> Decimal:
    """Read a nonnegative finite decimal without a float conversion."""
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise PortfolioError("A holding contains an invalid decimal.") from None
    if not result.is_finite() or result < 0:
        raise PortfolioError("A holding contains an invalid decimal.")
    return result


def summarize_holdings(
    holdings: Iterable[Mapping[str, str]], usd_inr: Optional[Decimal] = None
) -> PortfolioSummary:
    """Aggregate native cost and INR-translated cost, never market net worth.

    Args:
        holdings: Validated long-only snapshots with quantity and average cost.
        usd_inr: Positive INR per USD; None means USD conversion is unavailable.

    Returns:
        Native subtotals and a complete INR total, or None if any currency cannot
        be translated. Never return a misleading partial aggregate as a total.

    Raises:
        PortfolioError: A required field or decimal is invalid.
    """
    if usd_inr is not None and (not usd_inr.is_finite() or usd_inr <= 0):
        raise PortfolioError("The conversion rate must be positive and finite.")
    native: Dict[str, Decimal] = {}
    assets: Dict[str, Decimal] = {}
    complete = True
    try:
        with localcontext() as context:
            context.prec = 50
            for holding in holdings:
                quantity = decimal_value(holding["total_quantity"])
                price = decimal_value(holding["avg_buy_price"])
                currency = holding["native_currency"]
                cost = quantity * price
                native[currency] = native.get(currency, Decimal(0)) + cost
                rate = Decimal(1) if currency == "INR" else usd_inr if currency == "USD" else None
                if rate is None and cost != 0:
                    complete = False
                    continue
                asset_class = holding["asset_class"]
                assets[asset_class] = assets.get(asset_class, Decimal(0)) + cost * (rate or Decimal(0))
            total = sum(assets.values(), Decimal(0)) if complete else None
    except KeyError:
        raise PortfolioError("A holding is missing a required field.") from None
    return PortfolioSummary(native, assets if complete else {}, total)


def format_money(amount: Decimal, currency: str) -> str:
    """Round only for display using half-even cents and an explicit currency code."""
    with localcontext() as context:
        context.prec = max(50, len(amount.as_tuple().digits) + abs(amount.adjusted()) + 4)
        rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{currency} {rounded:,.2f}"