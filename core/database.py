"""Transactional local storage with one SQLite connection per operation."""

import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple


class DatabaseError(RuntimeError):
    """Report a storage failure without including private SQL values or paths."""


def _valid_decimal(value: object, allow_zero: int) -> int:
    """Validate exact, finite decimal text used by schema constraints."""
    if not isinstance(value, str):
        return 0
    try:
        number = Decimal(value)
        return int(number.is_finite() and (number >= 0 if allow_zero else number > 0))
    except InvalidOperation:
        return 0


def _valid_date(value: object) -> int:
    """Require a canonical ISO calendar date, not a locale-dependent string."""
    if not isinstance(value, str):
        return 0
    try:
        return int(date.fromisoformat(value).isoformat() == value)
    except ValueError:
        return 0


_SCHEMA: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS current_holdings (
        ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
        broker TEXT NOT NULL CHECK(length(trim(broker)) > 0),
        asset_class TEXT NOT NULL CHECK(length(trim(asset_class)) > 0),
        native_currency TEXT NOT NULL
            CHECK(native_currency GLOB '[A-Z][A-Z][A-Z]'),
        total_quantity TEXT NOT NULL CHECK(valid_decimal(total_quantity, 1)),
        avg_buy_price TEXT NOT NULL CHECK(valid_decimal(avg_buy_price, 1)),
        PRIMARY KEY (ticker, broker, asset_class)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS transaction_ledger (
        trade_id TEXT NOT NULL CHECK(length(trim(trade_id)) > 0),
        broker TEXT NOT NULL CHECK(length(trim(broker)) > 0),
        asset_class TEXT NOT NULL CHECK(length(trim(asset_class)) > 0),
        ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
        tx_type TEXT NOT NULL CHECK(tx_type IN ('BUY', 'SELL')),
        quantity TEXT NOT NULL CHECK(valid_decimal(quantity, 0)),
        price_native TEXT NOT NULL CHECK(valid_decimal(price_native, 1)),
        fx_rate TEXT NOT NULL CHECK(valid_decimal(fx_rate, 0)),
        trade_date TEXT NOT NULL CHECK(valid_date(trade_date)),
        PRIMARY KEY (broker, trade_id),
        FOREIGN KEY (ticker, broker, asset_class)
            REFERENCES current_holdings(ticker, broker, asset_class)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS ledger_instrument_date
    ON transaction_ledger(ticker, broker, asset_class, trade_date)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_update
    BEFORE UPDATE ON transaction_ledger
    BEGIN SELECT RAISE(ABORT, 'Ledger events are immutable'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_delete
    BEFORE DELETE ON transaction_ledger
    BEGIN SELECT RAISE(ABORT, 'Ledger events are immutable'); END
    """,
)


class Database:
    """Open independent connections so no connection crosses thread boundaries.

    Args:
        path: Explicit absolute path supplied by configuration.
        timeout_seconds: Maximum time SQLite waits for another writer.

    Decimal columns contain text. Bind decimal values with ``str(value)``.
    Holdings are materialized snapshots; callers must update a snapshot and
    append its event in the same transaction. Retain zero-quantity snapshots
    to preserve ledger references. This foundation supports long-only trades.
    """

    def __init__(self, path: Path, timeout_seconds: float = 10.0) -> None:
        if not path.is_absolute():
            raise DatabaseError("An absolute database path is required.")
        if timeout_seconds <= 0:
            raise DatabaseError("The database timeout must be positive.")
        self._path = path
        self._timeout_seconds = timeout_seconds

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any failure, and always close.

        Args:
            write: Acquire the write reservation up front when true; otherwise
                enforce a read-only transaction on this connection.

        Yields:
            A connection confined to the calling thread and context lifetime.

        Raises:
            DatabaseError: Opening, querying, or committing storage failed.
        """
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(
                self._path, timeout=self._timeout_seconds, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.create_function("valid_decimal", 2, _valid_decimal, deterministic=True)
            connection.create_function("valid_date", 1, _valid_date, deterministic=True)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            if not write:
                connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error:
            raise DatabaseError("The local database operation failed.") from None
        finally:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                finally:
                    connection.close()

    def initialize(self) -> None:
        """Create the local storage directory, then create the schema atomically."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise DatabaseError("The local database directory could not be created.") from None
        with self.transaction() as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    def load_holdings(self) -> List[Dict[str, str]]:
        """Return a stable snapshot without passing live connections to the UI."""
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT ticker, broker, asset_class, native_currency,
                          total_quantity, avg_buy_price
                   FROM current_holdings ORDER BY ticker, broker, asset_class"""
            ).fetchall()
            return [dict(row) for row in rows]