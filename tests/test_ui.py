"""Exercise actual Streamlit reruns with synthetic, temporary local storage."""

import os
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core.config import Settings
from core.database import Database


APP_PATH = Path(__file__).resolve().parents[1] / "ui" / "app.py"


class DashboardTests(unittest.TestCase):
    """Test lazy loading, empty state, currency gaps, and literal filtering."""

    def setUp(self) -> None:
        """Configure a synthetic database outside the repository."""
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".local" / "alaka_vault.sqlite3"
        database_path = patch("core.config.DATABASE_PATH", self.path)
        database_path.start()
        self.addCleanup(database_path.stop)
        environment = patch.dict(os.environ, {
            "ALAKA_DATABASE_PATH": "ignored-legacy-value", "ALAKA_ALLOW_MARKET_DATA": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_fixed_path_ignores_environment_override(self) -> None:
        """Use the fixed location without creating storage during configuration."""
        self.assertEqual(Settings.from_environment().database_path, self.path)
        self.assertFalse(self.path.parent.exists())

    def test_lazy_and_empty_states(self) -> None:
        """Do not create a database before the user requests holdings."""
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        self.assertFalse(app.exception)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(self.path.is_file())
        self.assertEqual(app.info[0].value, "No holdings recorded.")

    def test_populated_filters_and_missing_fx(self) -> None:
        """Show exact native costs and prevent incomplete aggregate valuations."""
        database = Database(self.path)
        database.initialize()
        with database.transaction() as connection:
            connection.executemany(
                "INSERT INTO current_holdings VALUES (?, ?, ?, ?, ?, ?)",
                [(f"DEMO-{index:02d}", "TEST", "EQUITY", "USD", "2", "10.50") for index in range(30)],
            )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value, "Not valued")
        self.assertEqual(app.metric[1].value, "FX unavailable")
        self.assertEqual(app.metric[2].value, "USD 630.00")
        self.assertEqual(len(app.dataframe[0].value), 25)
        app.number_input[0].set_value(2).run()
        self.assertEqual(len(app.dataframe[0].value), 5)
        app.text_input[0].set_value("DEMO-01").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.number_input[0].value, 1)
        self.assertEqual(len(app.dataframe[0].value), 1)
        app.text_input[0].set_value("[").run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe[0].value), 0)

    def test_cas_upload_preview_without_database_write(self) -> None:
        """Parse an uploaded workbook into a session preview without opening the vault."""
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active.append(["ISIN", "Security Name", "Quantity", "Market Value"])
        workbook.active.append(["INE000A01010", "Synthetic Security", "12.50", "1250.00"])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        uploaded = SimpleNamespace(name="synthetic.xlsx", size=len(stream.getvalue()), getvalue=stream.getvalue)
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(app.dataframe[0].value.iloc[0]["ISIN"], "INE000A01010")
            self.assertEqual(app.dataframe[0].value.iloc[0]["Quantity"], "12.50")
            self.assertFalse(self.path.exists())

    def test_cas_failure_clears_password_and_preview(self) -> None:
        """Surface a safe failure and clear sensitive/stale state after a bad PDF."""
        uploaded = SimpleNamespace(name="synthetic.pdf", size=7, getvalue=lambda: b"invalid")
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.text_input[0].set_value("synthetic-secret").run()
            app.session_state["cas_preview"] = [{"stale": True}]
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertIn("not a PDF", app.error[0].value)
            self.assertEqual(app.text_input[0].value, "")
            self.assertFalse(app.dataframe)
            self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()