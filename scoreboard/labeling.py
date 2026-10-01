"""The labeling sheet: the one manual step of the scoreboard.

prepare-labels writes one row per ticket with the engine's top candidates, so
labeling a ticket is usually typing a digit. apply-labels reads the sheet back.

In the 'expected' column type:

* 1 to 5: the candidate in that column is the right fiche;
* a fiche id, or several separated by |, when the right fiche is not proposed
  or several are equally right (duplicates, versions);
* none: no fiche of the KB covers this ticket;
* nothing: not labeled yet.

The sheet is .xlsx when openpyxl is installed and the file name ends in .xlsx
(ticket ids stay text, Excel cannot mangle them), otherwise CSV with ';' and a
UTF-8 BOM so that French Excel opens it correctly.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .dataset import NONE_WORDS, DatasetError, Ticket, format_expected
from .importers import read_rows, resolve_column

CANDIDATE_SEPARATOR = " — "
CELL_LIMIT = 32000  # Excel caps a cell at 32,767 characters
CANDIDATE_COLUMN = re.compile(r"^cand_(\d+)$")


def format_candidate(fiche_id: str, title: str) -> str:
    title = " ".join((title or "").split())
    return f"{fiche_id}{CANDIDATE_SEPARATOR}{title}" if title else fiche_id


def parse_candidate(cell: str) -> str:
    return cell.split(CANDIDATE_SEPARATOR, 1)[0].strip()


def _cap(text: str) -> str:
    return text if len(text) <= CELL_LIMIT else text[: CELL_LIMIT - 1] + "…"


@dataclass
class PrepareReport:
    rows: int = 0
    with_candidates: int = 0
    candidate_errors: int = 0
    first_error: str | None = None


def prepare_labels(tickets: list[Ticket], out_path: str | Path, engine=None, k: int = 5,
                   include_labeled: bool = False, progress=None) -> PrepareReport:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = ["ticket_id", "client", "text"] + [f"cand_{i}" for i in range(1, k + 1)] + ["expected", "notes"]
    report = PrepareReport()
    rows = []
    selected = [t for t in tickets if include_labeled or not t.labeled]
    can_propose = engine is not None and callable(getattr(engine, "candidates", None))
    for index, ticket in enumerate(selected):
        candidates: list[tuple[str, str]] = []
        if can_propose:
            try:
                candidates = list(engine.candidates(ticket, k))[:k]
            except Exception as exc:  # keep going: an empty row can still be labeled by hand
                report.candidate_errors += 1
                report.first_error = report.first_error or f"{type(exc).__name__}: {exc}"
        if candidates:
            report.with_candidates += 1
        cells = [format_candidate(fid, title) for fid, title in candidates] + [""] * (k - len(candidates))
        rows.append([ticket.ticket_id, ticket.client, _cap(ticket.text)] + cells + [format_expected(ticket.expected), ticket.notes or ""])
        if progress:
            progress(index, len(selected))
    report.rows = len(rows)
    if out_path.suffix.lower() == ".xlsx":
        _write_xlsx(out_path, header, rows, k)
    else:
        with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
    return report


def _write_xlsx(path: Path, header: list[str], rows: list[list[str]], k: int) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError:
        raise DatasetError("writing .xlsx needs openpyxl (pip install openpyxl); or use a .csv file name") from None
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "labels"
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    widths = {"A": 16, "B": 12, "C": 70}
    for offset in range(k):
        widths[chr(ord("D") + offset)] = 38
    widths[chr(ord("D") + k)] = 16
    widths[chr(ord("D") + k + 1)] = 30
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap
            cell.number_format = "@"
    sheet.freeze_panes = "D2"
    workbook.save(path)


@dataclass
class ApplyReport:
    rows: int = 0
    labeled: int = 0
    none: int = 0
    empty: int = 0
    unknown: list[str] = field(default_factory=list)


def _normalize_id(ticket_id: str) -> str:
    if ticket_id.isdigit():
        return ticket_id.lstrip("0") or "0"
    return ticket_id


def apply_labels(sheet_path: str | Path, tickets: list[Ticket]) -> tuple[list[Ticket], ApplyReport]:
    headers, rows, _ = read_rows(sheet_path)
    id_col = resolve_column(headers, "ticket_id", "ticket_id")
    client_col = resolve_column(headers, "client", "client")
    expected_col = resolve_column(headers, "expected", "expected")
    candidate_cols = {match.group(1): h for h in headers if (match := CANDIDATE_COLUMN.match(h.strip()))}

    updated = [replace(t) for t in tickets]
    by_key = {t.key: t for t in updated}
    # Excel drops leading zeros from numeric ids: match those too.
    by_loose_key = {f"{t.client}/{_normalize_id(t.ticket_id)}": t for t in updated}

    report = ApplyReport(rows=len(rows))
    for line, row in enumerate(rows, 2):
        raw = str(row.get(expected_col) or "").strip()
        if not raw:
            report.empty += 1
            continue
        client = str(row.get(client_col) or "").strip()
        ticket_id = str(row.get(id_col) or "").strip()
        ticket = by_key.get(f"{client}/{ticket_id}") or by_loose_key.get(f"{client}/{_normalize_id(ticket_id)}")
        if ticket is None:
            report.unknown.append(f"{client}/{ticket_id}")
            continue
        tokens = [token.strip() for token in raw.split("|") if token.strip()]
        if len(tokens) == 1 and tokens[0].lower() in NONE_WORDS:
            ticket.expected = []
            report.none += 1
            continue
        ids: list[str] = []
        for token in tokens:
            if token.lower() in NONE_WORDS:
                raise DatasetError(f"{sheet_path}, line {line}: 'none' cannot be combined with fiche ids")
            if token.isdigit() and token in candidate_cols:
                cell = str(row.get(candidate_cols[token]) or "").strip()
                if not cell:
                    raise DatasetError(f"{sheet_path}, line {line}: candidate {token} is empty for ticket {ticket.key}")
                token = parse_candidate(cell)
            if token not in ids:
                ids.append(token)
        ticket.expected = ids
        report.labeled += 1
    return updated, report
