"""Offline classification of statement instruments without market-data requests."""

import re
from typing import Dict, Literal


AssetClass = Literal["Mutual Fund", "Gold ETF", "Stock", "Bond", "Unclassified"]

MUTUAL_FUND: AssetClass = "Mutual Fund"
GOLD_ETF: AssetClass = "Gold ETF"
STOCK: AssetClass = "Stock"
BOND: AssetClass = "Bond"
UNCLASSIFIED: AssetClass = "Unclassified"

_STATEMENT_TYPES: Dict[str, AssetClass] = {
    "mutual fund": MUTUAL_FUND,
    "mutual funds": MUTUAL_FUND,
    "mf": MUTUAL_FUND,
    "etf": MUTUAL_FUND,
    "exchange traded fund": MUTUAL_FUND,
    "gold etf": GOLD_ETF,
    "gold exchange traded fund": GOLD_ETF,
    "stock": STOCK,
    "stocks": STOCK,
    "equity": STOCK,
    "equity shares": STOCK,
    "ordinary shares": STOCK,
    "bond": BOND,
    "bonds": BOND,
    "debenture": BOND,
    "debentures": BOND,
    "ncd": BOND,
    "non convertible debenture": BOND,
    "government security": BOND,
    "g sec": BOND,
    "sovereign gold bond": BOND,
    "sgb": BOND,
}
_FUND = re.compile(r"\b(?:MUTUAL FUNDS?|ETF|EXCHANGE TRADED FUND)\b")
_BOND = re.compile(r"\b(?:BONDS?|DEBENTURES?|NCD|SGB|G[ -]?SEC|GOVERNMENT SECURITIES)\b")
_STOCK = re.compile(r"\b(?:EQUITY SHARES?|ORDINARY SHARES?|EQ(?:UITY)?)\b")
_OTHER = re.compile(r"\b(?:REIT|INVIT|AIF|PREFERENCE SHARES?|WARRANTS?)\b")
_GOLD = re.compile(r"\bGOLD\b")
_ETF = re.compile(r"\b(?:ETF|EXCHANGE[ -]TRADED[ -]FUND)\b")
_FUND_OF_FUNDS = re.compile(r"\b(?:FOF|FUND OF FUNDS?)\b")


def classify_asset(isin: str, security_name: str, statement_type: str = "") -> AssetClass:
    """Classify an instrument, preserving uncertainty rather than guessing.

    Args:
        isin: Statement ISIN; an INF prefix identifies Indian fund units.
        security_name: Statement description, never logged or sent externally.
        statement_type: Optional explicit type supplied by the statement.

    Returns:
        A presentation category. Explicit unsupported types remain Unclassified.
        Gold ETFs are distinct; other ETFs remain funds, not direct bonds/stocks.
        An INE prefix alone is insufficient to distinguish shares from debt.
    """
    normalized_type = " ".join(re.sub(r"[^a-z0-9]+", " ", statement_type.lower()).split())
    name = " ".join(security_name.upper().split())
    gold_etf = bool(_GOLD.search(name) and _ETF.search(name) and not _FUND_OF_FUNDS.search(name))
    if normalized_type:
        category = _STATEMENT_TYPES.get(normalized_type, UNCLASSIFIED)
        return GOLD_ETF if category == MUTUAL_FUND and gold_etf else category
    if _OTHER.search(name):
        return UNCLASSIFIED
    if gold_etf:
        return GOLD_ETF
    if re.fullmatch(r"INF[A-Z0-9]{9}", isin.upper()) or _FUND.search(name):
        return MUTUAL_FUND
    bond = bool(_BOND.search(name))
    stock = bool(_STOCK.search(name))
    if bond and stock:
        return UNCLASSIFIED
    if bond:
        return BOND
    if stock:
        return STOCK
    return UNCLASSIFIED