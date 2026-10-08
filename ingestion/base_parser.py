"""Typed contracts and privacy-safe error boundaries for statement ingestion."""

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generic, List, Literal, Optional, TypeVar, TypedDict


class ParsingError(ValueError):
    """Report an ingestion failure without exposing statement contents."""


class UnsupportedStatementError(ParsingError):
    """Indicate an unsupported file format or inaccessible local file."""


class ParserNotImplementedError(ParsingError):
    """Indicate a deliberately unimplemented broker adapter."""


class ParsedTransaction(TypedDict):
    """Normalized long-only event; FX is INR per unit of native currency.

    IDs must be stable within a broker for repeat-import idempotency. Parsers
    must emit Decimal values directly from source text, never through floats.
    Dates are broker-local trade dates, not settlement dates. Native currency
    belongs to the referenced holding and is included for import validation.
    """

    trade_id: str
    broker: str
    asset_class: str
    ticker: str
    native_currency: str
    tx_type: Literal["BUY", "SELL"]
    quantity: Decimal
    price_native: Decimal
    fx_rate: Decimal
    trade_date: date


class ParsedHolding(TypedDict):
    """A CAS snapshot row; statement values are not acquisition costs or trades."""

    isin: str
    security_name: str
    quantity: Decimal
    price: Optional[Decimal]
    market_value: Optional[Decimal]
    native_currency: str
    source: str


ParsedRecord = TypeVar("ParsedRecord")


class BaseParser(ABC, Generic[ParsedRecord]):
    """Normalize a local document without writing to a database or making requests."""

    @abstractmethod
    def parse_file(self, file_path: Path) -> List[ParsedRecord]:
        """Parse a local statement into validated records.

        Args:
            file_path: Local input document; never logged or uploaded.

        Returns:
            Normalized records, retaining exact source decimal precision.

        Raises:
            ParsingError: Parsing is unsupported, incomplete, or unsuccessful.
        """
        raise NotImplementedError