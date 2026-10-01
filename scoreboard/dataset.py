"""Labeled ticket sets: the ground truth every engine is scored against.

One ticket per line (JSONL, UTF-8):

    {"ticket_id": "T-0001", "client": "client-s", "text": "...", "expected": ["KB0012345"]}

``expected`` decides how a ticket is scored:

* absent or null: not labeled yet (skipped when engines are run);
* ``[]`` or ``"none"``: no fiche of the client's KB covers the ticket, so the
  right behaviour is to show no fiche at all;
* one or more fiche ids: any of them counts as the right fiche (duplicates and
  superseded versions of the same procedure are common in real KBs).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from kecore.errors import InputError

NONE_WORDS = frozenset({"none", "aucune", "aucun", "no", "-", "n/a", "na"})
LABEL_SEPARATOR = "|"


# One error type for bad inputs across the engine and the scoreboard.
DatasetError = InputError


@dataclass
class Ticket:
    ticket_id: str
    client: str
    text: str
    category: str | None = None
    expected: list[str] | None = None
    notes: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.client}/{self.ticket_id}"

    @property
    def labeled(self) -> bool:
        return self.expected is not None

    @property
    def answerable(self) -> bool:
        return bool(self.expected)

    def to_dict(self) -> dict:
        data: dict = {"ticket_id": self.ticket_id, "client": self.client, "text": self.text}
        if self.category:
            data["category"] = self.category
        if self.expected is not None:
            data["expected"] = list(self.expected)
        if self.notes:
            data["notes"] = self.notes
        if self.meta:
            data["meta"] = self.meta
        return data


def parse_expected(value) -> list[str] | None:
    """Normalize a label.

    Returns None when the value carries no label, [] for "no fiche covers this
    ticket", or the list of acceptable fiche ids (order kept, duplicates dropped).
    Several ids in one string are separated by ``|``.
    """
    if value is None:
        return None
    from_string = isinstance(value, str)
    if from_string:
        text = value.strip()
        if not text:
            return None
        if text.lower() in NONE_WORDS:
            return []
        items = text.split(LABEL_SEPARATOR)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise DatasetError(f"a label must be a string or a list, got {type(value).__name__}")

    ids: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise DatasetError(f"a fiche id must be a string, got {item!r}")
        item = item.strip()
        if not item:
            continue
        if item.lower() in NONE_WORDS:
            if len([i for i in items if str(i).strip()]) > 1:
                raise DatasetError(f"label {value!r} mixes 'none' with fiche ids")
            return []
        if item not in ids:
            ids.append(item)
    if from_string and not ids:
        return None
    return ids


def format_expected(expected: list[str] | None) -> str:
    """Inverse of parse_expected for spreadsheets: '' / 'none' / 'id1|id2'."""
    if expected is None:
        return ""
    if not expected:
        return "none"
    return LABEL_SEPARATOR.join(expected)


def ticket_from_dict(obj: dict, where: str = "") -> Ticket:
    prefix = f"{where}: " if where else ""
    if not isinstance(obj, dict):
        raise DatasetError(f"{prefix}each line must be a JSON object")

    def required(key: str) -> str:
        value = obj.get(key)
        if value is None or not str(value).strip():
            raise DatasetError(f"{prefix}missing '{key}'")
        return str(value) if key == "text" else str(value).strip()

    try:
        expected = parse_expected(obj.get("expected"))
    except DatasetError as exc:
        raise DatasetError(f"{prefix}{exc}") from None
    meta = obj.get("meta") or {}
    if not isinstance(meta, dict):
        raise DatasetError(f"{prefix}'meta' must be an object")
    return Ticket(
        ticket_id=required("ticket_id"),
        client=required("client"),
        text=required("text"),
        category=(str(obj["category"]).strip() or None) if obj.get("category") else None,
        expected=expected,
        notes=(str(obj["notes"]).strip() or None) if obj.get("notes") else None,
        meta=meta,
    )


def load_tickets(path: str | Path) -> list[Ticket]:
    path = Path(path)
    tickets: list[Ticket] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            where = f"{path}:{lineno}"
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{where}: invalid JSON ({exc.msg})") from None
            ticket = ticket_from_dict(obj, where)
            if ticket.key in seen:
                raise DatasetError(f"{where}: duplicate ticket {ticket.key}")
            seen.add(ticket.key)
            tickets.append(ticket)
    return tickets


def save_tickets(path: str | Path, tickets: Iterable[Ticket]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for ticket in tickets:
            handle.write(json.dumps(ticket.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    return count


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(tickets: list[Ticket]) -> dict:
    """Counts used by the 'validate' command and the run manifest."""
    labeled = [t for t in tickets if t.labeled]
    return {
        "tickets": len(tickets),
        "labeled": len(labeled),
        "with_fiche": sum(1 for t in labeled if t.answerable),
        "without_fiche": sum(1 for t in labeled if not t.answerable),
        "unlabeled": len(tickets) - len(labeled),
        "by_client": dict(sorted(Counter(t.client for t in tickets).items())),
    }
