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
from pathlib import Path, PurePosixPath

from .entities import APPS, OPERATING_SYSTEMS
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
    # how the dictionary was built (candidates, what the LLM kept, what the corpus check dropped) and the
    # entries a person rejected (never proposed again)
    dictionary_stats: dict = field(default_factory=dict)
    dictionary_rejected: list[str] = field(default_factory=list)
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
# A product name keeps its capital letter; a common word caught after a trigger ("le service Desk",
# "the service Request") is mostly written in lower case elsewhere. Measured on client-s
# (2026-10-06), links and paths aside: "Desk" 54 % capitalized, "Request" 32 %, against AutoCAD 100 %,
# VEEAM 98 %, Unity 94 %. Below this share a candidate is not a name.
NAME_CAPITALIZED_SHARE = 0.8
_LINK_OR_PATH_RE = re.compile(r"(?i)\bhttps?://\S+|\bwww\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+|\\\\\S+|\b[a-z]:\\\S*|%\w+%\S*")


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


def capitalized_share(term: str, texts) -> float:
    """Share of the occurrences of ``term`` (any case) written with a capital first letter, outside
    links, e-mail addresses and paths, where names are lower-cased by convention."""
    pattern = re.compile(r"(?<![\w.-])" + r"\s+".join(re.escape(word) for word in term.split()) + r"(?![\w-])",
                         re.IGNORECASE)
    total = capitalized = 0
    for text in texts:
        for match in pattern.finditer(_LINK_OR_PATH_RE.sub(" ", text)):
            total += 1
            capitalized += match.group()[0].isupper()
    return capitalized / total if total else 0.0


# --- names of the documents: candidates the trigger words miss ----------------------------
#
# Measured on client-s (2026-10-06): its products are named in its document names without any trigger
# word ("How to Install AutoCad", "VEEAM-Appel_Support", "UNITY Autopilot PC Deploy"), so the trigger
# rule found none of them. The words written like names in the document names are candidates; code
# alone cannot tell a product (VEEAM) from a place name, a parent-company name or a heading
# ("General"), so the LLM classifies them and the code keeps only what the fiches really write.

NAME_MIN_FICHES = 2
MAX_NAME_SHARE = 0.5
MAX_NAME_CANDIDATES = 150
_WORD_RE = re.compile(r"(?<![\w.-])\w+(?![\w-])")
_NAME_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{2,}\b")
_FICHE_NUMBER_RE = re.compile(r"(?i)^(?:KB?\d+|\d+)$")
_KNOWN_NAMES = frozenset(alias.lower() for table in (APPS, OPERATING_SYSTEMS) for aliases in table.values()
                         for alias in aliases)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _document_names(fiches) -> list[str]:
    """Per fiche: its document name, its id and its title, unless several fiches share that title
    (a template heading such as "General Information" is not a name)."""
    shared = Counter(f.title for f in fiches)
    names = []
    for fiche in fiches:
        source = getattr(fiche, "source", "") or ""
        stem = PurePosixPath(source.replace("\\", "/")).stem if source else ""
        own = (stem, fiche.fiche_id, fiche.title if fiche.title and shared[fiche.title] == 1 else "")
        names.append("\n".join(dict.fromkeys(n for n in own if n)))
    return names


def _occurrence_re(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w.-])" + r"\s+".join(re.escape(word) for word in name.split()) + r"(?![\w-])",
                      re.IGNORECASE)


def name_candidates(fiches, min_fiches: int = NAME_MIN_FICHES,
                    limit: int = MAX_NAME_CANDIDATES) -> list[tuple[str, int, list[str]]]:
    """Words written like names in the document names (VEEAM, AutoCad, LogMeIn), with the number of
    fiches that name them and two document names as examples. Kept: in at least ``min_fiches``
    fiches but not in most of them (a word in more than ``MAX_NAME_SHARE`` of the fiches is template
    vocabulary: "Document History" heads 221 of the 242 client-s fiches), written with their capital
    letter (``NAME_CAPITALIZED_SHARE``, as ``capitalized_share`` counts it), not a fiche number, not
    already in the static tables of kecore.entities."""
    fiches = list(fiches)
    names = _document_names(fiches)
    spelling: dict[str, str] = {}
    examples: dict[str, list[str]] = {}
    for name in names:
        label = name.split("\n", 1)[0]
        for token in _NAME_TOKEN_RE.findall(name):
            key = token.lower()
            if key in _KNOWN_NAMES or key in _DICTIONARY_STOPWORDS or _FICHE_NUMBER_RE.match(token):
                continue
            spelling.setdefault(key, token)
            seen = examples.setdefault(key, [])
            if len(seen) < 2 and label not in seen:
                seen.append(label)
    # one pass over the corpus: in how many fiches each word is, and how often it is capitalized
    in_fiches: Counter = Counter()
    written: Counter = Counter()
    capitalized: Counter = Counter()
    for name, fiche in zip(names, fiches):
        text = f"{name}\n{fiche.text}"
        in_fiches.update({m.group().lower() for m in _WORD_RE.finditer(text)} & spelling.keys())
        for match in _WORD_RE.finditer(_LINK_OR_PATH_RE.sub(" ", text)):
            key = match.group().lower()
            if key in spelling:
                written[key] += 1
                capitalized[key] += match.group()[0].isupper()
    ceiling = max(10, MAX_NAME_SHARE * len(fiches))
    candidates = []
    for key, token in spelling.items():
        count = in_fiches[key]
        if min_fiches <= count <= ceiling and written[key] and capitalized[key] / written[key] >= NAME_CAPITALIZED_SHARE:
            candidates.append((token, count, examples[key]))
    candidates.sort(key=lambda c: (-c[1], c[0].lower()))
    return candidates[:limit]


def build_dictionary(fiches, llm=None, min_fiches: int = 3, rejected=(),
                     report: dict | None = None) -> tuple[dict[str, list[str]], dict]:
    """The client's own software dictionary, built from its corpus (V10 pilier 2).

    Candidates: a capitalized name after a trigger word ("le logiciel X") in at least ``min_fiches``
    fiches, and, when an LLM is given, the words written like names in the document names
    (``name_candidates``). Every candidate must be written as a name (``NAME_CAPITALIZED_SHARE`` of
    its occurrences capitalized). The LLM keeps the products among them and groups their spellings;
    the code then keeps a spelling only if it builds on a candidate and is written in at least
    ``NAME_MIN_FICHES`` fiches, so the LLM can add nothing the KB does not say. Without an LLM, or if
    it fails, each trigger-word candidate is its own entry (document-name candidates need the LLM:
    alone they mix products with places and headings). An entry a person ``rejected`` (its id or one
    of its spellings) is never proposed again.

    Returns the dictionary and the LLM usage (``{"error": ...}`` when it failed); ``report``, when
    given, receives the counts: candidates, products the LLM named, spellings the corpus check dropped.
    """
    fiches = list(fiches)
    texts = [fiche.text for fiche in fiches]
    term_counts: Counter = Counter()
    for fiche in fiches:
        for term in _dictionary_candidates(fiche.text):
            term_counts[term] += 1
    kept = sorted({term for term, count in term_counts.items()
                   if count >= min_fiches and capitalized_share(term, texts) >= NAME_CAPITALIZED_SHARE})
    refused = {r.strip().lower() for r in rejected if isinstance(r, str) and r.strip()}
    stats = report if report is not None else {}
    stats["trigger_terms"] = len(kept)
    usage: dict = {}

    if llm is not None:
        from .llm import LLMError
        from .llm_segment import llm_dictionary_products

        names = [c for c in name_candidates(fiches) if c[0] not in kept]
        stats["name_candidates"] = len(names)
        candidates = [(term, term_counts[term], []) for term in kept] + names
        if candidates:
            try:
                products, llm_usage = llm_dictionary_products(llm, candidates)
                usage = asdict(llm_usage)
            except LLMError as exc:
                products = None
                usage = {"error": str(exc)}
            if products is not None:
                dictionary, dropped = _verified(products, fiches, candidates, refused)
                stats.update(llm_products=len(products), dropped_by_corpus_check=dropped)
                return dictionary, usage

    # No LLM (or it failed): each trigger-word spelling is its own entry, canonicalized by slugging.
    fallback: dict[str, list[str]] = {}
    for term in kept:
        canonical = _slug(term)
        if canonical and canonical not in refused and term.lower() not in refused:
            fallback.setdefault(canonical, []).append(term)
    return fallback, usage


def _verified(products: dict[str, list[str]], fiches, candidates, refused: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    """What the LLM proposed, kept only where the client's fiches write it."""
    corpus = [f"{n}\n{f.text}" for n, f in zip(_document_names(fiches), fiches)]
    words = {word.lower() for name, _, _ in candidates for word in name.split()}
    dictionary: dict[str, list[str]] = {}
    dropped: list[str] = []
    for name, aliases in sorted(products.items()):
        key = _slug(name)
        if not key or key in APPS or key in OPERATING_SYSTEMS or key in refused or name.lower() in refused:
            continue
        spellings = []
        for alias in dict.fromkeys(aliases):
            lowered = alias.lower()
            if lowered in _KNOWN_NAMES or lowered in refused:
                continue
            pattern = _occurrence_re(alias)
            if not {w.lower() for w in alias.split()} & words or sum(1 for t in corpus if pattern.search(t)) < NAME_MIN_FICHES:
                dropped.append(alias)
                continue
            spellings.append(alias)
        if spellings:
            dictionary.setdefault(key, [])
            dictionary[key] = sorted(set(dictionary[key]) | set(spellings))
    return dictionary, dropped


def build_profile(client: str, fiches, llm=None, heading_share: float = 0.1, boilerplate_share: float = 0.3,
                   with_dictionary: bool = True, dictionary_min_fiches: int = 3, dictionary_rejected=()) -> Profile:
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
    dictionary_stats: dict = {}
    if with_dictionary:
        dictionary, dictionary_usage = build_dictionary(fiches, llm=llm, min_fiches=dictionary_min_fiches,
                                                        rejected=dictionary_rejected, report=dictionary_stats)

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
        dictionary_stats=dictionary_stats,
        dictionary_rejected=sorted({r.strip() for r in dictionary_rejected if isinstance(r, str) and r.strip()}),
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
