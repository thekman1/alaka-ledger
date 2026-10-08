"""Validated, account-aware statement snapshots independent of ingestion and UI."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Optional, Tuple, TypedDict, cast

from core.asset_classifier import AssetClass

if TYPE_CHECKING:
    from core.database import Database


class SnapshotError(ValueError):
    """Reject incomplete snapshot input without disclosing financial values."""


class SnapshotConflictError(SnapshotError):
    """Reject a conflicting account/date snapshot without overwriting history."""


@dataclass(frozen=True)
class SnapshotPosition:
    """A validated, exact-decimal position ready for storage."""

    isin: str
    security_name: str
    asset_class: AssetClass
    native_currency: str
    quantity: str
    price: Optional[str]
    market_value: Optional[str]


@dataclass(frozen=True, repr=False)
class AccountSnapshot:
    """One complete account snapshot; exclude sensitive fields from repr."""

    account_id: str
    dp_id: str
    client_id: str
    broker: str
    positions: Tuple[SnapshotPosition, ...]
    content_sha256: str


@dataclass(frozen=True)
class SaveResult:
    """Describe an atomic import without exposing accounts or financial values."""

    import_id: int
    saved_accounts: int
    duplicate_accounts: int
    already_imported: bool = False


class SavedHolding(TypedDict):
    """A decoded persisted snapshot row, including provenance for display."""

    account_id: Optional[str]
    dp_id: Optional[str]
    client_id: Optional[str]
    broker: Optional[str]
    isin: str
    security_name: str
    asset_class: AssetClass
    native_currency: str
    quantity: Decimal
    price: Optional[Decimal]
    market_value: Optional[Decimal]
    source: str
    statement_date: str
    snapshot_id: str


_ASSET_CLASSES = {"Mutual Fund", "Gold ETF", "Stock", "Bond", "Unclassified"}


def _text(row: Mapping[str, object], field: str, *, required: bool = True) -> str:
    """Require text without implicit coercion of account identifiers."""
    value = row.get(field)
    if not isinstance(value, str) or (required and not value.strip()):
        raise SnapshotError("Every holding needs complete, valid account and instrument fields.")
    return " ".join(value.split())


def _decimal_text(value: object, *, optional: bool = False) -> Optional[str]:
    """Canonicalize bounded decimal text without binary floats or context rounding."""
    if value is None and optional:
        return None
    if not isinstance(value, (Decimal, str)):
        raise SnapshotError("Snapshot numeric values must be exact decimals.")
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise SnapshotError("Snapshot values must be finite and nonnegative.")
        if len(number.as_tuple().digits) > 100 or abs(int(number.as_tuple().exponent)) > 100:
            raise SnapshotError("A snapshot value exceeds supported precision.")
    except InvalidOperation:
        raise SnapshotError("A snapshot contains an invalid decimal.") from None
    if number.is_zero():
        return "0"
    formatted = format(number, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _position(row: Mapping[str, object]) -> SnapshotPosition:
    """Validate an instrument independently of any broker parser."""
    isin = _text(row, "isin")
    currency = _text(row, "native_currency")
    asset_class = _text(row, "asset_class")
    if not re.fullmatch(r"IN[A-Z0-9]{10}", isin) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise SnapshotError("A snapshot has an invalid instrument or currency.")
    if asset_class not in _ASSET_CLASSES:
        raise SnapshotError("A snapshot has an unsupported asset class.")
    quantity = _decimal_text(row.get("quantity"))
    if quantity is None:
        raise SnapshotError("A snapshot requires a quantity.")
    return SnapshotPosition(
        isin, _text(row, "security_name", required=False), cast(AssetClass, asset_class),
        currency, quantity, _decimal_text(row.get("price"), optional=True),
        _decimal_text(row.get("market_value"), optional=True),
    )


def prepare_snapshots(holdings: Iterable[Mapping[str, object]], statement_date: date) -> Tuple[AccountSnapshot, ...]:
    """Group validated positions by full demat identity and compute stable digests."""
    if type(statement_date) is not date or statement_date > date.today():
        raise SnapshotError("Confirm a valid statement date that is not in the future.")
    positions: Dict[str, Dict[str, SnapshotPosition]] = {}
    identities: Dict[str, Tuple[str, str, str]] = {}
    for row in holdings:
        account_id = _text(row, "account_id")
        dp_id, client_id, broker = (_text(row, field) for field in ("dp_id", "client_id", "broker"))
        if (
            not re.fullmatch(r"(?:\d{8}|IN\d{6})", dp_id, flags=re.ASCII)
            or not re.fullmatch(r"\d{8}", client_id, flags=re.ASCII)
            or account_id != f"demat:{dp_id}:{client_id}"
        ):
            raise SnapshotError("Complete and consistent DP/client identity is required before saving.")
        identity = (dp_id, client_id, broker)
        if account_id in identities and identities[account_id] != identity:
            raise SnapshotError("Conflicting broker labels were found for one account.")
        identities[account_id] = identity
        position = _position(row)
        account_positions = positions.setdefault(account_id, {})
        if position.isin in account_positions:
            raise SnapshotError("An instrument appears more than once in one account. Reconcile the statement before saving.")
        account_positions[position.isin] = position
    if not positions:
        raise SnapshotError("There are no holdings to save.")
    snapshots: List[AccountSnapshot] = []
    for account_id in sorted(positions):
        ordered = tuple(positions[account_id][isin] for isin in sorted(positions[account_id]))
        payload = [
            [item.isin, item.native_currency, item.quantity, item.price, item.market_value]
            for item in ordered
        ]
        digest = hashlib.sha256(json.dumps(
            [account_id, statement_date.isoformat(), payload], separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")).hexdigest()
        dp_id, client_id, broker = identities[account_id]
        snapshots.append(AccountSnapshot(account_id, dp_id, client_id, broker, ordered, digest))
    return tuple(snapshots)


def save_statement(
    database: "Database", holdings: Iterable[Mapping[str, object]], statement_date: date,
    source_sha256: str, source_format: str,
) -> SaveResult:
    """Validate the complete upload before creating storage or opening a write transaction."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256) or source_format not in {"pdf", "csv"}:
        raise SnapshotError("The parsed upload has no valid source identity. Parse it again.")
    snapshots = prepare_snapshots(holdings, statement_date)
    database.initialize()
    return database.save_snapshots(snapshots, statement_date, source_sha256, source_format)


def load_saved_holdings(database: "Database", snapshot_id: Optional[int] = None) -> List[SavedHolding]:
    """Decode exact values for the latest complete snapshot per account or one history entry."""
    result: List[SavedHolding] = []
    for row in database.load_snapshot_holdings(snapshot_id):
        result.append(SavedHolding(
            account_id=row["account_id"], dp_id=row["dp_id"], client_id=row["client_id"], broker=row["broker"],
            isin=str(row["isin"]), security_name=str(row["security_name"]),
            asset_class=cast(AssetClass, row["asset_class"]), native_currency=str(row["native_currency"]),
            quantity=Decimal(str(row["quantity"])),
            price=Decimal(row["price"]) if row["price"] is not None else None,
            market_value=Decimal(row["market_value"]) if row["market_value"] is not None else None,
            source=str(row["source_format"]).upper(), statement_date=str(row["statement_date"]),
            snapshot_id=str(row["snapshot_id"]),
        ))
    return result