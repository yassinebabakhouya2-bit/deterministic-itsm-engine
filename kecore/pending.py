"""Online feedback loop for the per-client software dictionary (V10 pilier 2).

``profile.build_dictionary`` learns a client's software dictionary once, offline, from its KB
corpus: a term becomes an entry only once it names several distinct fiches. New vocabulary shows
up later too, in live traffic (real tickets, real questions) that never goes through a KB
decomposition run. This module gives that traffic a second, independent path into the same
dictionary — same discipline (a term counts once per distinct text, never a one-off mention,
never a per-client special case), but counted across live observations instead of fiches, and
with an explicit human decision before anything is merged (unlike the offline corpus scan, this
loop was decided to stay human-validated: a pending candidate only ever reaches
``Profile.dictionary`` through ``apply_decision(..., accept=True)``, never automatically).

Lifecycle of one candidate term: unseen -> pending (observed, counted)
                                        -> accepted (merged into profile.dictionary)
                                        -> rejected (kept, never proposed again)

Storage-agnostic on purpose: this module only holds the rules. Persistence is
the caller's job, through ``to_dict`` / ``from_dict`` (V10 on Azure: one Table
Storage partition per client, written by the engine's Function / Web App, never
a file on an operator's machine).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .profile import Profile, _dictionary_candidates

PENDING_VERSION = 1
DEFAULT_MIN_OBSERVATIONS = 3


@dataclass
class PendingSynonyms:
    client: str
    candidates: dict[str, dict] = field(default_factory=dict)  # term (lowercase) -> {spelling, seen, status}
    version: int = PENDING_VERSION

    def to_dict(self) -> dict:
        return {"client": self.client, "candidates": self.candidates, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> "PendingSynonyms":
        return cls(client=data["client"], candidates=data.get("candidates", {}),
                   version=data.get("version", PENDING_VERSION))


def _known_terms(profile: Profile) -> set[str]:
    known = set(profile.dictionary.keys())
    for aliases in profile.dictionary.values():
        known.update(alias.lower() for alias in aliases)
    return known


def observe(pending: PendingSynonyms, texts: Iterable[str], profile: Profile | None = None) -> PendingSynonyms:
    """Record one observation per text for each candidate software mention it contains.

    One ``text`` is one real ticket/question. A candidate's count goes up by at most 1 per text
    (mirrors build_dictionary's "named across distinct fiches" rule, across live traffic instead
    of the corpus). A term the profile's dictionary already knows is skipped — nothing to learn.
    A term already decided (accepted/rejected) stops accumulating — the decision stands.
    """
    known = _known_terms(profile) if profile is not None else set()
    for text in texts:
        seen_in_this_text: set[str] = set()
        for term in _dictionary_candidates(text):
            key = term.lower()
            if key in known or key in seen_in_this_text:
                continue
            seen_in_this_text.add(key)
            entry = pending.candidates.setdefault(key, {"spelling": term, "seen": 0, "status": "pending"})
            if entry["status"] == "pending":
                entry["seen"] += 1
    return pending


def ready_for_review(pending: PendingSynonyms, min_observations: int = DEFAULT_MIN_OBSERVATIONS) -> list[tuple[str, dict]]:
    """Pending candidates that crossed the threshold and await a human decision, most-seen first."""
    items = [(key, entry) for key, entry in pending.candidates.items()
             if entry["status"] == "pending" and entry["seen"] >= min_observations]
    items.sort(key=lambda item: (-item[1]["seen"], item[0]))
    return items


def apply_decision(pending: PendingSynonyms, profile: Profile, key: str, accept: bool,
                    canonical: str | None = None) -> None:
    """Record an operator's decision on one candidate; on accept, merge it into profile.dictionary.

    ``canonical`` lets the operator fold a new spelling into an existing dictionary entry (e.g. a
    second surface form of a product already known under another id); without it, a new entry is
    created, keyed like build_dictionary's own fallback (slugified spelling).
    """
    entry = pending.candidates.get(key)
    if entry is None:
        raise KeyError(f"no pending candidate {key!r}")
    if entry["status"] != "pending":
        raise ValueError(f"{key!r} was already {entry['status']}")
    if accept:
        import re

        target = canonical or re.sub(r"[^a-z0-9]+", "-", key).strip("-")
        bucket = profile.dictionary.setdefault(target, [])
        if entry["spelling"] not in bucket:
            bucket.append(entry["spelling"])
        entry["status"] = "accepted"
    else:
        entry["status"] = "rejected"


__all__ = [
    "PendingSynonyms", "DEFAULT_MIN_OBSERVATIONS", "observe", "ready_for_review", "apply_decision",
]
