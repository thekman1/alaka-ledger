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


class AssetClassificationTests(unittest.TestCase):
    """Separate fund units, direct equity and direct debt without network lookups."""

    def test_instrument_descriptions(self) -> None:
        """Use specific markers while keeping fund holdings out of direct debt/equity."""
        from core.asset_classifier import classify_asset

        cases = [
            ("INE000A01010", "Synthetic Limited - EQUITY SHARES", "Stock"),
            ("INF000A01010", "Synthetic Equity Fund", "Mutual Fund"),
            ("INF000A01010", "Synthetic Bond Fund", "Mutual Fund"),
            ("INF000A01010", "Synthetic ETF", "Mutual Fund"),
            ("INF000A01010", "Synthetic AMC LTD#Synthetic MF-Synthetic ETF GOLD", "Gold ETF"),
            ("INF000A01010", "Synthetic Gold Exchange-Traded Fund", "Gold ETF"),
            ("INF000A01010", "Synthetic Gold Fund", "Mutual Fund"),
            ("INF000A01010", "Synthetic Gold ETF Fund of Funds", "Mutual Fund"),
            ("INE000A01010", "Synthetic Gold Mining EQUITY SHARES", "Stock"),
            ("INE000A07010", "Synthetic Limited 7.5% NCD", "Bond"),
            ("IN0000000001", "Sovereign Gold Bond", "Bond"),
            ("INE000A01010", "Synthetic Limited", "Unclassified"),
            ("INE000A01010", "Synthetic REIT", "Unclassified"),
            ("INE000A01010", "Synthetic Preference Shares", "Unclassified"),
            ("INE000A01010", "Synthetic EQUITY BOND", "Unclassified"),
        ]
        for isin, name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(classify_asset(isin, name), expected)

    def test_statement_type_precedence(self) -> None:
        """Respect explicit categories, including unsupported types, over heuristics."""
        from core.asset_classifier import classify_asset

        self.assertEqual(classify_asset("INE000A01010", "Synthetic", "Equity"), "Stock")
        self.assertEqual(classify_asset("INE000A07010", "Synthetic", "Non-Convertible Debenture"), "Bond")
        self.assertEqual(classify_asset("INF000A01010", "Synthetic Bond", "MF"), "Mutual Fund")
        self.assertEqual(classify_asset("INE000A01010", "Synthetic EQUITY", "Warrant"), "Unclassified")
        self.assertEqual(classify_asset("INF000A01010", "Synthetic Gold ETF", "ETF"), "Gold ETF")
        self.assertEqual(classify_asset("INF000A01010", "Synthetic Gold ETF", "Mutual Fund"), "Gold ETF")
        self.assertEqual(classify_asset("INF000A01010", "Synthetic", "Gold ETF"), "Gold ETF")
        self.assertEqual(classify_asset("IN0000000001", "Synthetic Gold", "SGB"), "Bond")


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

    def test_csv_snapshot(self) -> None:
        """Parse exact quantities and statement values, not fabricated trades."""
        content = (
            'Consolidated Account Statement\r\n'
            'ISIN,ISIN Name,Closing Balance,Market Value\r\n'
            'INE000A01010,"Synthetic,\nSecurity","1,234.5678","1,23,456.78"\r\n'
        ).encode("utf-8-sig")
        holdings = CdslCasParser().parse_bytes(content, ".CSV")
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["security_name"], "Synthetic, Security")
        self.assertEqual(holdings[0]["quantity"], Decimal("1234.5678"))
        self.assertEqual(holdings[0]["market_value"], Decimal("123456.78"))
        self.assertIsNone(holdings[0]["price"])
        self.assertNotIn("tx_type", holdings[0])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.csv"
            path.write_bytes(content)
            self.assertEqual(CdslCasParser().parse_file(path), holdings)

    def test_invalid_csv_and_removed_xlsx(self) -> None:
        """Reject invalid encoding, quoting, shifted fields and removed formats."""
        parser = CdslCasParser()
        for content in (
            b'ISIN,Quantity\nINE000A01010,"2',
            b'ISIN,Quantity\nINE000A01010,1,234',
            b'ISIN,Quantity\nINE000A01010,\xff',
            b'ISIN,Quantity\nINE000A01010,2\x00',
            b'ISIN,Quantity\nINE000A01010,=1+1',
        ):
            with self.assertRaises(ParsingError):
                parser.parse_bytes(content, ".csv")
        with self.assertRaises(UnsupportedStatementError):
            parser.parse_bytes(b"not supported", ".xlsx")

    def test_csv_security_names_and_asset_classes(self) -> None:
        """Map CDSL names and explicit security types into typed snapshot categories."""
        content = (
            "ISIN,ISIN Name,Balance,Value,Security Type\n"
            "INE000A01010,Synthetic Stock,2,20,Equity\n"
            "INF000A01010,Synthetic Fund,3,30,Mutual Fund\n"
            "INE000A07010,Synthetic Bond,4,40,Bond\n"
        ).encode()
        rows = CdslCasParser().parse_bytes(content, ".csv")
        self.assertEqual([row["security_name"] for row in rows], ["Synthetic Stock", "Synthetic Fund", "Synthetic Bond"])
        self.assertEqual([row["asset_class"] for row in rows], ["Stock", "Mutual Fund", "Bond"])

    def test_csv_last_closing_price(self) -> None:
        """Use statement closing prices, never nominal paid-up values, as unit prices."""
        content = (
            "ISIN,ISIN Name,Paid Up Value,Balance,Last Closing Price,Value\n"
            "INE000A01010,Synthetic Equity,1,2,123.4567,246.9134\n"
        ).encode()
        holding = CdslCasParser().parse_bytes(content, ".csv")[0]
        self.assertEqual(holding["price"], Decimal("123.4567"))

    def test_csv_account_sections(self) -> None:
        """Keep identical instruments in different accounts separate and preserve ID zeros."""
        content = (
            "DP ID : 00000001\nClient ID : 00000002\nDP Name : Synthetic Broker A\n"
            "ISIN,Balance,Last Closing Price\nINE000A01010,2,10.50\n"
            "DP ID : 00000003\nClient ID : 00000004\nDP Name : Synthetic Broker B\n"
            "ISIN,Balance,Last Closing Price\nINE000A01010,3,10.50\n"
        ).encode()
        rows = CdslCasParser().parse_bytes(content, ".csv")
        self.assertEqual([row["broker"] for row in rows], ["Synthetic Broker A", "Synthetic Broker B"])
        self.assertEqual([row["account_id"] for row in rows], ["demat:00000001:00000002", "demat:00000003:00000004"])
        self.assertEqual([row["dp_id"] for row in rows], ["00000001", "00000003"])

    def test_missing_account_identity_does_not_leak(self) -> None:
        """Never invent identity or copy a client from a preceding section or parse."""
        content = (
            "DP ID:00000001\nClient ID:00000002\nDP Name:Synthetic Broker\n"
            "ISIN,Balance\nINE000A01010,2\n"
            "DP ID:00000003\nISIN,Balance\nINE000A01010,3\n"
        ).encode()
        parser = CdslCasParser()
        rows = parser.parse_bytes(content, ".csv")
        self.assertIsNone(rows[1]["client_id"])
        self.assertIsNone(rows[1]["account_id"])
        self.assertIsNone(rows[1]["broker"])
        fresh = parser.parse_bytes(b"ISIN,Balance\nINE000A01010,1", ".csv")
        self.assertIsNone(fresh[0]["account_id"])

    def test_explicit_row_accounts_and_invalid_identity(self) -> None:
        """Recognize row-level identities and reject malformed account identifiers."""
        parser = CdslCasParser()
        content = (
            "ISIN,Balance,DP Name,DP ID,Client ID\n"
            "INE000A01010,2,Synthetic Broker,IN000001,00000002\n"
        ).encode()
        row = parser.parse_bytes(content, ".csv")[0]
        self.assertEqual(row["account_id"], "demat:IN000001:00000002")
        with self.assertRaises(ParsingError):
            parser.parse_bytes(b"DP ID:invalid\nISIN,Balance\nINE000A01010,2", ".csv")

    def test_inline_account_metadata_boundaries(self) -> None:
        """Stop account IDs at adjacent labels without storing personal metadata."""
        from ingestion.account_context import AccountContext

        account = AccountContext()
        account.consume(["DP ID: 00000001 Account Status: Active"])
        account.consume(["Client ID: 00000002 Category: Regular"])
        account.consume(["DP Name:", "Synthetic Broker"])
        self.assertEqual(account.snapshot({})["account_id"], "demat:00000001:00000002")
        self.assertEqual(account.snapshot({})["broker"], "Synthetic Broker")

    def test_ambiguous_pdf_identity_is_unknown(self) -> None:
        """Do not guess an account from IDs embedded in unrelated extracted text."""
        from ingestion.account_context import AccountContext

        account = AccountContext(strict=False)
        account.consume(["DP ID: 00000001 Client ID: 00000002"])
        account.consume(["DP ID: Unrelated label: 00000003"])
        self.assertIsNone(account.snapshot({})["account_id"])
        self.assertIsNone(account.snapshot({})["client_id"])

    def test_ruled_pdf_accounts_follow_table_order(self) -> None:
        """Bind account labels outside ruled tables to the following holdings section."""
        from unittest.mock import patch

        from ingestion.parsers import _parse_rows, _pdf_rows

        first = Mock(bbox=(0, 100, 600, 200))
        first.extract.return_value = [["ISIN", "Balance"], ["INE000A01010", "2"]]
        second = Mock(bbox=(0, 300, 600, 400))
        second.extract.return_value = [["ISIN", "Balance"], ["INE000A01010", "3"]]
        page = Mock(bbox=(0, 0, 600, 600))
        page.find_tables.return_value = [second, first]
        metadata = [
            [["DP ID:00000001"], ["Client ID:00000002"], ["DP Name:Synthetic Broker A"]],
            [["DP ID:00000003"], ["Client ID:00000004"], ["DP Name:Synthetic Broker B"]],
            [],
        ]
        with patch("ingestion.parsers._pdf_text_rows", side_effect=metadata):
            holdings = _parse_rows(_pdf_rows(page), "Page 1")
        self.assertEqual([row["account_id"] for row in holdings], ["demat:00000001:00000002", "demat:00000003:00000004"])
        self.assertEqual([row["broker"] for row in holdings], ["Synthetic Broker A", "Synthetic Broker B"])

    def test_csv_dimension_limits(self) -> None:
        """Apply bounded CSV row and column limits without large fixtures."""
        from unittest.mock import patch

        parser = CdslCasParser()
        too_wide = (",".join(["column"] * 101)).encode()
        with self.assertRaises(ParsingError):
            parser.parse_bytes(too_wide, ".csv")
        with patch("ingestion.parsers._MAX_ROWS", 2):
            with self.assertRaises(ParsingError):
                parser.parse_bytes(b"ISIN,Quantity\nINE000A01010,2\nINE000A01010,3", ".csv")

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
        """Return sanitized failures for unknown formats, invalid tables, and oversized input."""
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
            b"BT /F1 10 Tf 30 285 Td (DP ID: 00000001) Tj "
            b"0 -15 Td (Client ID: 00000002) Tj "
            b"0 -15 Td (DP Name: Synthetic Broker) Tj "
            b"0 -25 Td (ISIN            Security Name          Quantity     Market Value) Tj "
            b"0 -20 Td (INE000A01010    Synthetic Security     12.50        1,250.00) Tj ET"
        )
        page[NameObject("/Contents")] = content
        plain = BytesIO()
        writer.write(plain)
        result = CdslCasParser().parse_bytes(plain.getvalue(), ".pdf")
        self.assertEqual(result[0]["quantity"], Decimal("12.50"))
        self.assertEqual(result[0]["market_value"], Decimal("1250.00"))
        self.assertEqual(result[0]["account_id"], "demat:00000001:00000002")
        self.assertEqual(result[0]["broker"], "Synthetic Broker")
        writer.encrypt("synthetic-password", algorithm="AES-256")
        encrypted = BytesIO()
        writer.write(encrypted)
        with self.assertRaises(ParsingError):
            CdslCasParser().parse_bytes(encrypted.getvalue(), ".pdf", "wrong")
        parsed = CdslCasParser().parse_bytes(encrypted.getvalue(), ".pdf", "synthetic-password")
        self.assertEqual(parsed, result)


if __name__ == "__main__":
    unittest.main()