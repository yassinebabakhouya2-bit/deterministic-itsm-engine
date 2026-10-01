"""Text cleaning, personal-data masking, and the normalization behind every
verbatim check.

A step is shown to a technician only if its text is found, character for
character, in the fiche. "Character for character" is judged after a fixed
normalization: whitespace runs, typographic apostrophes, quotes and dashes,
markdown emphasis marks, case, and the French space before ':;!?' do not
count. What is displayed is always the original span of the fiche, never the
model's copy of it.
"""

from __future__ import annotations

import bisect
import html
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .errors import InputError

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# French and Moroccan formats: 06 12 34 56 78, 0612345678, +212 6 12 34 56 78,
# 00212612345678, +33 (0)6 12 34 56 78, 05.37.12.34.56
PHONE_RE = re.compile(
    r"(?<![\w+])"
    r"(?:(?:\+|00)\d{2,3}[\s.-]?(?:\(0\)[\s.-]?)?|0)"
    r"[1-9](?:[\s.-]?\d{2}){4}"
    r"(?!\w)"
)
HTML_HINT_RE = re.compile(
    r"(?i)<\s*/?\s*(?:p|br|div|span|ul|ol|li|b|i|u|strong|em|table|tbody|tr|td|th|font|a|img|h\d)\b[^>]*>"
)
HTML_LIST_ITEM_RE = re.compile(r"(?i)<\s*li\b[^>]*>")
HTML_BLOCK_OPEN_RE = re.compile(r"(?i)<\s*(?:p|div|h[1-6]|tr|ul|ol|table)\b[^>]*>")
HTML_BREAK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h\d|/ul|/ol|/table)\s*/?\s*>")
HTML_TAG_RE = re.compile(r"<[^>]*>")
HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def clean_text(value: str) -> str:
    """Flatten HTML (list items become '- ' lines) and tidy whitespace.

    Blank lines between paragraphs are kept (one at most): they carry the
    structure the step detection relies on.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if HTML_HINT_RE.search(text):
        text = HTML_LIST_ITEM_RE.sub("\n- ", text)
        text = HTML_BLOCK_OPEN_RE.sub("\n", text)
        text = HTML_BREAK_RE.sub("\n", text)
        text = HTML_TAG_RE.sub(" ", text)
    if HTML_ENTITY_RE.search(text):
        text = html.unescape(text)
    text = text.replace("\xa0", " ").replace(" ", " ")
    lines: list[str] = []
    for line in text.split("\n"):
        indent = len(line) - len(line.lstrip(" \t"))
        line = " ".join(line.split())
        if line and indent and _LIST_START_RE.match(line):
            line = " " * min(indent, 8) + line  # keep the nesting of list items
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


_LIST_START_RE = re.compile(r"^(?:\d{1,2}[.)]|[a-zA-Z][.)]|[-*•–·▪●◦➢➤►])\s")


@dataclass
class Scrubber:
    """Masks e-mail addresses and phone numbers, and counts what it masked."""

    extra_patterns: list[str] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self._rules = [("[email]", EMAIL_RE), ("[phone]", PHONE_RE)]
        for raw in self.extra_patterns:
            try:
                self._rules.append(("[masked]", re.compile(raw)))
            except re.error as exc:
                raise InputError(f"invalid masking pattern {raw!r}: {exc}") from None

    def __call__(self, text: str) -> str:
        for label, pattern in self._rules:
            text, count = pattern.subn(label, text)
            if count:
                self.counts[label] += count
        return text


# --- normalization for verbatim checks -------------------------------------

_TRANSLATE = {}
for _ch in "’‘ʼ´′ʹ":
    _TRANSLATE[_ch] = "'"
for _ch in "«»“”„‟″":
    _TRANSLATE[_ch] = '"'
for _ch in "–—‐‑‒−":
    _TRANSLATE[_ch] = "-"
_TRANSLATE["…"] = "..."
_DROPPED = frozenset("*`")
_NO_SPACE_BEFORE = frozenset(":;!?,.)\"")
_NO_SPACE_AFTER = frozenset("(\"")
LIST_MARKER_RE = re.compile(
    r"^\s*(?:(?:[EÉeé]tape|ETAPE|ÉTAPE|[Ss]tep)\s*\d{1,2}\s*[:.)\-–]?|\d{1,2}[.)]|[a-zA-Z][.)]|[-*•–·▪●◦➢➤►✓✔])\s+"
)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, for each normalized character, its index in ``text``."""
    out: list[str] = []
    index: list[int] = []
    pending_space = False
    for position, char in enumerate(text):
        if char.isspace():
            if out:
                pending_space = True
            continue
        if char in _DROPPED:
            continue
        mapped = _TRANSLATE.get(char, char)
        folded = unicodedata.normalize("NFKC", mapped).casefold()
        if not folded:
            continue
        if pending_space:
            if folded[0] not in _NO_SPACE_BEFORE and out and out[-1] not in _NO_SPACE_AFTER:
                out.append(" ")
                index.append(position)
            pending_space = False
        for piece in folded:
            out.append(piece)
            index.append(position)
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


def strip_list_marker(text: str) -> str:
    return LIST_MARKER_RE.sub("", text, count=1)


class NormalizedText:
    """A fiche's text, ready for verbatim lookups that return original spans."""

    def __init__(self, text: str):
        self.text = text
        self.norm, self.index = normalize_with_map(text)

    def _to_norm(self, original_position: int) -> int:
        return bisect.bisect_left(self.index, original_position)

    def find(self, quote: str, after: int = 0) -> tuple[int, int] | None:
        """Original (start, end) of ``quote`` in the text, or None.

        The quote is accepted only on word boundaries. The first match at or
        after ``after`` wins, so that steps keep their order; failing that,
        the first match anywhere.
        """
        needle = normalize(strip_list_marker(quote.strip()))
        needle = needle.rstrip(" .;:,!").strip()
        if needle.endswith("..."):
            needle = needle[:-3].rstrip()
        if len(needle) < 3:
            return None
        for origin in (self._to_norm(after), 0):
            position = self.norm.find(needle, origin)
            while position != -1:
                span = self._span(position, len(needle))
                if span is not None:
                    return self._extend(span, quote)
                position = self.norm.find(needle, position + 1)
        return None

    def _extend(self, span: tuple[int, int], quote: str) -> tuple[int, int]:
        """Take in the closing markdown marks and the final punctuation the lookup ignored."""
        start, end = span
        for mark in ("**", "`"):
            if self.text[start:end].count(mark) % 2 == 1 and self.text.startswith(mark, end):
                end += len(mark)
        final = quote.rstrip()[-1:]
        if final in ".!?" and self.text[end:end + 1] == final:
            end += 1
        return start, end

    def _span(self, position: int, length: int) -> tuple[int, int] | None:
        start = self.index[position]
        end = self.index[position + length - 1] + 1
        before = self.text[start - 1] if start > 0 else " "
        after = self.text[end] if end < len(self.text) else " "
        if before.isalnum() or after.isalnum():
            return None
        return start, end

    def contains(self, fragment: str) -> bool:
        needle = normalize(fragment).strip()
        return bool(needle) and needle in self.norm
