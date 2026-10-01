"""Fiche identifiers, shared by the engine and the scoreboard.

Labels and engines must name fiches the same way, or every answer looks wrong.
Both go through ``extract_fiche_id``: a ServiceNow number (KB0012345) when one
is found, otherwise the document name without its folder and extension.
"""

from __future__ import annotations

import os
import re

DOC_EXTENSIONS = frozenset(
    {
        ".md", ".txt", ".pdf", ".docx", ".doc", ".html", ".htm", ".pptx", ".ppt",
        ".xlsx", ".xls", ".json", ".csv", ".wav", ".mp3", ".mp4", ".vtt",
    }
)
DEFAULT_FICHE_REGEX = r"KB\d{5,}"


def strip_document_path(value: str) -> str:
    """'Kbs/KB0012345.pdf' -> 'KB0012345'; a title such as 'VPN / Wi-Fi' is left alone."""
    base = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    root, ext = os.path.splitext(base)
    if root and ext.lower() in DOC_EXTENSIONS:
        return root
    return value


def extract_fiche_id(raw, pattern: re.Pattern | None = None, strip_path: bool = True) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = next((item for item in raw if item), None)
        if raw is None:
            return None
    value = str(raw).strip()
    if not value:
        return None
    if pattern is not None:
        match = pattern.search(value)
        if match:
            return match.group(0)
    return strip_document_path(value) if strip_path else value


def compile_pattern(regex: str | None) -> re.Pattern | None:
    if not regex:
        return None
    try:
        return re.compile(regex)
    except re.error as exc:
        raise ValueError(f"invalid fiche id regex {regex!r}: {exc}") from None
