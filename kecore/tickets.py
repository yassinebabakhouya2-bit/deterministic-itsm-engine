"""Real tickets (V10 slice 4): parse the client's export, strip personal data, keep the rest.

An ITSM export (EasyVista, semicolon-separated, quoted fields that hold embedded newlines) is
read with the ``csv`` module, never split by hand. A few exports carry one header-only metadata
row right after the real header (column widths, no actual ticket) -- dropped because its ticket
number does not look like one. Columns that name a person (beneficiary, requester, the
technician who worked it) are dropped entirely, never masked: a masked name is still a name-shaped
hole next to the rest of the row, worse than not having the column. What free text remains
(title, subject, description, resolution) is passed through ``kecore.text.Scrubber`` for e-mail
addresses and phone numbers, same mechanism the KB decomposition already uses.

Nothing here decides which fiche a ticket should have reached -- this module only gets a ticket
from a raw export into a shape ``kefind`` can read.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field

from .text import Scrubber

TICKET_ID_RE = re.compile(r"^I\d+")

_RESERVED_PROPERTIES = frozenset({"partitionkey", "rowkey", "timestamp", "etag"})

# Names of people: never kept, masking would still leave a name-shaped hole.
DROPPED_COLUMNS = frozenset({
    "Bénéficiaire", "Demandeur", "Intervenant en cours", "Enregistré par", "Résolu par (intervenant)",
})

# What a ticket "says", in order, for kefind's funnel (kefind.funnel.find reads free text, not fields).
TEXT_COLUMNS = ("Titre", "Sujet", "Description")

TICKET_ID_COLUMN = "N° de ticket"


def _slug(column: str) -> str:
    """A column name as a valid Azure Table property: ASCII letters, digits, underscore only."""
    plain = unicodedata.normalize("NFKD", column).encode("ascii", "ignore").decode("ascii")
    plain = re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_").lower()
    if not plain or plain[0].isdigit():
        plain = "c_" + plain
    if plain in _RESERVED_PROPERTIES:
        plain = "field_" + plain
    return plain


@dataclass
class Ticket:
    id: str
    fields: dict[str, str]  # scrubbed, DROPPED_COLUMNS excluded
    masked: dict[str, int] = field(default_factory=dict)  # "[email]" / "[phone]" -> count, no PII itself

    def text(self) -> str:
        parts = [self.fields.get(column, "").strip() for column in TEXT_COLUMNS]
        return "\n".join(part for part in parts if part)

    def to_entity(self, client: str) -> dict:
        """One Azure Table entity: ``PartitionKey``/``RowKey`` plus every kept field, column
        names slugged into valid Table property names (French, spaces and accents are not)."""
        entity = {"PartitionKey": client, "RowKey": self.id}
        for column, value in self.fields.items():
            entity[_slug(column)] = value
        for label, count in self.masked.items():
            entity[f"masked_{label.strip('[]')}"] = count
        return entity


@dataclass
class ScrubReport:
    rows_read: int = 0
    tickets: int = 0
    skipped: int = 0
    masked: dict[str, int] = field(default_factory=dict)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def scrub(data: bytes) -> tuple[list[Ticket], ScrubReport]:
    """Every real ticket in a raw export, personal columns dropped, free text masked."""
    reader = csv.DictReader(io.StringIO(_decode(data)), delimiter=";")
    scrubber = Scrubber()
    tickets: list[Ticket] = []
    report = ScrubReport()
    seen: set[str] = set()
    for row in reader:
        report.rows_read += 1
        ticket_id = (row.get(TICKET_ID_COLUMN) or "").strip()
        if not TICKET_ID_RE.match(ticket_id) or ticket_id in seen:
            report.skipped += 1
            continue
        seen.add(ticket_id)
        fields: dict[str, str] = {}
        for column, value in row.items():
            if column is None or column in DROPPED_COLUMNS or column == TICKET_ID_COLUMN:
                continue
            fields[column] = scrubber(value or "") if column in TEXT_COLUMNS else (value or "")
        tickets.append(Ticket(id=ticket_id, fields=fields))
    report.tickets = len(tickets)
    report.masked = dict(scrubber.counts)
    return tickets, report


__all__ = ["Ticket", "ScrubReport", "scrub", "DROPPED_COLUMNS", "TEXT_COLUMNS", "TICKET_ID_RE", "TICKET_ID_COLUMN"]
