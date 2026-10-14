"""System exclusion: fiches that are not procedures never reach the map (rule 3 of the routing spec).

A KB keeps placeholder documents next to real fiches -- "KB0266 - LIBRE - A REUTILISER" (a number
kept free for reuse), "NE PAS UTILISER" -- and fiches with no usable content. Indexed like the others
they come back as candidates for real questions. This rule removes them from the run's map, by code
only, before the graph, the semantic index and every decision:

- a placeholder: the fiche's normalized title or document name contains one of the client's
  patterns as whole words (case, accents and punctuation ignored, so "LIBRE - A REUTILISER",
  "Libre à réutiliser" and "libre_a_reutiliser" all match "A REUTILISER");
- empty: no verified step AND under ``min_chars`` characters of body -- the text once the client's
  boilerplate and the fiche's own title are removed, whitespace not counted. The threshold is low on
  purpose: a short fiche of plain information is real content ("KB0010020 - Politique des mots de
  passe", 12 characters minimum, 90 days, 0 step, 126 characters of body, is kept); only a fiche
  with next to nothing in it ("A compléter.") is empty. 200 characters, the first value considered,
  excluded that policy fiche: measured on the demo KB, not assumed.

Nothing is ever dropped silently: every exclusion is returned with its rule and detail, written to the
run's ``excluded.json`` and counted in its summary. A person can force a fiche back in
(``force_include``), whatever the rules say; that decision lives in the client's
``exclusion-config.json`` in its kecore container and is read by every run.

A ``force_include`` entry names a fiche by its exact id. A fiche id is often the document name, spaces
and dashes verbatim ("KB0233 -  Associate a phone line", double space included), and report.md used to
print ids with their spaces collapsed, so an entry copied from it, or typed by hand, named nothing and
the fiche stayed excluded without a word (runbook 19.18 #5). Hence, in ``apply``: an entry equal to no
fiche id falls back to the ONE fiche whose id is equal once whitespace is collapsed and case folded
(``loose_key``) -- never to a guess between two, never across a different character (an en dash is not a
hyphen). And every outcome is reported: ``forced`` (the fiches an entry actually kept out of an
exclusion), ``matched_loosely`` (the entries to correct) and ``force_include_unmatched`` (the entries
that rescued nothing).

The default patterns are deliberately narrow: a bare "LIBRE" or "OBSOLETE" would also match real
titles ("espace disque libre", "supprimer les comptes obsolètes"); a client widens its own list.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import PurePosixPath

from .decompose import DecomposedFiche

DEFAULT_TITLE_PATTERNS = ("A REUTILISER", "NE PAS UTILISER")
DEFAULT_MIN_CHARS = 40
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize(text: str) -> str:
    """Upper case, no accents, every run of non-alphanumeric characters a single space, padded."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).upper()
    return " " + " ".join(_NON_ALNUM.sub(" ", plain).split()) + " "


def loose_key(text: str) -> str:
    """The form a ``force_include`` entry is compared in when it equals no fiche id: every run of
    whitespace (non-breaking spaces and tabs included) one space, trimmed, and case folded the Unicode
    way (canonical caseless match, so an "é" typed as one character equals the "e" + accent a Mac
    document name stores: both render the same, a person cannot tell them apart). Nothing else is
    ignored: unlike ``normalize``, punctuation and accents still count, so "KB0233 - Vide" never names
    "KB0233 – Vide" (en dash) and "Reinitialiser" never names "Réinitialiser" -- a loose match is a copy
    that lost its spacing or case, not a different id."""
    folded = unicodedata.normalize("NFD", text or "").casefold()
    return unicodedata.normalize("NFC", " ".join(folded.split()))


@dataclass(frozen=True)
class ExclusionRules:
    title_patterns: tuple[str, ...] = DEFAULT_TITLE_PATTERNS
    min_chars: int = DEFAULT_MIN_CHARS
    force_include: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict | None) -> "ExclusionRules":
        """The client's exclusion-config.json; a missing key keeps its default."""
        data = data or {}
        patterns = data.get("title_patterns", DEFAULT_TITLE_PATTERNS)
        min_chars = data.get("min_chars", DEFAULT_MIN_CHARS)
        force = data.get("force_include", ())
        if not isinstance(patterns, (list, tuple)) or not all(isinstance(p, str) for p in patterns):
            raise ValueError("title_patterns must be a list of strings")
        if not isinstance(min_chars, int) or isinstance(min_chars, bool) or min_chars < 0:
            raise ValueError("min_chars must be a non-negative integer")
        if not isinstance(force, (list, tuple)) or not all(isinstance(f, str) for f in force):
            raise ValueError("force_include must be a list of fiche ids")
        patterns = tuple(p for p in patterns if normalize(p).strip())
        return cls(title_patterns=patterns, min_chars=min_chars, force_include=tuple(force))

    def to_dict(self) -> dict:
        return {"title_patterns": list(self.title_patterns), "min_chars": self.min_chars,
                "force_include": list(self.force_include)}


@dataclass(frozen=True)
class Exclusion:
    fiche_id: str
    title: str
    source: str
    rule: str          # "title_pattern" | "empty"
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def body_chars(fiche: DecomposedFiche) -> int:
    """Non-whitespace characters of the fiche's text without its own title (a fiche holding only its
    title is empty however long the title)."""
    text = " ".join((fiche.text or "").split())
    title = " ".join((fiche.title or "").split())
    if title:
        text = text.replace(title, " ", 1)
    return len("".join(text.split()).lstrip("#"))


def exclusion_of(fiche: DecomposedFiche, rules: ExclusionRules) -> Exclusion | None:
    """Why this fiche is excluded, or None to keep it. ``force_include`` is matched exactly here: the
    loose match needs every fiche id of the run (it must name exactly one), so it lives in ``apply``."""
    if fiche.fiche_id in rules.force_include:
        return None
    names = normalize(fiche.title) + normalize(PurePosixPath(fiche.source or "").stem)
    for pattern in rules.title_patterns:
        if normalize(pattern) in names:
            return Exclusion(fiche.fiche_id, fiche.title, fiche.source, "title_pattern", pattern)
    chars = body_chars(fiche)
    if not fiche.steps and chars < rules.min_chars:
        return Exclusion(fiche.fiche_id, fiche.title, fiche.source, "empty",
                         f"no verified step, {chars} characters of body (minimum {rules.min_chars})")
    return None


def resolve_force_include(entries: Iterable[str], fiche_ids: Iterable[str]) -> tuple[set[str], list[dict], list[str]]:
    """Which fiches the ``force_include`` entries name: ``(ids, matched_loosely, unmatched)``.

    An entry equal to a fiche id names that fiche, even when another id is equal to it loosely: an exact
    id is what a person copied from excluded.json, and it is never second-guessed. Otherwise the entry
    names the one fiche with the same ``loose_key`` and is listed in ``matched_loosely`` as
    {"entry", "fiche_id"} (the exact id to write instead). Two fiches or more with that key -- ids that
    differ only by spacing or case -- are as unmatched as none: forcing either back in would be a guess,
    and forcing both would keep a fiche nobody named. Each distinct entry is considered once, in the
    config's order, so the lists are deterministic and an entry repeated is reported once."""
    present = set(fiche_ids)
    by_key: dict[str, list[str]] = {}
    for fiche_id in sorted(present):
        by_key.setdefault(loose_key(fiche_id), []).append(fiche_id)
    ids: set[str] = set()
    loosely: list[dict] = []
    unmatched: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        if entry in present:
            ids.add(entry)
            continue
        candidates = by_key.get(loose_key(entry), [])
        if len(candidates) == 1:
            ids.add(candidates[0])
            loosely.append({"entry": entry, "fiche_id": candidates[0]})
        else:
            unmatched.append(entry)
    return ids, loosely, unmatched


@dataclass
class ExclusionResult:
    kept: list[DecomposedFiche]
    excluded: list[Exclusion] = field(default_factory=list)
    rules: ExclusionRules = field(default_factory=ExclusionRules)
    # ids of the fiches force_include kept although the rules exclude them, in the run's order (an entry
    # naming a fiche the rules keep anyway is not listed: it no longer does anything)
    forced: list[str] = field(default_factory=list)
    # force_include entries that named no fiche of the run, verbatim, in the config's order
    force_include_unmatched: list[str] = field(default_factory=list)
    # [{"entry", "fiche_id"}]: entries that named a fiche only once spaces and case were ignored
    matched_loosely: list[dict] = field(default_factory=list)

    def _force_include_report(self) -> dict:
        return {"forced": list(self.forced), "force_include_unmatched": list(self.force_include_unmatched),
                "matched_loosely": [dict(m) for m in self.matched_loosely]}

    def to_dict(self) -> dict:
        return {"rules": self.rules.to_dict(), "kept": len(self.kept), "excluded": [e.to_dict() for e in self.excluded],
                **self._force_include_report()}

    def stats(self) -> dict:
        by_rule: dict[str, int] = {}
        for e in self.excluded:
            by_rule[e.rule] = by_rule.get(e.rule, 0) + 1
        return {"kept": len(self.kept), "excluded": len(self.excluded), "by_rule": by_rule,
                "fiche_ids": [e.fiche_id for e in self.excluded], **self._force_include_report()}


def apply(decomposed: list[DecomposedFiche], rules: ExclusionRules | None = None) -> ExclusionResult:
    """Splits the run's fiches into those kept for the map and those excluded, in their order, and says
    what each ``force_include`` entry did (see ``resolve_force_include``)."""
    rules = rules or ExclusionRules()
    forced_ids, loosely, unmatched = resolve_force_include(rules.force_include, [f.fiche_id for f in decomposed])
    by_rules = replace(rules, force_include=())
    result = ExclusionResult(kept=[], rules=rules, force_include_unmatched=unmatched, matched_loosely=loosely)
    for fiche in decomposed:
        why = exclusion_of(fiche, by_rules)
        if why is None:
            result.kept.append(fiche)
        elif fiche.fiche_id in forced_ids:
            result.kept.append(fiche)
            if fiche.fiche_id not in result.forced:
                result.forced.append(fiche.fiche_id)
        else:
            result.excluded.append(why)
    return result


__all__ = ["DEFAULT_TITLE_PATTERNS", "DEFAULT_MIN_CHARS", "ExclusionRules", "Exclusion", "ExclusionResult",
           "body_chars", "exclusion_of", "apply", "normalize", "loose_key", "resolve_force_include"]
