"""Local CAS snapshot extraction and an explicit IBKR transaction adapter stub."""

import csv
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Optional, Sequence

from core.asset_classifier import classify_asset
from ingestion.account_context import AccountContext, account_metadata
from ingestion.base_parser import (
    BaseParser,
    ParsedHolding,
    ParsedTransaction,
    ParserNotImplementedError,
    ParsingError,
    UnsupportedStatementError,
)

if TYPE_CHECKING:
    from pdfplumber.page import Page


_MAX_BYTES = 20 * 1024 * 1024
_MAX_ROWS = 50_000
_ISIN = re.compile(r"\bIN[A-Z0-9]{10}\b")
_STATEMENT_DATE_LABEL = re.compile(r"\bstatement\s+(?:as\s+(?:on|of)|date)\s*:?\s*", re.IGNORECASE)
_HEADERS = {
    "isin": {"isin", "isincode"},
    "security_name": {"security", "securityname", "nameofsecurity", "companyname", "company", "description", "schemename", "nameofthecompany", "nameofcompany", "nameofthecompanysecurity", "isinname"},
    "quantity": {"quantity", "qty", "units", "balance", "closingbalance", "closingquantity", "currentbalance", "numberofunits", "noofshares", "noofunits", "numberofshares", "totalbalance"},
    "price": {"price", "marketprice", "unitprice", "nav", "rate", "pricers", "navrs", "lastclosingprice"},
    "market_value": {"value", "valuation", "marketvalue", "currentvalue", "valueinrs", "valuers", "valuationrs", "marketvaluers", "currentvaluation"},
    "native_currency": {"currency", "nativecurrency"},
    "asset_class": {"assetclass", "securitytype", "instrumenttype"},
    "broker": {"broker", "brokername", "dpname"},
    "dp_id": {"dpid"},
    "client_id": {"clientid"},
}


def _text(value: object) -> str:
    """Normalize whitespace in extracted cells without logging source data."""
    return " ".join(str(value).split()) if value is not None else ""


def _header(value: str) -> str:
    """Match headings independent of case, punctuation and line wrapping."""
    return re.sub(r"[^a-z]", "", value.lower())


def _columns(cells: Sequence[str]) -> Dict[str, int]:
    """Recognize only unambiguous holdings headings, never transaction headings."""
    columns: Dict[str, int] = {}
    for index, cell in enumerate(cells):
        for field, aliases in _HEADERS.items():
            if _header(cell) in aliases:
                if field in columns:
                    return {}
                columns[field] = index
    return columns if {"isin", "quantity"} <= columns.keys() else {}


def _transaction_header(cells: Sequence[str]) -> bool:
    """Identify transaction sections so their quantities never become snapshots."""
    headers = {_header(cell) for cell in cells}
    return bool(headers & {"transactiondate", "tradedate", "transactiontype", "credit", "debit", "narration"}) or (
        "date" in headers and bool(headers & {"isin", "isincode"})
    )


def _number(value: str, *, required: bool = False) -> Optional[Decimal]:
    """Parse plain or Indian-grouped nonnegative decimals without float math."""
    if value in {"", "-", "--", "N/A"} and not required:
        return None
    cleaned = re.sub(r"^(?:INR|Rs\.?)\s*", "", value, flags=re.IGNORECASE).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?", cleaned):
        raise ParsingError("A holdings row contains an invalid or missing numeric value.")
    try:
        number = Decimal(cleaned.replace(",", ""))
    except InvalidOperation:
        raise ParsingError("A holdings row contains an invalid numeric value.") from None
    if not number.is_finite() or number < 0:
        raise ParsingError("A holdings row contains an invalid numeric value.")
    return number


def _labelled_date(text: str) -> Optional[date]:
    """Read explicit day-first or ISO dates without guessing transaction or print dates."""
    tokens = text.replace(",", " ").split()
    if not tokens:
        return None
    for candidate in (tokens[0], " ".join(tokens[:3])):
        for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(candidate, pattern).date()
            except ValueError:
                continue
            return parsed if date(1900, 1, 1) <= parsed <= date.today() else None
    return None


class _StatementDateContext:
    """Resolve repeated explicit labels only when they agree on one valid date."""

    def __init__(self) -> None:
        self.dates: set[Optional[date]] = set()

    def consume(self, cells: Sequence[str]) -> bool:
        """Inspect metadata rows without confusing security descriptions with labels."""
        text = " ".join(cells)
        if _ISIN.search(text):
            return False
        labels = list(_STATEMENT_DATE_LABEL.finditer(text))
        for label in labels:
            self.dates.add(_labelled_date(text[label.end():]))
        return bool(labels)

    @property
    def value(self) -> Optional[date]:
        """Leave missing, invalid or conflicting metadata for explicit user input."""
        return next(iter(self.dates)) if len(self.dates) == 1 else None


def _parse_rows(
    rows: Iterable[Sequence[object]], source: str, account: Optional[AccountContext] = None,
    statement_dates: Optional[_StatementDateContext] = None,
) -> List[ParsedHolding]:
    """Extract rows under recognized headers; reject ambiguous ISIN-bearing rows."""
    holdings: List[ParsedHolding] = []
    columns: Dict[str, int] = {}
    previous: List[str] = []
    transaction_section = False
    account = account if account is not None else AccountContext()
    for row_index, row in enumerate(rows):
        if row_index >= _MAX_ROWS:
            raise ParsingError("The statement exceeds the supported row limit.")
        cells = [_text(cell) for cell in row]
        if statement_dates is not None and statement_dates.consume(cells):
            account.consume(cells)
            continue
        if not _columns(cells) and account.consume(cells):
            columns = {}
            previous = []
            transaction_section = False
            continue
        if _transaction_header(cells):
            columns = {}
            previous = []
            transaction_section = True
            continue
        recognized = _columns(cells)
        if not recognized and previous and len(previous) == len(cells):
            recognized = _columns([f"{above} {below}" for above, below in zip(previous, cells)])
        if recognized:
            columns = recognized
            previous = []
            transaction_section = False
            continue
        if any(_header(cell) in {"isin", "isincode"} for cell in cells):
            columns = {}
        previous = cells
        if transaction_section:
            continue
        matches = _ISIN.findall(" ".join(cells))
        if not matches:
            if columns and columns["isin"] < len(cells):
                identifier = cells[columns["isin"]]
                quantity_text = cells[columns["quantity"]] if columns["quantity"] < len(cells) else ""
                total_row = any(_header(cell) in {"total", "subtotal", "grandtotal"} for cell in cells)
                if not total_row and (identifier or quantity_text):
                    raise ParsingError("A holdings row is missing a valid ISIN. No partial preview was accepted.")
            continue
        if not columns or len(matches) != 1 or max(columns.values()) >= len(cells):
            raise ParsingError("An ISIN row has no unambiguous holdings columns. This layout is not supported.")
        values = {field: cells[index] for field, index in columns.items()}
        if not _ISIN.fullmatch(values["isin"]):
            raise ParsingError("A holdings row has an invalid ISIN column.")
        quantity = _number(values["quantity"], required=True)
        if quantity is None:
            raise ParsingError("A holdings row is missing its quantity.")
        currency = values.get("native_currency", "INR").upper()
        if currency != "INR":
            raise ParsingError("Only INR-denominated CAS holdings are supported.")
        holdings.append(ParsedHolding(
            **account.snapshot(values),
            isin=values["isin"], security_name=values.get("security_name", ""),
            asset_class=classify_asset(values["isin"], values.get("security_name", ""), values.get("asset_class", "")),
            quantity=quantity, price=_number(values.get("price", "")),
            market_value=_number(values.get("market_value", "")),
            native_currency=currency, source=source,
        ))
    return holdings


def _validate_file(file_path: Path, suffix: str) -> None:
    """Validate only file metadata, without reading or logging private contents."""
    try:
        valid = file_path.suffix.lower() == suffix and file_path.is_file()
    except OSError:
        raise UnsupportedStatementError("The statement file is inaccessible.") from None
    if not valid:
        raise UnsupportedStatementError("The statement file is missing or has an unsupported format.")


def _pdf_text_rows(page: "Page") -> List[Sequence[object]]:
    """Use word coordinates to separate columns without relying on rendered spaces."""
    rows: List[Sequence[object]] = []
    cells: List[str] = []
    line_top: Optional[float] = None
    previous_right = 0.0
    previous_width = 0.0
    for word in sorted(page.extract_words(x_tolerance=1), key=lambda item: (item["top"], item["x0"])):
        top = float(word["top"])
        left = float(word["x0"])
        right = float(word["x1"])
        text = str(word["text"])
        if line_top is None or abs(top - line_top) > 3:
            if cells:
                rows.append(cells)
            cells = [text]
            line_top = top
        elif left - previous_right > max(8, previous_width * 1.8):
            cells.append(text)
        else:
            cells[-1] += " " + text
        previous_right = right
        previous_width = (right - left) / max(1, len(text))
    if cells:
        rows.append(cells)
    return rows


def _pdf_metadata(row: Sequence[object]) -> bool:
    """Retain account and statement-date labels surrounding ruled tables."""
    cells = [_text(cell) for cell in row]
    return bool(account_metadata(cells) or _STATEMENT_DATE_LABEL.search(" ".join(cells)))


def _pdf_rows(page: "Page") -> List[Sequence[object]]:
    """Interleave table rows and nearby account labels in page reading order."""
    tables = sorted(page.find_tables(), key=lambda table: table.bbox[1])
    if not any(_columns([_text(cell) for cell in row]) for table in tables for row in table.extract()):
        return _pdf_text_rows(page)
    rows: List[Sequence[object]] = []
    top = page.bbox[1]
    for table in tables:
        if table.bbox[1] < top:
            raise ParsingError("Overlapping PDF tables cannot be assigned to accounts safely.")
        if table.bbox[1] > top:
            region = page.crop((page.bbox[0], top, page.bbox[2], table.bbox[1]))
            rows.extend(row for row in _pdf_text_rows(region) if _pdf_metadata(row))
        rows.extend(table.extract())
        top = table.bbox[3]
    if top < page.bbox[3]:
        region = page.crop((page.bbox[0], top, page.bbox[2], page.bbox[3]))
        rows.extend(row for row in _pdf_text_rows(region) if _pdf_metadata(row))
    return rows


def _csv_rows(reader: Iterable[List[str]]) -> Iterator[List[str]]:
    """Bound CSV dimensions and reject shifted columns in holdings rows."""
    width: Optional[int] = None
    for row in reader:
        if len(row) > 100 or any("\x00" in cell for cell in row):
            raise ParsingError("The CSV has too many columns or contains invalid text.")
        if _transaction_header(row):
            width = None
        elif _columns(row):
            width = len(row)
        elif width is not None and any(_ISIN.search(cell) for cell in row) and len(row) != width:
            raise ParsingError("A CSV holdings row has inconsistent columns. Quote values containing commas.")
        yield row


class CdslCasParser(BaseParser[ParsedHolding]):
    """Read tabular CAS snapshots locally, preserving statement valuation fields.

    Supports text-based PDF tables and CSV holdings exports with ISIN and closing
    quantity headings. Scanned PDFs and unknown layouts are rejected, not guessed.
    Uploaded bytes and PDF passwords are never written to disk by this parser.
    """

    def __init__(self) -> None:
        self.statement_date: Optional[date] = None

    def parse_file(self, file_path: Path) -> List[ParsedHolding]:
        """Read a bounded local PDF/CSV; use parse_bytes for password-protected PDFs."""
        suffix = file_path.suffix.lower()
        if suffix not in {".pdf", ".csv"}:
            raise UnsupportedStatementError("Select a PDF or CSV CAS statement.")
        _validate_file(file_path, suffix)
        try:
            with file_path.open("rb") as document:
                content = document.read(_MAX_BYTES + 1)
        except OSError:
            raise ParsingError("The statement could not be read.") from None
        return self.parse_bytes(content, suffix)

    def parse_bytes(self, content: bytes, suffix: str, password: Optional[str] = None) -> List[ParsedHolding]:
        """Parse an in-memory upload; sanitize third-party exceptions at the boundary."""
        self.statement_date = None
        statement_dates = _StatementDateContext()
        if not content or len(content) > _MAX_BYTES:
            raise ParsingError("Select a nonempty statement no larger than 20 MiB.")
        try:
            if suffix.lower() == ".pdf":
                holdings = self._pdf(content, password, statement_dates)
            elif suffix.lower() == ".csv":
                holdings = self._csv(content, statement_dates)
            else:
                raise UnsupportedStatementError("Select a PDF or CSV CAS statement.")
        except ParsingError:
            raise
        except Exception:
            raise ParsingError("The statement could not be parsed. Check its format and PDF password.") from None
        if not holdings:
            raise ParsingError("No supported holdings table found. Scanned PDFs require OCR; unknown layouts are not imported.")
        self.statement_date = statement_dates.value
        return holdings

    @staticmethod
    def _csv(content: bytes, statement_dates: _StatementDateContext) -> List[ParsedHolding]:
        """Read UTF-8 CSV, optionally with a BOM, without evaluating formulas."""
        try:
            with StringIO(content.decode("utf-8-sig"), newline="") as stream:
                return _parse_rows(_csv_rows(csv.reader(stream, strict=True)), "CSV", statement_dates=statement_dates)
        except (UnicodeDecodeError, csv.Error):
            raise ParsingError("The CSV must be valid UTF-8 comma-separated text with correctly quoted fields.") from None

    @staticmethod
    def _pdf(content: bytes, password: Optional[str], statement_dates: _StatementDateContext) -> List[ParsedHolding]:
        """Decrypt in memory and extract ruled or whitespace-aligned text tables."""
        import pdfplumber
        from pypdf import PdfReader, PdfWriter

        if not content.startswith(b"%PDF-"):
            raise ParsingError("The selected file is not a PDF document.")
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise ParsingError("The PDF password is missing or incorrect.")
        if len(reader.pages) > 200:
            raise ParsingError("The PDF exceeds the supported page limit.")
        decrypted = BytesIO()
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.write(decrypted)
        decrypted.seek(0)
        holdings: List[ParsedHolding] = []
        account = AccountContext(strict=False)
        with pdfplumber.open(decrypted) as document:
            for page_index, page in enumerate(document.pages, start=1):
                source = f"Page {page_index}"
                holdings.extend(_parse_rows(_pdf_rows(page), source, account, statement_dates))
        return holdings


class IbkrFlexParser(BaseParser[ParsedTransaction]):
    """Future offline IBKR/Paasa Flex XML adapter; no tokens or API calls."""

    def parse_file(self, file_path: Path) -> List[ParsedTransaction]:
        """Validate an XML path and raise ParserNotImplementedError for this stub."""
        _validate_file(file_path, ".xml")
        raise ParserNotImplementedError("IBKR Flex parsing is not implemented yet.")