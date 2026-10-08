"""Verify transaction boundaries, exact decimal storage, and schema integrity."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from core.database import Database, DatabaseError


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