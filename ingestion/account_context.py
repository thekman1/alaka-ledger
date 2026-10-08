"""Section-scoped demat identity extraction without storing holder personal details."""

import re
from typing import Dict, Iterator, Optional, Sequence, Tuple

from ingestion.base_parser import ParsedAccount, ParsingError


_FIELDS = {"dpid": "dp_id", "clientid": "client_id", "dpname": "broker", "broker": "broker", "brokername": "broker"}
_LABEL = re.compile(r"(?<!\w)(DP\s*ID|Client\s*ID|DP\s*Name|Broker(?:\s*Name)?)\s*:", re.IGNORECASE)


def _field(label: str) -> Optional[str]:
    """Map a known metadata label to an account field."""
    return _FIELDS.get(re.sub(r"[^a-z]", "", label.lower()))


def _cell_metadata(cells: Sequence[str], index: int) -> Iterator[Tuple[str, str]]:
    """Extract known labels from one cell, allowing a separate following value cell."""
    cell = cells[index]
    following = cells[index + 1].strip() if index + 1 < len(cells) else ""
    matches = list(_LABEL.finditer(cell))
    if not matches:
        field = _field(cell)
        if field is not None:
            yield field, following
        return
    for position, match in enumerate(matches):
        field = _field(match.group(1))
        end = matches[position + 1].start() if position + 1 < len(matches) else len(cell)
        value = cell[match.end():end].strip() or following
        if field is not None:
            yield field, value


def account_metadata(cells: Sequence[str]) -> Dict[str, str]:
    """Read labelled metadata, rejecting conflicting values within a row."""
    values: Dict[str, str] = {}
    for index in range(len(cells)):
        for field, value in _cell_metadata(cells, index):
            if field in values and values[field] != value:
                raise ParsingError("Conflicting account metadata was found.")
            values[field] = value
    return values


def _identifier(value: str, pattern: str, label: str) -> Optional[str]:
    """Read a complete ID followed only by the end of a field or another label."""
    value = value.strip().upper()
    if not value:
        return None
    match = re.match(r"(" + pattern + r")(?=$|\s+[A-Z][^:]*:)", value)
    if match is None:
        raise ParsingError(f"The statement contains an invalid {label}.")
    return match.group(1)


def account_record(values: Dict[str, str]) -> ParsedAccount:
    """Validate identifiers as text, preserving leading zeros and missing values."""
    dp_id = _identifier(values.get("dp_id", ""), r"(?:\d{8}|IN\d{6})", "DP ID")
    client_id = _identifier(values.get("client_id", ""), r"\d{8}", "client ID")
    broker = values.get("broker", "").strip() or None
    if broker is not None and (len(broker) > 200 or ":" in broker):
        raise ParsingError("The statement contains ambiguous broker metadata.")
    return ParsedAccount(
        broker=broker, dp_id=dp_id, client_id=client_id,
        account_id=f"demat:{dp_id}:{client_id}" if dp_id and client_id else None,
    )


class AccountContext:
    """Carry identity within sections; repeated labels start a fresh account section."""

    def __init__(self, *, strict: bool = True) -> None:
        """Begin with unknown account identity."""
        self._values: Dict[str, str] = {}
        self._strict = strict

    def consume(self, cells: Sequence[str]) -> bool:
        """Update context from metadata rows without retaining previous-section IDs."""
        values = account_metadata(cells)
        if not values:
            return False
        if self._values.keys() & values.keys():
            self._values = {}
        self._values.update(values)
        try:
            account_record(self._values)
        except ParsingError:
            self._values = {}
            if self._strict:
                raise
        return True

    def snapshot(self, row: Dict[str, str]) -> ParsedAccount:
        """Prefer explicit row-level identity without filling its gaps from another account."""
        explicit = {field: row[field] for field in ("broker", "dp_id", "client_id") if field in row}
        return account_record(explicit if explicit else self._values)