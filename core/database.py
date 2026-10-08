"""Transactional local storage with one SQLite connection per operation."""

import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from core.snapshots import AccountSnapshot, SaveResult, SnapshotConflictError


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
    """
    CREATE TABLE IF NOT EXISTS snapshot_accounts (
        account_id TEXT PRIMARY KEY NOT NULL,
        dp_id TEXT NOT NULL CHECK(length(dp_id) = 8 AND
            (dp_id NOT GLOB '*[^0-9]*' OR dp_id GLOB 'IN[0-9][0-9][0-9][0-9][0-9][0-9]')),
        client_id TEXT NOT NULL CHECK(length(client_id) = 8 AND client_id NOT GLOB '*[^0-9]*'),
        CHECK(account_id = 'demat:' || dp_id || ':' || client_id),
        UNIQUE(dp_id, client_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_imports (
        import_id INTEGER PRIMARY KEY,
        source_sha256 TEXT NOT NULL UNIQUE
            CHECK(length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_format TEXT NOT NULL CHECK(source_format IN ('pdf', 'csv')),
        statement_date TEXT NOT NULL CHECK(valid_date(statement_date)),
        imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE(import_id, statement_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_snapshots (
        snapshot_id INTEGER PRIMARY KEY,
        import_id INTEGER NOT NULL,
        account_id TEXT NOT NULL REFERENCES snapshot_accounts(account_id) ON DELETE RESTRICT,
        statement_date TEXT NOT NULL CHECK(valid_date(statement_date)),
        broker TEXT NOT NULL CHECK(length(trim(broker)) > 0),
        content_sha256 TEXT NOT NULL
            CHECK(length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        FOREIGN KEY(import_id, statement_date)
            REFERENCES snapshot_imports(import_id, statement_date) ON DELETE RESTRICT,
        UNIQUE(account_id, statement_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_holdings (
        snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(snapshot_id) ON DELETE RESTRICT,
        isin TEXT NOT NULL CHECK(length(isin) = 12 AND isin GLOB 'IN*' AND isin NOT GLOB '*[^A-Z0-9]*'),
        security_name TEXT NOT NULL,
        asset_class TEXT NOT NULL CHECK(asset_class IN ('Mutual Fund', 'Gold ETF', 'Stock', 'Bond', 'Unclassified')),
        native_currency TEXT NOT NULL CHECK(native_currency GLOB '[A-Z][A-Z][A-Z]'),
        quantity TEXT NOT NULL CHECK(valid_decimal(quantity, 1)),
        price TEXT CHECK(price IS NULL OR valid_decimal(price, 1)),
        market_value TEXT CHECK(market_value IS NULL OR valid_decimal(market_value, 1)),
        PRIMARY KEY(snapshot_id, isin)
    ) WITHOUT ROWID
    """,
)


_SNAPSHOT_TRIGGERS: Tuple[str, ...] = tuple(
    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
    f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END"
    for table in ("snapshot_imports", "account_snapshots", "snapshot_holdings")
    for operation in ("UPDATE", "DELETE")
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
            for statement in _SCHEMA + _SNAPSHOT_TRIGGERS:
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

    def save_snapshots(
        self, snapshots: Sequence[AccountSnapshot], statement_date: date,
        source_sha256: str, source_format: str,
    ) -> SaveResult:
        """Persist an entire validated upload atomically, with duplicate/conflict checks."""
        date_text = statement_date.isoformat()
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT import_id, statement_date FROM snapshot_imports WHERE source_sha256 = ?", (source_sha256,),
            ).fetchone()
            if previous is not None:
                if previous["statement_date"] != date_text:
                    raise SnapshotConflictError("This file was already saved with a different statement date.")
                return SaveResult(previous["import_id"], 0, len(snapshots), True)
            pending: List[AccountSnapshot] = []
            for snapshot in snapshots:
                existing = connection.execute(
                    "SELECT content_sha256 FROM account_snapshots WHERE account_id = ? AND statement_date = ?",
                    (snapshot.account_id, date_text),
                ).fetchone()
                if existing is None:
                    pending.append(snapshot)
                elif existing["content_sha256"] != snapshot.content_sha256:
                    raise SnapshotConflictError("Different holdings are already saved for an account on this statement date. Nothing was overwritten.")
            cursor = connection.execute(
                "INSERT INTO snapshot_imports(source_sha256, source_format, statement_date) VALUES (?, ?, ?)",
                (source_sha256, source_format, date_text),
            )
            import_id = cursor.lastrowid
            if import_id is None:
                raise DatabaseError("The snapshot import could not be created.")
            for snapshot in pending:
                connection.execute(
                    "INSERT INTO snapshot_accounts(account_id, dp_id, client_id) VALUES (?, ?, ?) ON CONFLICT(account_id) DO NOTHING",
                    (snapshot.account_id, snapshot.dp_id, snapshot.client_id),
                )
                cursor = connection.execute(
                    """INSERT INTO account_snapshots(import_id, account_id, statement_date, broker, content_sha256)
                       VALUES (?, ?, ?, ?, ?)""",
                    (import_id, snapshot.account_id, date_text, snapshot.broker, snapshot.content_sha256),
                )
                snapshot_id = cursor.lastrowid
                if snapshot_id is None:
                    raise DatabaseError("The account snapshot could not be created.")
                connection.executemany(
                    """INSERT INTO snapshot_holdings(snapshot_id, isin, security_name, asset_class,
                       native_currency, quantity, price, market_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(snapshot_id, item.isin, item.security_name, item.asset_class,
                      item.native_currency, item.quantity, item.price, item.market_value) for item in snapshot.positions],
                )
            return SaveResult(import_id, len(pending), len(snapshots) - len(pending))

    def load_snapshot_holdings(self, snapshot_id: Optional[int] = None) -> List[Dict[str, Optional[str]]]:
        """Read whole latest account snapshots, never stale per-instrument positions."""
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT accounts.account_id, accounts.dp_id, accounts.client_id, snapshots.broker,
                          snapshots.snapshot_id, snapshots.statement_date, imports.source_format,
                          holdings.isin, holdings.security_name, holdings.asset_class, holdings.native_currency,
                          holdings.quantity, holdings.price, holdings.market_value
                   FROM account_snapshots AS snapshots
                   JOIN snapshot_accounts AS accounts USING(account_id)
                   JOIN snapshot_imports AS imports USING(import_id)
                   JOIN snapshot_holdings AS holdings USING(snapshot_id)
                   WHERE (? IS NOT NULL AND snapshots.snapshot_id = ?)
                      OR (? IS NULL AND NOT EXISTS (
                          SELECT 1 FROM account_snapshots AS newer
                          WHERE newer.account_id = snapshots.account_id
                            AND newer.statement_date > snapshots.statement_date))
                   ORDER BY accounts.account_id, holdings.isin""",
                (snapshot_id, snapshot_id, snapshot_id),
            ).fetchall()
            return [{key: str(value) if value is not None else None for key, value in dict(row).items()} for row in rows]

    def list_snapshots(self) -> List[Dict[str, str]]:
        """List historical snapshots without returning a live connection."""
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT snapshots.snapshot_id, snapshots.account_id, accounts.dp_id, accounts.client_id,
                          snapshots.broker, snapshots.statement_date, imports.imported_at,
                          count(holdings.isin) AS holding_count
                   FROM account_snapshots AS snapshots
                   JOIN snapshot_accounts AS accounts USING(account_id)
                   JOIN snapshot_imports AS imports USING(import_id)
                   JOIN snapshot_holdings AS holdings USING(snapshot_id)
                   GROUP BY snapshots.snapshot_id
                   ORDER BY snapshots.statement_date DESC, snapshots.snapshot_id DESC""",
            ).fetchall()
            return [{key: str(value) for key, value in dict(row).items()} for row in rows]