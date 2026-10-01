"""Plain-text helpers for KB passages extracted by Document Intelligence."""
from __future__ import annotations

import html as _html
import re

_TAG_BREAK = re.compile(r"</(?:tr|p|div|li|h[1-6]|table)>|<br\s*/?>", re.I)
_CELL_BREAK = re.compile(r"</t[dh]>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SECRET = re.compile(r"(mot de passe|password|pwd|mdp)\s*[:=]\s*\S+", re.I)


def kb_text(raw, limit=3500):
    """Readable text of a KB passage (HTML tables, comments removed)."""
    t = _COMMENT.sub("", raw or "")
    t = _CELL_BREAK.sub(" | ", t)
    t = _TAG_BREAK.sub("\n", t)
    t = _html.unescape(_ANY_TAG.sub("", t))
    lines = [re.sub(r"[ \t]*\|[ \t|]*$", "", re.sub(r"[ \t]+", " ", ln)).strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln and ln.strip("| ")]
    out = "\n".join(lines)
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "..."


def redact(text: str) -> str:
    return _SECRET.sub(r"\1: [REDACTED]", text or "")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", kb_text(text, limit=10**6)).strip().lower()
