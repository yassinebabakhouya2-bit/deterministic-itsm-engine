"""The writing profile of a client's KB, learned from its own fiches.

Each client writes fiches its own way. The profile records, without anyone
validating it:

- headings: the section names the client uses and the role of each one
  (keyword table first, the LLM once for the names it does not know);
- boilerplate: lines repeated across many fiches (contact lines, signatures,
  legal notices), removed before decomposition;
- styles: how many fiches use numbered lists, bullets or prose.

A stability check learns the headings on two halves of the KB. When the halves
disagree, the profile trusts only the keyword table.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .segment import LABEL_RE, MARKER_RE, detect_heading, first_verb, heading_key, iter_lines, keyword_role
from .text import normalize

PROFILE_VERSION = 1
BOILERPLATE_MARKERS = re.compile(
    r"(?i)(contact|support|assistance|hotline|service desk|©|copyright|confidentiel|tous droits|mentions l[ée]gales|"
    r"r[ée]dig[ée] par|auteur|derni[eè]re mise [àa] jour|mis [àa] jour le|version du document|ne pas diffuser)"
)


@dataclass
class Profile:
    client: str
    fiches: int = 0
    headings: dict[str, dict] = field(default_factory=dict)
    boilerplate: list[str] = field(default_factory=list)
    styles: dict[str, int] = field(default_factory=dict)
    stable: bool = True
    stability: float | None = None
    llm_usage: dict = field(default_factory=dict)
    # V10 pilier 2 — per-client software dictionary, extracted from the corpus, never hand-coded.
    # {canonical_id: [surface forms seen in the corpus]}. Empty for a profile built before pilier 2,
    # or when build_profile(..., with_dictionary=False) is used.
    dictionary: dict[str, list[str]] = field(default_factory=dict)
    dictionary_usage: dict = field(default_factory=dict)
    version: int = PROFILE_VERSION

    def role_lookup(self):
        def lookup(key: str) -> str | None:
            entry = self.headings.get(key)
            if entry and entry.get("role") and (self.stable or entry.get("source") == "rules"):
                return entry["role"]
            return keyword_role(key)

        return lookup

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _heading_candidates(text: str) -> dict[str, str]:
    """Heading keys of one fiche -> an original spelling: explicit headings, labels, short lines."""
    found: dict[str, str] = {}
    for line, start, end in iter_lines(text):
        stripped = line.strip()
        if not stripped or MARKER_RE.match(line):
            continue
        heading = detect_heading(line, start, end, lambda key: None)
        if heading is not None and heading.explicit and heading.key:
            found.setdefault(heading.key, heading.title.strip())
            continue
        label = LABEL_RE.match(line)
        if label:
            # Only a label alone on its line ("Pistes :") may be a section name; "Erreur : 0x8007..." is content.
            key = heading_key(label.group(1))
            if key and not label.group(2).strip():
                found.setdefault(key, label.group(1).strip())
            continue
        if len(stripped) <= 40 and not stripped.endswith((".", "!", "?", ";", ",")) and len(stripped.split()) <= 5:
            key = heading_key(stripped)
            if key and first_verb(stripped) is None:
                found.setdefault(key, stripped.rstrip(":").strip())
    return found


def _boilerplate_candidates(text: str) -> set[str]:
    lines = set()
    for line, _, _ in iter_lines(text):
        stripped = line.strip()
        if len(stripped) < 20 or MARKER_RE.match(line):
            continue
        if first_verb(stripped) is not None and not BOILERPLATE_MARKERS.search(stripped):
            continue
        lines.add(normalize(stripped))
    return lines


def _half(fiche_id: str) -> int:
    return hashlib.sha256(fiche_id.encode("utf-8")).digest()[0] % 2


def _frequent(counter: Counter, total: int, share: float, floor: int) -> set[str]:
    threshold = max(floor, share * total)
    return {key for key, count in counter.items() if count >= threshold}


# --- dynamic software dictionary (V10 pilier 2) --------------------------------------------
#
# Replaces the hard-coded APPS table of kecore.entities for client-specific business software
# (an ERP, a client portal...) that no generic list can know in advance. A fixed OS table stays
# fine on its own — an operating system is not client-specific — so only applications are built
# dynamically here. The mechanism is the same for every client: a trigger word ("application",
# "l'ERP", "le logiciel"...) followed by a capitalized name, counted across distinct fiches, kept
# only once it is not a one-off. No client name or product name is ever hard-coded.

_DICTIONARY_TRIGGERS = (
    "application", "applicatif", "logiciel", "outil", "erp", "progiciel", "systeme", "système",
    "plateforme", "interface", "portail", "module", "solution", "service",
)
_DICTIONARY_TRIGGER_RE = re.compile(
    r"\b(?i:l['’]|le |la |les |l'|un |une |du |de la |notre |votre )*(?i:" + "|".join(_DICTIONARY_TRIGGERS) + r")\s+"
    # the captured name itself stays case-sensitive: only a capitalized word is a candidate product name.
    r"([A-Z][\w&-]{1,24}(?:\s+(?:[A-Z][\w&-]{1,24}|&|et|and))*)"
)
_DICTIONARY_STOPWORDS = frozenset({
    "windows", "microsoft", "office", "outlook", "teams", "word", "excel", "powerpoint", "onedrive",
    "sharepoint", "exchange", "edge", "chrome", "firefox", "citrix", "vpn", "wifi", "active", "directory",
})


def _dictionary_candidates(text: str) -> set[str]:
    """Candidate surface forms of client software mentioned in one fiche (deduplicated within it)."""
    found: set[str] = set()
    for match in _DICTIONARY_TRIGGER_RE.finditer(text):
        term = " ".join(match.group(1).split())
        key = term.lower()
        if key in _DICTIONARY_STOPWORDS or len(key) < 2:
            continue
        found.add(term)
    return found


def build_dictionary(fiches, llm=None, min_fiches: int = 3) -> tuple[dict[str, list[str]], dict]:
    """The client's own software dictionary, built from its corpus (V10 pilier 2).

    Deterministic by default: a candidate becomes a dictionary entry only once it names at
    least ``min_fiches`` distinct fiches, never from a single mention. With ``llm`` given,
    near-duplicate spellings of the same product ("Harmony" / "ERP Harmony") are clustered
    under one canonical id; without it, each distinct spelling stays its own entry — still
    correct, just less consolidated. Mirrors build_profile's optional LLM step for headings.
    """
    fiches = list(fiches)
    term_counts: Counter = Counter()
    for fiche in fiches:
        for term in _dictionary_candidates(fiche.text):
            term_counts[term] += 1

    kept = sorted({term for term, count in term_counts.items() if count >= min_fiches})
    usage: dict = {}
    if not kept:
        return {}, usage

    if llm is not None:
        from .llm import LLMError
        from .llm_segment import llm_dictionary_aliases

        try:
            clusters, llm_usage = llm_dictionary_aliases(llm, kept)
            usage = asdict(llm_usage)
        except LLMError as exc:
            clusters, usage = {}, {"error": str(exc)}
        if clusters:
            return {k: sorted(set(v)) for k, v in clusters.items()}, usage

    # No LLM (or it failed): each surface form is its own entry, canonicalized by slugging.
    fallback: dict[str, list[str]] = {}
    for term in kept:
        canonical = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        if canonical:
            fallback.setdefault(canonical, []).append(term)
    return fallback, usage


def build_profile(client: str, fiches, llm=None, heading_share: float = 0.1, boilerplate_share: float = 0.3,
                   with_dictionary: bool = True, dictionary_min_fiches: int = 3) -> Profile:
    fiches = list(fiches)
    total = len(fiches)
    heading_counts: Counter = Counter()
    halves = (Counter(), Counter())
    examples: dict[str, str] = {}
    boiler_counts: Counter = Counter()
    styles: Counter = Counter()
    for fiche in fiches:
        candidates = _heading_candidates(fiche.text)
        heading_counts.update(candidates.keys())
        halves[_half(fiche.fiche_id)].update(candidates.keys())
        for key, spelling in candidates.items():
            examples.setdefault(key, spelling)
        boiler_counts.update(_boilerplate_candidates(fiche.text))
        markers = [MARKER_RE.match(line) for line, _, _ in iter_lines(fiche.text)]
        markers = [m for m in markers if m]
        if any(m.group("number") or m.group("word_number") for m in markers):
            styles["numbered"] += 1
        elif markers:
            styles["bulleted"] += 1
        else:
            styles["prose"] += 1

    frequent = _frequent(heading_counts, total, heading_share, 2)
    known = {key for key in heading_counts if keyword_role(key) is not None}
    headings: dict[str, dict] = {}
    for key in sorted(known | frequent):
        role = keyword_role(key)
        headings[key] = {
            "count": heading_counts[key],
            "role": role,
            "source": "rules" if role else "unmapped",
            "example": examples.get(key, key),
        }

    usage = {}
    unmapped = [key for key, entry in headings.items() if entry["source"] == "unmapped"]
    if llm is not None and unmapped:
        from .llm_segment import llm_heading_roles

        from .llm import LLMError

        spellings = {headings[key]["example"]: key for key in unmapped}
        try:
            mappings, llm_usage = llm_heading_roles(llm, sorted(spellings))
            usage = asdict(llm_usage)
        except LLMError as exc:
            mappings, usage = {}, {"error": str(exc)}
        for spelling, role in mappings.items():
            key = spellings.get(spelling) or heading_key(spelling)
            if key in headings and headings[key]["source"] == "unmapped" and role != "other":
                headings[key].update(role=role, source="llm")

    first, second = (_frequent(h, max(1, total // 2), heading_share, 2) for h in halves)
    union = first | second
    stability = len(first & second) / len(union) if union else 1.0
    stable = total < 10 or stability >= 0.5

    boilerplate = sorted(_frequent(boiler_counts, total, boilerplate_share, 3) - set(heading_counts))

    dictionary: dict[str, list[str]] = {}
    dictionary_usage: dict = {}
    if with_dictionary:
        dictionary, dictionary_usage = build_dictionary(fiches, llm=llm, min_fiches=dictionary_min_fiches)

    return Profile(
        client=client,
        fiches=total,
        headings=headings,
        boilerplate=boilerplate,
        styles=dict(styles),
        stable=stable,
        stability=round(stability, 3),
        llm_usage=usage,
        dictionary=dictionary,
        dictionary_usage=dictionary_usage,
    )


def remove_boilerplate(text: str, profile: Profile | None) -> tuple[str, int]:
    if profile is None or not profile.boilerplate:
        return text, 0
    banned = set(profile.boilerplate)
    kept = []
    removed = 0
    for line in text.split("\n"):
        if line.strip() and normalize(line.strip()) in banned:
            removed += 1
            continue
        kept.append(line)
    cleaned = "\n".join(kept)
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip(), removed
