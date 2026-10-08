"""Exercise actual Streamlit reruns with synthetic, temporary local storage."""

import os
import unittest
from datetime import date
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
        """Parse uploaded CSV into a session preview without opening the vault."""
        stream = BytesIO(
            b"DP ID:00000001\nClient ID:00000002\nDP Name:Synthetic Broker\n"
            b"ISIN,ISIN Name,Quantity,Last Closing Price,Market Value\n"
            b"INE000A01010,Synthetic Equity Shares,12.50,100,1250.00\n"
            b"INF000A01010,Synthetic Bond Fund,2,10,20\n"
            b"INE000A07010,Synthetic NCD,3,10,30\n"
        )
        uploaded = SimpleNamespace(name="synthetic.csv", size=len(stream.getvalue()), getvalue=stream.getvalue)
        with patch("streamlit.file_uploader", return_value=uploaded) as uploader:
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            self.assertEqual(uploader.call_args.kwargs["type"], ["pdf", "csv"])
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(app.dataframe[0].value.iloc[0]["ISIN"], "INE000A01010")
            self.assertEqual(app.dataframe[0].value.iloc[0]["Quantity"], "12.50")
            self.assertEqual(app.dataframe[0].value.iloc[0]["Security"], "Synthetic Equity Shares")
            self.assertEqual(app.dataframe[0].value["Asset Class"].tolist(), ["Stock", "Mutual Fund", "Bond"])
            self.assertEqual(app.dataframe[0].value.iloc[0]["Statement Price"], "100")
            self.assertEqual(app.dataframe[0].value.iloc[0]["Broker / DP"], "Synthetic Broker")
            self.assertEqual(app.dataframe[0].value.iloc[0]["Account"], "DP ****0001 / Client ****0002")
            app.checkbox[0].check().run()
            self.assertEqual(app.dataframe[0].value.iloc[0]["Account"], "demat:00000001:00000002")
            self.assertFalse(self.path.exists())

    def test_save_to_vault_and_fresh_session_reload(self) -> None:
        """Require confirmation, save explicitly, and reload persisted holdings in a fresh session."""
        content = (
            b"DP ID:00000001\nClient ID:00000002\nDP Name:Synthetic Broker\n"
            b"ISIN,ISIN Name,Quantity,Last Closing Price\n"
            b"INE000A01010,Synthetic Equity Shares,12.50,100\n"
        )
        uploaded = SimpleNamespace(name="synthetic.csv", size=len(content), getvalue=lambda: content)
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button[1].click().run()
            self.assertIsNone(app.date_input[0].value)
            self.assertTrue(app.button[2].disabled)
            self.assertFalse(self.path.exists())
            app.date_input[0].set_value(date(2026, 1, 2)).run()
            app.checkbox[1].check().run()
            self.assertFalse(app.button[2].disabled)
            app.button[2].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertIn("Saved 1 account", app.success[0].value)
            self.assertTrue(self.path.exists())
            self.assertEqual(app.dataframe[1].value.iloc[0]["Statement Date"], "2026-01-02")
            app.button[2].click().run()
            self.assertFalse(app.exception)
            self.assertIn("Already saved", app.success[0].value)
            app.date_input[0].set_value(date(2026, 1, 3)).run()
            self.assertFalse(app.success)
            app.button[2].click().run()
            self.assertIn("different statement date", app.error[0].value)
        fresh = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        fresh.button[0].click().run()
        self.assertFalse(fresh.exception)
        self.assertEqual(len(fresh.dataframe[0].value), 1)
        self.assertEqual(fresh.dataframe[0].value.iloc[0]["Quantity"], "12.5")
        self.assertEqual(fresh.dataframe[0].value.iloc[0]["Account"], "DP ****0001 / Client ****0002")
        fresh.selectbox[0].set_value(1).run()
        self.assertFalse(fresh.exception)
        self.assertEqual(fresh.dataframe[0].value.iloc[0]["Statement Date"], "2026-01-02")
        self.assertEqual(Database(self.path).load_holdings(), [])

    def test_statement_date_autofill_and_edit(self) -> None:
        """Prefill the parsed date once, retain edits across reruns, and save the edited date."""
        holdings = (
            b"DP ID:00000001\nClient ID:00000002\nDP Name:Synthetic Broker\n"
            b"ISIN,Quantity\nINE000A01010,1\n"
        )
        content = b"Statement as on : 02-Jan-2026\n" + holdings
        uploaded = SimpleNamespace(name="synthetic.csv", size=len(content), getvalue=lambda: content)
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.date_input[0].value, date(2026, 1, 2))
            self.assertTrue(app.button[2].disabled)
            self.assertFalse(self.path.exists())
            app.date_input[0].set_value(date(2026, 1, 3)).run()
            app.checkbox[0].check().run()
            self.assertEqual(app.date_input[0].value, date(2026, 1, 3))
            app.checkbox[1].check().run()
            app.button[2].click().run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertEqual(Database(self.path).list_snapshots()[0]["statement_date"], "2026-01-03")
            content = b"Statement Date:04-Jan-2026\n" + holdings
            app.run()
            app.button[1].click().run()
            self.assertEqual(app.date_input[0].value, date(2026, 1, 4))
            self.assertFalse(app.success)
            self.assertFalse(app.checkbox[1].value)
            content = holdings
            app.run()
            app.button[1].click().run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.date_input[0].value)

    def test_fund_categories_in_preview_and_saved_holdings(self) -> None:
        """Display both scheme levels before and after saving without changing asset classes."""
        content = (
            b"Statement Date:02-Jan-2026\nDP ID:00000001\nClient ID:00000002\nDP Name:Synthetic Broker\n"
            b"ISIN,ISIN Name,Quantity\n"
            b"INF000A01010,Synthetic Flexi Cap Fund Direct Growth,1\n"
            b"INF000B01010,Synthetic Liquid Fund,2\n"
            b"INF000C01010,Synthetic Arbitrage Fund,3\n"
            b"INF000D01010,Synthetic Opportunities Fund,4\n"
            b"INF000E01010,Synthetic Gold ETF,5\n"
            b"INF000F01010,Synthetic ELSS Tax Saver Nifty LargeMidcap 250 Index Fund Direct Growth,6\n"
        )
        uploaded = SimpleNamespace(name="synthetic.csv", size=len(content), getvalue=lambda: content)
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button[1].click().run()
            self.assertFalse(app.exception)
            preview = app.dataframe[0].value
            self.assertEqual(preview["Fund Category"].tolist(), ["Equity", "Debt", "Hybrid", "Unknown", "-", "Equity"])
            self.assertEqual(preview["Scheme Category"].tolist(), ["Flexi Cap", "Liquid", "Arbitrage", "Unknown", "-", "ELSS"])
            self.assertEqual(preview["Asset Class"].tolist(), ["Mutual Fund"] * 4 + ["Gold ETF", "Mutual Fund"])
            self.assertFalse(self.path.exists())
            app.checkbox[1].check().run()
            app.button[2].click().run()
            self.assertFalse(app.error)
        fresh = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        fresh.button[0].click().run()
        self.assertFalse(fresh.exception)
        saved = fresh.dataframe[0].value
        self.assertEqual(saved["Fund Category"].tolist(), preview["Fund Category"].tolist())
        self.assertEqual(saved["Scheme Category"].tolist(), preview["Scheme Category"].tolist())
        fresh.selectbox[0].set_value(1).run()
        self.assertFalse(fresh.exception)
        self.assertEqual(fresh.dataframe[0].value["Scheme Category"].tolist(), preview["Scheme Category"].tolist())

    def test_unknown_account_cannot_be_saved(self) -> None:
        """Keep ambiguous-account previews usable without permitting unsafe persistence."""
        content = b"ISIN,Quantity\nINE000A01010,1\n"
        uploaded = SimpleNamespace(name="synthetic.csv", size=len(content), getvalue=lambda: content)
        with patch("streamlit.file_uploader", return_value=uploaded):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
            app.button[1].click().run()
            app.date_input[0].set_value(date(2026, 1, 2)).run()
            app.checkbox[1].check().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button[2].disabled)
            self.assertFalse(self.path.exists())

    def test_inr_distribution_renders(self) -> None:
        """Exercise the chart and holdings table with the current width API."""
        database = Database(self.path)
        database.initialize()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO current_holdings VALUES (?, ?, ?, ?, ?, ?)",
                ("DEMO", "TEST", "EQUITY", "INR", "2", "10.50"),
            )
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("plotly_chart")), 1)
        self.assertEqual(len(app.dataframe[0].value), 1)

    def test_broker_display_alias_preserves_provenance(self) -> None:
        """Shorten only the recognized legal name and retain original parsed metadata."""
        from ingestion.parsers import CdslCasParser
        from ui.app import broker_label, cas_preview_frame

        for name in ("GROWW INVEST TECH PRIVATE LIMITED", " groww  invest tech private limited "):
            self.assertEqual(broker_label(name), "Groww")
        for name in ("ZERODHA BROKING LIMITED", " zerodha  broking limited "):
            self.assertEqual(broker_label(name), "Zerodha")
        self.assertEqual(broker_label("Synthetic Broker"), "Synthetic Broker")
        self.assertEqual(broker_label("Groww Unrelated Entity"), "Groww Unrelated Entity")
        self.assertEqual(broker_label(None), "Unknown")
        self.assertEqual(broker_label("  "), "Unknown")
        rows = CdslCasParser().parse_bytes(
            b"DP Name:GROWW INVEST TECH PRIVATE LIMITED\nISIN,Balance\nINE000A01010,1", ".csv",
        )
        self.assertEqual(cas_preview_frame(rows, False).iloc[0]["Broker / DP"], "Groww")
        self.assertEqual(rows[0]["broker"], "GROWW INVEST TECH PRIVATE LIMITED")
        gold_rows = CdslCasParser().parse_bytes(
            b"DP Name:ZERODHA BROKING LIMITED\nISIN,ISIN Name,Balance\n"
            b"INF000A01010,Synthetic AMC#Synthetic MF-Synthetic ETF GOLD,1", ".csv",
        )
        frame = cas_preview_frame(gold_rows, False)
        self.assertEqual(frame.iloc[0]["Broker / DP"], "Zerodha")
        self.assertEqual(frame.iloc[0]["Asset Class"], "Gold ETF")
        self.assertEqual(gold_rows[0]["broker"], "ZERODHA BROKING LIMITED")

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