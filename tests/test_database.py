"""Verify transaction boundaries, exact decimal storage, and schema integrity."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from core.database import Database, DatabaseError
from core.snapshots import SnapshotConflictError, SnapshotError, load_saved_holdings, save_statement
from ingestion.base_parser import ParsedHolding


class DatabaseTests(unittest.TestCase):
    """Exercise a temporary database with synthetic financial events."""

    def setUp(self) -> None:
        """Create an isolated on-disk database for each test."""
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "test.sqlite3")
        self.database.initialize()

    @staticmethod
    def insert_holding(connection: sqlite3.Connection, ticker: str = "DEMO") -> None:
        """Insert a synthetic holding through bound parameters."""
        connection.execute(
            "INSERT INTO current_holdings VALUES (?, ?, ?, ?, ?, ?)",
            (ticker, "TEST", "EQUITY", "INR", "0.123456789123456789", "10.25"),
        )

    @staticmethod
    def insert_trade(connection: sqlite3.Connection, trade_id: str = "TEST-1") -> None:
        """Insert a synthetic trade through bound parameters."""
        connection.execute(
            "INSERT INTO transaction_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, "TEST", "EQUITY", "DEMO", "BUY", "1", "10.25", "1", "2026-01-01"),
        )

    def test_exact_storage_and_foreign_keys(self) -> None:
        """Preserve decimal digits and enforce foreign keys on each connection."""
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.insert_holding(connection)
            self.insert_trade(connection)
        self.assertEqual(self.database.load_holdings()[0]["total_quantity"], "0.123456789123456789")
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            connection.execute("DELETE FROM current_holdings WHERE ticker = ?", ("DEMO",))

    def test_missing_parent_rolls_back(self) -> None:
        """Reject events whose instrument snapshot does not exist."""
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            self.insert_trade(connection)

    def test_application_failure_rolls_back_and_closes(self) -> None:
        """Roll back exceptions outside SQLite and close the escaped connection."""
        with self.assertRaisesRegex(ValueError, "synthetic"), self.database.transaction() as connection:
            self.insert_holding(connection)
            raise ValueError("synthetic")
        self.assertEqual(self.database.load_holdings(), [])
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_invalid_decimal_rolls_back(self) -> None:
        """Reject malformed, nonfinite, and negative quantities."""
        for value in ("NaN", "Infinity", "-1", "1oops"):
            with self.subTest(value=value), self.assertRaises(DatabaseError):
                with self.database.transaction() as connection:
                    self.insert_holding(connection)
                    connection.execute("UPDATE current_holdings SET total_quantity = ?", (value,))
            self.assertEqual(self.database.load_holdings(), [])

    def test_duplicate_and_mutated_events_are_rejected(self) -> None:
        """Prevent duplicate broker event IDs and accidental ledger rewrites."""
        with self.database.transaction() as connection:
            self.insert_holding(connection)
            self.insert_trade(connection)
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            self.insert_trade(connection)
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            connection.execute("UPDATE transaction_ledger SET quantity = ?", ("2",))
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            connection.execute("DELETE FROM transaction_ledger WHERE trade_id = ?", ("TEST-1",))

    def test_read_only_transaction(self) -> None:
        """Disallow writes through the read-only connection boundary."""
        with self.assertRaises(DatabaseError), self.database.transaction(write=False) as connection:
            self.insert_holding(connection)

    def test_snapshot_schema_is_additive(self) -> None:
        """Create snapshot storage without changing existing ledger rows."""
        with self.database.transaction() as connection:
            self.insert_holding(connection)
            self.insert_trade(connection)
        self.database.initialize()
        with self.database.transaction(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM transaction_ledger").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM current_holdings").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM account_snapshots").fetchone()[0], 0)
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO snapshot_holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (999, "INE000A01010", "Synthetic", "Stock", "INR", "1", None, None),
            )

    @staticmethod
    def snapshot_row(client_id: str = "00000002", quantity: str = "1.25") -> ParsedHolding:
        """Build a synthetic account position without reading any local statements."""
        return ParsedHolding(
            account_id=f"demat:00000001:{client_id}", dp_id="00000001", client_id=client_id,
            broker="Synthetic Broker", isin="INE000A01010", security_name="Synthetic Equity",
            asset_class="Stock", native_currency="INR", quantity=Decimal(quantity),
            price=Decimal("10.123456789123456789"), market_value=None, source="CSV",
        )

    def test_snapshot_save_reload_and_duplicates(self) -> None:
        """Persist exact values across connections and deduplicate file and economic contents."""
        digest = sha256(b"synthetic-file").hexdigest()
        result = save_statement(self.database, [self.snapshot_row()], date(2026, 1, 2), digest, "csv")
        self.assertEqual(result.saved_accounts, 1)
        repeated = save_statement(self.database, [self.snapshot_row()], date(2026, 1, 2), digest, "csv")
        self.assertTrue(repeated.already_imported)
        equivalent = save_statement(self.database, [self.snapshot_row(quantity="1.2500")], date(2026, 1, 2), sha256(b"alternate-export").hexdigest(), "pdf")
        self.assertEqual(equivalent.saved_accounts, 0)
        self.assertEqual(equivalent.duplicate_accounts, 1)
        reloaded = load_saved_holdings(Database(self.database._path))
        self.assertEqual(reloaded[0]["price"], Decimal("10.123456789123456789"))
        self.assertIsNone(reloaded[0]["market_value"])
        self.assertEqual(len(self.database.list_snapshots()), 1)
        self.assertEqual(self.database.load_holdings(), [])
        with self.database.transaction(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM transaction_ledger").fetchone()[0], 0)

    def test_snapshot_history_and_latest_whole_account(self) -> None:
        """Keep history without carrying positions absent from a newer account snapshot."""
        older = self.snapshot_row()
        extra = dict(older, isin="INE000B01010")
        other_account = self.snapshot_row("00000003", "8")
        save_statement(self.database, [older, extra, other_account], date(2026, 1, 1), sha256(b"old").hexdigest(), "csv")
        save_statement(self.database, [self.snapshot_row(quantity="4")], date(2026, 1, 3), sha256(b"new").hexdigest(), "csv")
        save_statement(self.database, [self.snapshot_row(quantity="3")], date(2026, 1, 2), sha256(b"late-old").hexdigest(), "csv")
        latest = load_saved_holdings(self.database)
        self.assertEqual([row["quantity"] for row in latest], [Decimal("4"), Decimal("8")])
        history = self.database.list_snapshots()
        self.assertEqual(len(history), 4)
        old_snapshot = next(row for row in history if row["statement_date"] == "2026-01-01" and row["client_id"] == "00000002")
        self.assertEqual(len(load_saved_holdings(self.database, int(old_snapshot["snapshot_id"]))), 2)

    def test_snapshot_conflicts_and_validation_are_atomic(self) -> None:
        """Reject invalid or conflicting batches without saving any of their accounts."""
        digest = sha256(b"initial").hexdigest()
        save_statement(self.database, [self.snapshot_row()], date(2026, 1, 1), digest, "csv")
        conflict_rows = [self.snapshot_row("00000003"), self.snapshot_row(quantity="5")]
        conflict_date, conflict_digest = date(2026, 1, 1), sha256(b"conflict").hexdigest()
        with self.assertRaises(SnapshotConflictError):
            save_statement(self.database, conflict_rows, conflict_date, conflict_digest, "csv")
        rows, changed_date = [self.snapshot_row()], date(2026, 1, 2)
        with self.assertRaises(SnapshotConflictError):
            save_statement(self.database, rows, changed_date, digest, "csv")
        invalid_rows = [self.snapshot_row(), dict(self.snapshot_row(), client_id=None)]
        invalid_date, invalid_digest = date(2026, 1, 3), sha256(b"invalid").hexdigest()
        with self.assertRaises(SnapshotError):
            save_statement(self.database, invalid_rows, invalid_date, invalid_digest, "csv")
        with self.database.transaction(write=False) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM snapshot_imports").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM snapshot_accounts").fetchone()[0], 1)
        with self.assertRaises(DatabaseError), self.database.transaction() as connection:
            connection.execute("UPDATE snapshot_holdings SET quantity = ?", ("99",))

    def test_snapshot_database_failure_rolls_back_entire_import(self) -> None:
        """Force a failure after a successful position insert and leave no partial history."""
        with self.database.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER synthetic_failure BEFORE INSERT ON snapshot_holdings
                   WHEN NEW.isin = 'INE000B01010'
                   BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""",
            )
        rows = [self.snapshot_row(), dict(self.snapshot_row(), isin="INE000B01010")]
        statement_date, digest = date(2026, 1, 1), sha256(b"rollback").hexdigest()
        with self.assertRaises(DatabaseError):
            save_statement(self.database, rows, statement_date, digest, "csv")
        with self.database.transaction(write=False) as connection:
            for table in ("snapshot_accounts", "snapshot_imports", "account_snapshots", "snapshot_holdings"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_invalid_snapshot_does_not_create_storage(self) -> None:
        """Reject unknown identity, duplicate instruments and malformed values before disk access."""
        invalid_rows = [
            [dict(self.snapshot_row(), broker=None)],
            [dict(self.snapshot_row(), quantity=Decimal("NaN"))],
            [dict(self.snapshot_row(), price=Decimal("-1"))],
            [self.snapshot_row(), self.snapshot_row()],
        ]
        path = self.database._path.parent / "unused" / "vault.sqlite3"
        database = Database(path)
        statement_date, digest = date(2026, 1, 1), sha256(b"invalid").hexdigest()
        for rows in invalid_rows:
            with self.subTest(), self.assertRaises(SnapshotError):
                save_statement(database, rows, statement_date, digest, "csv")
        self.assertFalse(path.parent.exists())

    def test_concurrent_snapshot_reimport(self) -> None:
        """Serialize duplicate uploads so only one writer inserts a snapshot."""
        def save(index: int) -> int:
            """Attempt the same synthetic import on an independent connection."""
            result = save_statement(self.database, [self.snapshot_row()], date(2026, 1, 1), sha256(b"concurrent").hexdigest(), "csv")
            return result.saved_accounts

        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(sum(executor.map(save, range(4))), 1)
        self.assertEqual(len(self.database.list_snapshots()), 1)

    def test_concurrent_writers(self) -> None:
        """Use independent connections and let SQLite serialize concurrent writes."""
        def insert(index: int) -> None:
            """Write one unique synthetic instrument."""
            with self.database.transaction() as connection:
                self.insert_holding(connection, f"DEMO-{index}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(insert, range(12)))
        self.assertEqual(len(self.database.load_holdings()), 12)


if __name__ == "__main__":
    unittest.main()