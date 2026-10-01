"""Turn an ITSM export into a ticket set.

EasyVista gives us exports, not an API: CSV (comma or semicolon, UTF-8 or
Windows-1252), XLSX, JSON or JSONL. Reading, HTML cleaning and masking are
shared with the engine (kecore.tables, kecore.text). Ticket text is cleaned of
HTML and, by default, of e-mail addresses and phone numbers. Names of people
are not detected: check a sample before sharing anything outside clients-local/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kecore.tables import (
    cell_to_str,
    decode_bytes,
    guess_delimiter,
    normalize_header,
    read_rows,
    resolve_column,
)
from kecore.text import EMAIL_RE, PHONE_RE, Scrubber, clean_text

from .dataset import DatasetError, Ticket, parse_expected
from .ids import strip_document_path

# Re-exported: the labeling sheet and the tests read exports through this module.
__all__ = [
    "EMAIL_RE", "PHONE_RE", "ImportReport", "Scrubber", "cell_to_str", "clean_text", "decode_bytes",
    "guess_delimiter", "import_tickets", "normalize_header", "read_rows", "resolve_column",
]


@dataclass
class ImportReport:
    rows: int = 0
    imported: int = 0
    skipped_empty: int = 0
    skipped_no_id: int = 0
    duplicates: int = 0
    labeled: int = 0
    masked: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)


def import_tickets(
    path: str | Path,
    client: str,
    text_cols: list[str],
    id_col: str | None = None,
    category_col: str | None = None,
    expected_col: str | None = None,
    expected_transform: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    sheet: str | None = None,
    scrub: bool = True,
    scrub_patterns: list[str] | None = None,
    limit: int | None = None,
) -> tuple[list[Ticket], ImportReport]:
    client = client.strip()
    if not client:
        raise DatasetError("--client is required")
    if not text_cols:
        raise DatasetError("at least one text column is required")
    if expected_transform not in (None, "basename"):
        raise DatasetError(f"unknown expected transform {expected_transform!r}")

    headers, rows, info = read_rows(path, delimiter, encoding, sheet)
    text_columns = [resolve_column(headers, c, "text") for c in text_cols]
    id_column = resolve_column(headers, id_col, "id") if id_col else None
    category_column = resolve_column(headers, category_col, "category") if category_col else None
    expected_column = resolve_column(headers, expected_col, "expected") if expected_col else None
    scrubber = Scrubber(scrub_patterns or []) if scrub else None

    report = ImportReport(rows=len(rows), source=info)
    tickets: list[Ticket] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, 1):
        parts = [clean_text(cell_to_str(row.get(column))) for column in text_columns]
        text = "\n".join(part for part in parts if part)
        if not text:
            report.skipped_empty += 1
            continue
        if scrubber is not None:
            text = scrubber(text)
        ticket_id = cell_to_str(row.get(id_column)).strip() if id_column else f"{client}-{index:04d}"
        if not ticket_id:
            report.skipped_no_id += 1
            continue
        if ticket_id in seen:
            report.duplicates += 1
            continue
        seen.add(ticket_id)
        category = None
        if category_column:
            category = clean_text(cell_to_str(row.get(category_column))) or None
        expected = None
        if expected_column:
            try:
                expected = parse_expected(row.get(expected_column))
            except DatasetError as exc:
                raise DatasetError(f"row {index}: {exc}") from None
            if expected and expected_transform == "basename":
                expected = list(dict.fromkeys(strip_document_path(e) for e in expected))
        tickets.append(Ticket(ticket_id=ticket_id, client=client, text=text, category=category, expected=expected))
        if expected is not None:
            report.labeled += 1
        if limit and len(tickets) >= limit:
            break
    report.imported = len(tickets)
    report.masked = dict(scrubber.counts) if scrubber is not None else {}
    return tickets, report
