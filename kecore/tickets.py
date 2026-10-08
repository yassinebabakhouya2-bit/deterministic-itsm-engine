"""Real tickets (V10 slice 4): parse the client's export, strip personal data, keep the rest.

An ITSM export (EasyVista, semicolon-separated, quoted fields that hold embedded newlines) is
read with the ``csv`` module, never split by hand. A few exports carry one metadata row right after
the real header (column widths, no actual ticket) -- dropped because its ticket number does not
look like one.

Columns are kept by allowlist (``KEPT_COLUMNS``): a column that names a person (beneficiary,
requester, the technician who worked it) is dropped entirely, never masked -- a masked name is
still a name-shaped hole -- and so is any column this module does not know (a renamed or new
column could hold names: it is reported, never stored). Header names are compared trimmed and
case-folded.

Every kept column except the purely structured ones (dates, priority, status, SLA, reference
numbers) is cleaned, in this order:

0. a value longer than ``MAX_FIELD_CHARS`` is cut there first (Azure Table limits a property to
   64 KiB, and it bounds the work of every pattern below);
1. free text only (description, resolution, root cause): the e-mail signature is cut -- from a
   closing formula ("Cordialement", "Bien à vous", "Best regards", "Merci d'avance", "Merci",
   and the abbreviations "Cdt", "Cdlt" at the start of a line only) that ends its line, alone or
   followed by up to three capitalised words (the signer's name) -- since that is where names,
   job titles, phones and addresses sit; "je vous remercie cordialement de votre aide" or "le CDT
   du chantier" are never cut. The name after a greeting ("Bonjour Jean,", "Bonjour M. Dupont,",
   "Bonjour Jean et Paul,") becomes ``[nom]``; the content of forwarded e-mail header lines ("De :",
   "From:", "À :", "Cc :", "De la part de :"...) becomes ``[masqué]``;
2. e-mail addresses (``[email]``) and phone numbers (``[phone]``: French and Moroccan formats,
   never a date such as "01.10.2026 18:16");
3. mentions (``@Jean Dupont``, ``@jdupont``): ``[mention]``;
4. the people the export names in its person columns (``[nom]``) -- at scrub time only, while those
   columns are still at hand: a first-name/surname pair in any case ("JEAN DUPONT", "Dupont Jean"),
   a single name only when written as a name ("Dupont", never "DUPONT" alone, which could be a word
   of an upper-case title); a single upper-case value ("ADMINISTRATEUR") is an account, not a person.

Known limits, documented rather than hidden: a first name alone in running text, with no marker
around it and not named in the row's person columns, is not detected; nor is an upper-case surname
alone, or a lower-case one after a lower-case mention ("@jean dupont"). The placeholders are
removed from the text kefind reads (``ticket_text``): "[email]" must not become the word "email"
of a fiche. ``scrub_entity`` re-applies steps 0 to 3 to a row already stored (the raw
export is deleted once scrubbed, so a stricter cleaning can only be applied to what was kept).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field


_RESERVED_PROPERTIES = frozenset({"partitionkey", "rowkey", "timestamp", "etag"})


def slug(column: str) -> str:
    """A column name as a valid Azure Table property: ASCII letters, digits, underscore only."""
    plain = unicodedata.normalize("NFKD", column).encode("ascii", "ignore").decode("ascii")
    plain = re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_").lower()
    if not plain or plain[0].isdigit():
        plain = "c_" + plain
    if plain in _RESERVED_PROPERTIES:
        plain = "field_" + plain
    return plain


_slug = slug  # kept for callers written before it was public


def _norm(column: str) -> str:
    return " ".join(unicodedata.normalize("NFC", column or "").split()).casefold()


# An EasyVista incident number: I + digits, then letters, digits, '_' or '-' only (a Table RowKey
# refuses '/', '\\', '#', '?'; the labeling tab's URLs take the same characters).
TICKET_ID_RE = re.compile(r"I\d+[A-Za-z0-9_-]*")
TICKET_ID_COLUMN = "N° de ticket"

# Names of people: never kept, masking would still leave a name-shaped hole.
DROPPED_COLUMNS = frozenset({
    "Bénéficiaire", "Demandeur", "Intervenant en cours", "Enregistré par", "Résolu par (intervenant)",
})

# Kept exactly as exported: dates, codes and reference numbers.
STRUCTURED_COLUMNS = frozenset({
    "Date d'émission", "Date de résolution", "Dernière modification", "Priorité", "Criticité", "Impact",
    "Statut", "Meta Statut", "SLA", "N° d'origine", "Numéro SR",
})

# Written by a person in full sentences: signature, greeting and forwarded headers are cleaned too.
FREE_TEXT_COLUMNS = frozenset({"Description", "Résolution", "Cause réelle"})

# Every column stored (besides the ticket id): an allowlist, not a blocklist.
KEPT_COLUMNS = STRUCTURED_COLUMNS | FREE_TEXT_COLUMNS | frozenset({
    "Titre", "Sujet", "Application / Service", "Groupe en cours", "Entité complète", "Localisation complète",
    "Groupe responsable du sujet", "Référence externe", "Origine", "Groupe de résolution",
    "1er groupe d'affectation", "Sujet complet",
})

# What a ticket "says", in order, for kefind's funnel (kefind.funnel.find reads free text, not fields).
TEXT_COLUMNS = ("Titre", "Sujet", "Description")

MAX_FIELD_CHARS = 30_000
MAX_EXPORT_FIELD_CHARS = 16 * 1024 * 1024

_KEPT = {_norm(c): c for c in KEPT_COLUMNS}
_DROPPED = {_norm(c) for c in DROPPED_COLUMNS}
_TICKET_ID = _norm(TICKET_ID_COLUMN)

_UPPER = "A-ZÀ-ÖØ-Þ"
_NAME_WORD = rf"[{_UPPER}][\w'’-]*"

_NAMES = rf"{_NAME_WORD}(?:(?:[ \t]+|[ \t]*,[ \t]*|[ \t]+(?:et|and|&)[ \t]+){_NAME_WORD}){{0,3}}"
# what may follow a closing formula on its line: punctuation and the signer's name, nothing else
_SIGNATURE_TAIL = rf"[ \t]*[,.!:]?[ \t]*(?:{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,2}})?[ \t]*[,.!]?[ \t]*$"
SIGNATURE_RE = re.compile(
    r"(?m)"
    r"(?:(?:^[ \t>]*|(?<=[\s,.;!]))"
    r"(?i:(?:(?:bien|très|meilleures)\s+)?cordialement|bien\s+à\s+(?:vous|toi)"
    r"|(?:sinc[èe]res\s+|meilleures\s+|bonnes\s+)?salutations|(?:best|kind|warm)\s+regards|regards"
    r"|merci(?:\s+(?:d['’]avance|par\s+avance|beaucoup|bien))?)"
    r"|^[ \t>]*(?i:cdlt|cdt|slts|bàv|bav))"
    + _SIGNATURE_TAIL
)
GREETING_RE = re.compile(
    r"(?m)^([ \t>]*(?i:bonjour|bonsoir|hello|hi|salut|cher|chère|chers|chères|dear)[ \t]+)"
    rf"((?:(?i:m\.|mme\.?|mlle\.?|monsieur|madame|mr\.?|mrs\.?|ms\.?)[ \t]+)?{_NAMES})"
    r"(?=[ \t]*(?:[,!.:]|$))"
)
HEADER_RE = re.compile(
    r"(?im)^([ \t>]*(?:de|from|à|to|cc|cci|bcc|envoyé\s+par|sent\s+by|expéditeur|destinataire"
    r"|répondre\s+à|reply-to|de\s+la\s+part\s+de)[ \t]*:)[ \t]*\S[^\n]*$"
)
# "A :" without its accent is a header only when the line carries an address
HEADER_A_RE = re.compile(r"(?im)^([ \t>]*a[ \t]*:)[ \t]*[^\n]*(?:@|\[email\])[^\n]*$")
# Bounded local part: the unbounded [\w.+-]+@ of kecore.text.EMAIL_RE is quadratic on a long run
# of letters without "@" (seconds per 30k characters).
TICKET_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
# French and Moroccan numbers, as kecore.text.PHONE_RE writes them (06 12 34 56 78, 0612345678,
# 0661-234567, +212 6 12 34 56 78, 00212612345678, +33 (0)6 12 34 56 78, 05.37.12.34.56), but never
# starting on a date: 01.10.2026, 01-10-2026, 01 10 2026.
TICKET_PHONE_RE = re.compile(
    r"(?<![\w+])"
    r"(?!\d{2}(?P<dsep>[./ -])\d{2}(?P=dsep)\d{4})"
    r"(?:(?:\+|00)\d{2,3}[\s.-]?(?:\(0\)[\s.-]?)?|0)"
    r"[1-9](?:[\s.-]?\d{2}){4}"
    r"(?!\w)"
)
MENTION_RE = re.compile(rf"(?<![\w.])@[ \t]?(?:{_NAME_WORD}|\w[\w.'’-]*)(?:[ \t]+{_NAME_WORD}){{0,2}}")
PLACEHOLDER_RE = re.compile(r"\[(?:email|phone|nom|mention|masqué|masked)\]")


def _cut_signature(text: str) -> tuple[str, bool]:
    for match in SIGNATURE_RE.finditer(text):
        if text[: match.start()].strip():
            return text[: match.start()].rstrip(" \t\n,;:"), True
    return text, False


_PERSON_WORD_RE = re.compile(r"[^\W\d_][\w'’-]{2,}")


class NameMasker:
    """The people of one row's person columns, masked in its text (see the module docstring, step 4)."""

    def __init__(self, values: list[str]):
        pairs: set[str] = set()
        singles: set[str] = set()
        for value in values:
            words = _PERSON_WORD_RE.findall(value or "")
            has_lower = any(ch.islower() for ch in value or "")
            if not words or (len(words) == 1 and not has_lower):
                continue  # an account ("ADMINISTRATEUR", "EASYVISTA"), not a person
            for a, b in zip(words, words[1:]):
                pairs.add(re.escape(a) + r"[ \t]+" + re.escape(b))
                pairs.add(re.escape(b) + r"[ \t]+" + re.escape(a))  # "DUPONT Jean" written "Jean Dupont"
            if has_lower:
                singles.update(re.escape(w) for w in words)
        self._pairs = re.compile(r"(?<!\w)(?:" + "|".join(sorted(pairs, key=len, reverse=True)) + r")(?!\w)",
                                 re.IGNORECASE) if pairs else None
        self._singles = re.compile(r"(?<!\w)(?:" + "|".join(sorted(singles, key=len, reverse=True)) + r")(?!\w)",
                                   re.IGNORECASE) if singles else None

    def __call__(self, text: str, counts: Counter) -> str:
        if self._pairs is not None:
            text, found = self._pairs.subn("[nom]", text)
            if found:
                counts["[nom]"] += found
        if self._singles is not None:
            def single(match: re.Match) -> str:
                word = match.group(0)
                if word[0].isupper() and not word.isupper():  # "Dupont", never "DUPONT" nor "dupont"
                    counts["[nom]"] += 1
                    return "[nom]"
                return word
            text = self._singles.sub(single, text)
        return text


def names_pattern(values: list[str]) -> NameMasker | None:
    masker = NameMasker(values)
    return masker if masker._pairs is not None or masker._singles is not None else None


def clean_value(column: str, value: str | None, counts: Counter, names: NameMasker | None = None) -> str:
    """One kept column's value, cleaned as described in the module docstring."""
    text = value or ""
    if column in STRUCTURED_COLUMNS or not text:
        return text
    if len(text) > MAX_FIELD_CHARS:
        text = text[:MAX_FIELD_CHARS]
        counts["[truncated]"] += 1
    if column in FREE_TEXT_COLUMNS:
        text, cut = _cut_signature(text)
        if cut:
            counts["[signature]"] += 1
        text, greeted = GREETING_RE.subn(r"\1[nom]", text)
        if greeted:
            counts["[nom]"] += greeted
        for pattern in (HEADER_RE, HEADER_A_RE):
            text, headers = pattern.subn(r"\1 [masqué]", text)
            if headers:
                counts["[masqué]"] += headers
    for label, pattern in (("[email]", TICKET_EMAIL_RE), ("[phone]", TICKET_PHONE_RE), ("[mention]", MENTION_RE)):
        text, found = pattern.subn(label, text)
        if found:
            counts[label] += found
    if names is not None:  # after e-mails and mentions: each is masked whole, not name by name
        text = names(text, counts)
    return text[:MAX_FIELD_CHARS]


def _readable(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", PLACEHOLDER_RE.sub(" ", text)).strip()


@dataclass
class Ticket:
    id: str
    fields: dict[str, str]  # cleaned, only KEPT_COLUMNS

    def text(self) -> str:
        parts = [_readable(self.fields.get(column, "")) for column in TEXT_COLUMNS]
        return "\n".join(part for part in parts if part)

    def to_entity(self, client: str) -> dict:
        """One Azure Table entity: ``PartitionKey``/``RowKey`` plus every kept field, column
        names slugged into valid Table property names (French, spaces and accents are not)."""
        entity = {"PartitionKey": client, "RowKey": self.id}
        for column, value in self.fields.items():
            entity[slug(column)] = value
        return entity


def ticket_text(entity: dict) -> str:
    """The text kefind reads, from a stored row: same columns and order as ``Ticket.text``,
    without the masking placeholders ("[email]" would otherwise be searched as "email")."""
    parts = [_readable(str(entity.get(slug(column)) or "")) for column in TEXT_COLUMNS]
    return "\n".join(part for part in parts if part)


@dataclass
class ScrubReport:
    rows_read: int = 0
    tickets: int = 0
    skipped: int = 0
    masked: dict[str, int] = field(default_factory=dict)
    unknown_columns: list[str] = field(default_factory=list)  # dropped: not in the allowlist


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def scrub(data: bytes) -> tuple[list[Ticket], ScrubReport]:
    """Every real ticket in a raw export, person and unknown columns dropped, the rest cleaned."""
    # a pasted e-mail thread can exceed the csv module's default 128 KiB per field, which would fail
    # the whole export on one ticket; every value is cut to MAX_FIELD_CHARS before it is stored
    csv.field_size_limit(max(csv.field_size_limit(), MAX_EXPORT_FIELD_CHARS))
    reader = csv.DictReader(io.StringIO(_decode(data)), delimiter=";")
    counts: Counter = Counter()
    tickets: list[Ticket] = []
    report = ScrubReport()
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    id_header = next((h for h in headers if _norm(h) == _TICKET_ID), TICKET_ID_COLUMN)
    person_headers = [h for h in headers if _norm(h) in _DROPPED]
    kept = {h: _KEPT[_norm(h)] for h in headers if _norm(h) in _KEPT}
    report.unknown_columns = sorted(h for h in headers if h not in kept and h != id_header and h not in person_headers)
    seen: set[str] = set()
    for row in reader:
        report.rows_read += 1
        ticket_id = (row.get(id_header) or "").strip()
        if not TICKET_ID_RE.fullmatch(ticket_id) or ticket_id in seen:
            report.skipped += 1
            continue
        seen.add(ticket_id)
        names = names_pattern([row.get(h) or "" for h in person_headers])
        fields = {column: clean_value(column, row.get(header), counts, names) for header, column in kept.items()}
        tickets.append(Ticket(id=ticket_id, fields=fields))
    report.tickets = len(tickets)
    report.masked = dict(sorted(counts.items()))
    return tickets, report


# Properties of a stored row that are not exported columns: keys, and what machines add later.
_MACHINE_PREFIXES = ("kefind_", "masked_")
_SLUG_TO_COLUMN = {slug(c): c for c in KEPT_COLUMNS}


def scrub_entity(entity: dict, counts: Counter) -> dict:
    """The cleaning of ``scrub`` re-applied to a row already stored. Returns only the properties
    that change; a property that is not a kept column (a person column, an unknown one) is emptied.
    The same row cleaned twice does not change a second time."""
    changes: dict = {}
    for key, value in entity.items():
        if key in ("PartitionKey", "RowKey", "Timestamp", "etag") or key.startswith(_MACHINE_PREFIXES):
            continue
        if not isinstance(value, str):
            continue
        column = _SLUG_TO_COLUMN.get(key)
        if column is None:
            if value:
                changes[key] = ""
                counts["[dropped]"] += 1
            continue
        cleaned = clean_value(column, value, counts)
        if cleaned != value:
            changes[key] = cleaned
    return changes


__all__ = [
    "Ticket", "ScrubReport", "scrub", "scrub_entity", "clean_value", "ticket_text", "slug", "names_pattern", "NameMasker",
    "DROPPED_COLUMNS", "STRUCTURED_COLUMNS", "FREE_TEXT_COLUMNS", "KEPT_COLUMNS", "TEXT_COLUMNS", "TICKET_ID_RE",
    "TICKET_ID_COLUMN", "MAX_FIELD_CHARS",
]
