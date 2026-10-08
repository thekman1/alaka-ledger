"""Deterministic service tests with synthetic inputs and no external requests."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from core.forex_engine import ForexEngine, ForexUnavailableError, FxQuote
from core.portfolio_engine import format_money, summarize_holdings
from ingestion.base_parser import ParserNotImplementedError, ParsingError, UnsupportedStatementError
from ingestion.parsers import CdslCasParser, IbkrFlexParser


class ForexTests(unittest.TestCase):
    """Verify consent, request cooldown, quote validation, and bounded fallback."""

    def setUp(self) -> None:
        """Provide a deterministic clock and synthetic quote fetcher."""
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.clock = Mock(return_value=self.now)
        self.fetcher = Mock(return_value=FxQuote(Decimal("85.25"), self.now))

    def test_network_is_opt_in(self) -> None:
        """Perform no requests without explicit opt-in."""
        result = ForexEngine(fetcher=self.fetcher).usd_inr()
        self.assertEqual(result.status, "disabled")
        self.fetcher.assert_not_called()

    def test_cache_stale_fallback_and_expiry(self) -> None:
        """Reuse quotes, label offline fallback stale, and reject expired data."""
        engine = ForexEngine(allow_network=True, fetcher=self.fetcher, clock=self.clock)
        self.assertEqual(engine.usd_inr().status, "fresh")
        self.assertEqual(engine.usd_inr().status, "cached")
        self.fetcher.assert_called_once()
        self.clock.return_value = self.now + timedelta(minutes=16)
        self.fetcher.side_effect = ForexUnavailableError("offline")
        result = engine.usd_inr()
        self.assertEqual(result.status, "stale")
        self.assertIsNotNone(result.quote)
        self.clock.return_value = self.now + timedelta(days=8)
        self.assertIsNone(engine.usd_inr().quote)

    def test_failed_fetch_has_cooldown(self) -> None:
        """Avoid retry storms when an empty cache cannot be populated."""
        self.fetcher.side_effect = ForexUnavailableError("offline")
        engine = ForexEngine(allow_network=True, fetcher=self.fetcher, clock=self.clock)
        self.assertEqual(engine.usd_inr().status, "unavailable")
        self.assertEqual(engine.usd_inr().status, "unavailable")
        self.fetcher.assert_called_once()

    def test_invalid_quotes_are_rejected(self) -> None:
        """Reject nonfinite, nonpositive, future, naive, and expired quotes."""
        quotes = [
            FxQuote(Decimal("NaN"), self.now),
            FxQuote(Decimal("0"), self.now),
            FxQuote(Decimal("85"), self.now + timedelta(hours=1)),
            FxQuote(Decimal("85"), self.now.replace(tzinfo=None)),
            FxQuote(Decimal("85"), self.now - timedelta(days=8)),
        ]
        for quote in quotes:
            with self.subTest(quote=quote):
                self.fetcher.return_value = quote
                engine = ForexEngine(allow_network=True, fetcher=self.fetcher, clock=self.clock)
                self.assertIsNone(engine.usd_inr().quote)


class PortfolioTests(unittest.TestCase):
    """Verify exact totals and refuse partial currency conversion."""

    def test_exact_cost_and_formatting(self) -> None:
        """Keep decimals exact until final half-even display rounding."""
        rows = [dict(total_quantity="0.1", avg_buy_price="0.2", native_currency="USD", asset_class="EQUITY")]
        result = summarize_holdings(rows, Decimal("85.25"))
        self.assertEqual(result.total_cost_inr, Decimal("1.7050"))
        self.assertEqual(format_money(Decimal("1.7050"), "INR"), "INR 1.70")

    def test_unknown_or_unavailable_fx_blocks_total(self) -> None:
        """Do not mistake incomplete valuations for a complete portfolio."""
        for currency in ("USD", "EUR"):
            result = summarize_holdings([
                dict(total_quantity="1", avg_buy_price="10", native_currency=currency, asset_class="EQUITY")
            ])
            self.assertIsNone(result.total_cost_inr)
            self.assertEqual(result.cost_by_currency[currency], Decimal("10"))
            self.assertEqual(result.cost_by_asset_inr, {})


class ParserTests(unittest.TestCase):
    """Assert unfinished adapters cannot claim an empty successful import."""

    def test_missing_file(self) -> None:
        """Use a sanitized custom exception for invalid paths."""
        parser = CdslCasParser()
        missing = Path("missing.pdf")
        with self.assertRaises(UnsupportedStatementError):
            parser.parse_file(missing)

    def test_stubs_fail_explicitly(self) -> None:
        """Keep IBKR explicitly unsupported and reject empty CAS input."""
        with TemporaryDirectory() as directory:
            for parser, suffix, error in (
                (CdslCasParser(), ".pdf", ParsingError),
                (IbkrFlexParser(), ".xml", ParserNotImplementedError),
            ):
                path = Path(directory) / f"synthetic{suffix}"
                path.touch()
                with self.assertRaises(error):
                    parser.parse_file(path)

    def test_xlsx_snapshot(self) -> None:
        """Parse exact quantities and statement values, not fabricated trades."""
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Consolidated Account Statement"])
        sheet.append(["ISIN", "Security Name", "Closing Balance", "Market Value"])
        sheet.append(["INE000A01010", "Synthetic Security", "1,234.5678", "1,23,456.78"])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        holdings = CdslCasParser().parse_bytes(stream.getvalue(), ".xlsx")
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["quantity"], Decimal("1234.5678"))
        self.assertEqual(holdings[0]["market_value"], Decimal("123456.78"))
        self.assertIsNone(holdings[0]["price"])
        self.assertNotIn("tx_type", holdings[0])

    def test_invalid_holdings_rows_fail_closed(self) -> None:
        """Never accept a partial result when a candidate row is malformed."""
        from ingestion.parsers import _parse_rows

        for quantity in ("=1+1", "NaN", "-2", "1,2", ""):
            with self.subTest(quantity=quantity):
                rows = [["ISIN", "Quantity"], ["INE000A01010", "2"], ["INE000A01010", quantity]]
                with self.assertRaises(ParsingError):
                    _parse_rows(rows, "Sheet 1")

    def test_mixed_tables_and_repeated_headers(self) -> None:
        """Ignore explicitly identified transactions while retaining separate snapshot rows."""
        from ingestion.parsers import _parse_rows

        rows = [
            ["ISIN", "Date", "Quantity"], ["INE000A01010", "2026-01-01", "999"],
            ["ISIN", "Quantity"], ["INE000A01010", "2"],
            ["ISIN", "Quantity"], ["INE000A01010", "3"], ["Total", "5"],
        ]
        result = _parse_rows(rows, "Sheet 1")
        self.assertEqual([row["quantity"] for row in result], [Decimal("2"), Decimal("3")])

    def test_invalid_isin_and_ambiguous_headers(self) -> None:
        """Reject malformed identifiers and conflicting quantity columns."""
        from ingestion.parsers import _parse_rows

        for rows in (
            [["ISIN", "Quantity"], ["INVALID", "2"]],
            [["ISIN", "Quantity"], ["", "2"]],
            [["ISIN", "Quantity", "Closing Balance"], ["INE000A01010", "2", "3"]],
        ):
            with self.assertRaises(ParsingError):
                _parse_rows(rows, "Sheet 1")

    def test_corrupt_and_unsupported_uploads(self) -> None:
        """Return sanitized failures for unknown formats, broken ZIPs, and oversized input."""
        parser = CdslCasParser()
        for content, suffix in ((b"broken", ".xlsx"), (b"broken", ".csv"), (b"", ".pdf")):
            with self.assertRaises(ParsingError):
                parser.parse_bytes(content, suffix)
        oversized = b"x" * (20 * 1024 * 1024 + 1)
        with self.assertRaises(ParsingError):
            parser.parse_bytes(oversized, ".pdf")

    def test_pdf_snapshot_and_password(self) -> None:
        """Exercise actual PDF text extraction and password rejection in memory."""
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=650, height=300)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Courier"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        content = DecodedStreamObject()
        content.set_data(
            b"BT /F1 10 Tf 30 250 Td (ISIN            Security Name          Quantity     Market Value) Tj "
            b"0 -20 Td (INE000A01010    Synthetic Security     12.50        1,250.00) Tj ET"
        )
        page[NameObject("/Contents")] = content
        plain = BytesIO()
        writer.write(plain)
        result = CdslCasParser().parse_bytes(plain.getvalue(), ".pdf")
        self.assertEqual(result[0]["quantity"], Decimal("12.50"))
        self.assertEqual(result[0]["market_value"], Decimal("1250.00"))
        writer.encrypt("synthetic-password", algorithm="AES-256")
        encrypted = BytesIO()
        writer.write(encrypted)
        with self.assertRaises(ParsingError):
            CdslCasParser().parse_bytes(encrypted.getvalue(), ".pdf", "wrong")
        parsed = CdslCasParser().parse_bytes(encrypted.getvalue(), ".pdf", "synthetic-password")
        self.assertEqual(parsed, result)


if __name__ == "__main__":
    unittest.main()