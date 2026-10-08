"""Offline classification of statement instruments without market-data requests."""

import re
from dataclasses import dataclass
from typing import Dict, Literal, Optional, Tuple


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

FundBroadCategory = Literal["Equity", "Debt", "Hybrid", "Solution Oriented", "Other", "Unknown"]
_SOLUTION_ORIENTED: FundBroadCategory = "Solution Oriented"


@dataclass(frozen=True)
class FundCategory:
    """Name-derived scheme categories, not an authoritative regulatory classification."""

    broad: Optional[FundBroadCategory]
    scheme: Optional[str]


_FUND_CATEGORY_RULES: Tuple[Tuple[FundBroadCategory, str, str], ...] = (
    ("Equity", "Large & Mid Cap", r"LARGE (?:AND )?MID CAP"),
    ("Equity", "Large Cap", r"LARGE CAP"),
    ("Equity", "Mid Cap", r"MID CAP"),
    ("Equity", "Small Cap", r"SMALL CAP"),
    ("Equity", "Flexi Cap", r"FLEXI CAP"),
    ("Equity", "Multi Cap", r"MULTI CAP"),
    ("Equity", "ELSS", r"ELSS|EQUITY LINKED SAVINGS|TAX SAVER"),
    ("Equity", "Value", r"VALUE FUND"),
    ("Equity", "Contra", r"CONTRA"),
    ("Equity", "Focused", r"FOCUSED|FOCUSSED"),
    ("Equity", "Dividend Yield", r"DIVIDEND YIELD"),
    ("Equity", "Sectoral / Thematic", r"SECTORAL|THEMATIC"),
    ("Debt", "Overnight", r"OVERNIGHT"),
    ("Debt", "Liquid", r"LIQUID"),
    ("Debt", "Ultra Short Duration", r"ULTRA SHORT (?:DURATION|TERM)"),
    ("Debt", "Low Duration", r"LOW DURATION"),
    ("Debt", "Money Market", r"MONEY MARKET"),
    ("Debt", "Short Duration", r"SHORT (?:DURATION|TERM(?! EQUITY))"),
    ("Debt", "Medium to Long Duration", r"MEDIUM (?:TO )?LONG (?:DURATION|TERM)"),
    ("Debt", "Medium Duration", r"MEDIUM (?:DURATION|TERM)"),
    ("Debt", "Long Duration", r"LONG (?:DURATION|TERM(?! EQUITY))"),
    ("Debt", "Dynamic Bond", r"DYNAMIC BOND"),
    ("Debt", "Corporate Bond", r"CORPORATE BOND"),
    ("Debt", "Credit Risk", r"CREDIT RISK"),
    ("Debt", "Banking & PSU", r"BANKING (?:AND )?PSU"),
    ("Debt", "Gilt - 10 Year Constant Duration", r"GILT (?:WITH )?10 YEAR CONSTANT DURATION"),
    ("Debt", "Gilt", r"GILT"),
    ("Debt", "Floater", r"FLOATER|FLOATING RATE"),
    ("Debt", "Target Maturity", r"TARGET MATURITY"),
    ("Hybrid", "Conservative Hybrid", r"CONSERVATIVE HYBRID"),
    ("Hybrid", "Balanced Hybrid", r"BALANCED HYBRID"),
    ("Hybrid", "Aggressive Hybrid", r"AGGRESSIVE HYBRID"),
    ("Hybrid", "Balanced Advantage / Dynamic Asset Allocation", r"BALANCED ADVANTAGE|DYNAMIC ASSET ALLOCATION"),
    ("Hybrid", "Multi Asset Allocation", r"MULTI ASSET(?: ALLOCATION)?"),
    ("Hybrid", "Arbitrage", r"ARBITRAGE"),
    ("Hybrid", "Equity Savings", r"EQUITY SAVINGS"),
    (_SOLUTION_ORIENTED, "Retirement", r"RETIREMENT"),
    (_SOLUTION_ORIENTED, "Children's", r"CHILDREN(?: S)?|CHILD S"),
)


_FUND_BROAD_RULES: Tuple[Tuple[FundBroadCategory, str], ...] = (
    ("Equity", r"\b(?:EQUITY|SENSEX)\b"),
    ("Equity", r"\bNIFTY (?:50|100|200|500|NEXT 50)\b"),
    ("Equity", r"\bNIFTY ALPHA 50\b"),
    ("Equity", r"\bNASDAQ 100\b"),
    ("Equity", r"\bS (?:AND )?P 500\b"),
    ("Debt", r"\b(?:DEBT|BOND|G SEC|GOVERNMENT SECURITIES)\b"),
    ("Hybrid", r"\b(?:HYBRID|BALANCED)\b"),
)


_ELSS_CATEGORY: Tuple[FundBroadCategory, str] = ("Equity", "ELSS")


def _specific_fund_categories(name: str) -> set[Tuple[FundBroadCategory, str]]:
    """Prefer containing phrases and ELSS over equity styles; retain asset conflicts."""
    matches = [
        (match.start(), match.end(), broad, scheme)
        for broad, scheme, pattern in _FUND_CATEGORY_RULES
        for match in re.finditer(r"\b(?:" + pattern + r")\b", name)
    ]
    categories = {
        (broad, scheme) for start, end, broad, scheme in matches
        if not any(
            outer_start <= start and outer_end >= end and (outer_start, outer_end) != (start, end)
            for outer_start, outer_end, _, _ in matches
        )
    }
    if _ELSS_CATEGORY in categories and {broad for broad, _ in categories} == {"Equity"}:
        return {_ELSS_CATEGORY}
    return categories


def classify_mutual_fund(security_name: str, asset_class: AssetClass = MUTUAL_FUND) -> FundCategory:
    """Infer broad and detailed categories locally, preserving uncertain fund identities."""
    if asset_class != MUTUAL_FUND:
        return FundCategory(None, None)
    name = re.sub(r"[^A-Z0-9]+", " ", security_name.upper().replace("&", " AND ")).strip()
    name = re.sub(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])", " ", name)
    name = re.sub(r"\bLARGE\s*MID\s*CAP\b", "LARGE MID CAP", name)
    name = re.sub(r"\b(LARGE|MID|SMALL|FLEXI|MULTI)CAP\b", r"\1 CAP", name)
    categories = _specific_fund_categories(name)
    if len(categories) > 1:
        broad_categories = {broad for broad, _ in categories}
        broad = next(iter(broad_categories)) if len(broad_categories) == 1 else "Unknown"
        return FundCategory(broad, "Unknown")
    if categories:
        broad, scheme = next(iter(categories))
        return FundCategory(broad, "Fund of Funds" if _FUND_OF_FUNDS.search(name) else scheme)
    return _generic_fund_category(name)


def _generic_fund_category(name: str) -> FundCategory:
    """Separate broad investment hints from generic index and fund-of-funds structures."""
    broad_matches: set[FundBroadCategory] = set()
    for broad, pattern in _FUND_BROAD_RULES:
        if re.search(pattern, name):
            broad_matches.add(broad)
    broad = next(iter(broad_matches)) if len(broad_matches) == 1 else "Unknown"
    if len(broad_matches) > 1:
        return FundCategory("Unknown", "Unknown")
    if _FUND_OF_FUNDS.search(name):
        return FundCategory(broad if broad != "Unknown" else "Other", "Fund of Funds")
    if _ETF.search(name) or re.search(r"\bINDEX\b", name):
        return FundCategory(broad, "Index Fund / ETF")
    return FundCategory(broad, "Unknown")


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