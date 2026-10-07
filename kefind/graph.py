"""Relations between a client's fiches, computed by code from the decomposed KB (V10 slice 3).

Nothing here is guessed or generated: every relation points at a file name or a sentence.

- KB number: each fiche's own number, read from its id or title (``KB0052``, ``KB 52``,
  ``kb-0052`` all give 52).
- Duplicates: two fiches whose texts share ``NEAR_DUPLICATE`` or more of their 5-word
  sequences (shingles), or that carry the same number and share ``SAME_NUMBER_DUPLICATE``
  or more (two versions of one fiche). They form a duplicate group.
- Number conflicts: two fiches with the same number but different texts. Measured on
  client-s (2026-10-06): KB0076 is both "Global Protect - Fixing Connection Issue" and
  "User not Appearing in Mysupport". A shared number alone never merges two fiches.
- References: a fiche citing another fiche's number in its text. When the citation sits in a
  step whose role is ``prerequisite``, the cited fiche is a prerequisite of the citing one.
- Replacement: an explicit sentence ("remplace la fiche KB0052", "replaced by KB0120").

Each duplicate group keeps one canonical fiche (guided before citable before info_only, then
the one with more steps, then one that carries a number, then the smaller id). ``prune`` maps a duplicate to its canonical
fiche and a replaced fiche to the fiche replacing it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from itertools import combinations

from kecore.decompose import DecomposedFiche

from .search import tokenize

GRAPH_VERSION = 1
KB_NUMBER_RE = re.compile(r"(?i)\bKB\s?[-_]?0*(\d{1,7})\b")
NEAR_DUPLICATE = 0.8
SAME_NUMBER_DUPLICATE = 0.5
SHINGLE = 5
_REPLACES_RE = re.compile(
    r"(?i)\b(?:annule et remplace|remplace|replaces|supersedes)\b[^.\n]{0,40}?\bKB\s?[-_]?0*(\d{1,7})\b")
_REPLACED_BY_RE = re.compile(
    r"(?i)\b(?:remplac[ée]e? par|obsol[eè]te[^.\n]{0,20}?par|replaced by|superseded by)\b[^.\n]{0,40}?\bKB\s?[-_]?0*(\d{1,7})\b")
_STATUS_RANK = {"guided": 0, "citable": 1, "info_only": 2}


@dataclass
class FicheNode:
    fiche_id: str
    numbers: list[int] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    superseded_by: list[str] = field(default_factory=list)


@dataclass
class KBGraph:
    nodes: dict[str, FicheNode] = field(default_factory=dict)
    groups: list[list[str]] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    missing: dict[str, list[int]] = field(default_factory=dict)
    version: int = GRAPH_VERSION

    def to_dict(self) -> dict:
        return {"version": self.version, "nodes": {k: asdict(v) for k, v in sorted(self.nodes.items())},
                "groups": self.groups, "conflicts": self.conflicts, "missing": self.missing, "stats": self.stats()}

    @classmethod
    def from_dict(cls, data: dict) -> "KBGraph":
        return cls(nodes={k: FicheNode(**v) for k, v in data.get("nodes", {}).items()},
                   groups=[list(g) for g in data.get("groups", [])],
                   conflicts={k: list(v) for k, v in data.get("conflicts", {}).items()},
                   missing={k: list(v) for k, v in data.get("missing", {}).items()},
                   version=data.get("version", GRAPH_VERSION))

    def stats(self) -> dict:
        return {
            "fiches": len(self.nodes),
            "with_number": sum(1 for n in self.nodes.values() if n.numbers),
            "references": sum(len(n.references) for n in self.nodes.values()),
            "prerequisites": sum(len(n.prerequisites) for n in self.nodes.values()),
            "duplicate_groups": len(self.groups),
            "duplicates": sum(len(g) - 1 for g in self.groups),
            "number_conflicts": len(self.conflicts),
            "superseded": sum(1 for n in self.nodes.values() if n.superseded_by),
        }

    def canonical(self, fiche_id: str) -> str:
        node = self.nodes.get(fiche_id)
        if node is not None and node.duplicate_of and node.duplicate_of in self.nodes:
            return node.duplicate_of
        return fiche_id

    def resolve_number(self, number: int) -> list[str]:
        """The canonical fiches carrying this number: one, several (number conflict) or none."""
        return sorted({self.canonical(n.fiche_id) for n in self.nodes.values() if number in n.numbers})

    def prune(self, fiche_ids) -> tuple[list[str], list[dict]]:
        """Candidates after the graph: a duplicate becomes its canonical fiche, a replaced fiche the
        fiche replacing it. Order is kept (first occurrence wins). Returns the pruned list and one
        trace entry per change."""
        kept: list[str] = []
        changes: list[dict] = []
        for fiche_id in fiche_ids:
            target = self.canonical(fiche_id)
            if target != fiche_id:
                changes.append({"fiche_id": fiche_id, "change": "duplicate_of", "to": target})
            node = self.nodes.get(target)
            replacements = sorted({self.canonical(r) for r in node.superseded_by if r in self.nodes}) if node else []
            if replacements:
                changes.append({"fiche_id": target, "change": "superseded_by", "to": replacements[0]})
                target = replacements[0]
            if target not in kept:
                kept.append(target)
        return kept, changes


def own_numbers(fiche: DecomposedFiche) -> list[int]:
    for candidate in (fiche.fiche_id, fiche.title or ""):
        match = KB_NUMBER_RE.search(candidate)
        if match:
            return [int(match.group(1))]
    return []


def _shingles(text: str) -> set[tuple[str, ...]]:
    tokens = tokenize(text)
    if len(tokens) < SHINGLE:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_graph(fiches: list[DecomposedFiche]) -> KBGraph:
    graph = KBGraph()
    by_id = {f.fiche_id: f for f in fiches}
    ids = sorted(by_id)
    owners: dict[int, list[str]] = {}
    for fiche_id in ids:
        node = FicheNode(fiche_id=fiche_id, numbers=own_numbers(by_id[fiche_id]))
        graph.nodes[fiche_id] = node
        for number in node.numbers:
            owners.setdefault(number, []).append(fiche_id)
    shingles = {fiche_id: _shingles(by_id[fiche_id].text) for fiche_id in ids}

    # duplicate groups (union-find over fiche ids)
    parent = {fiche_id: fiche_id for fiche_id in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a, b in combinations(ids, 2):
        small, large = sorted((len(shingles[a]), len(shingles[b])))
        if not large or small / large < SAME_NUMBER_DUPLICATE:
            continue  # exact bound: the Jaccard index never exceeds the ratio of the two sizes
        same_number = bool(set(graph.nodes[a].numbers) & set(graph.nodes[b].numbers))
        if small / large < NEAR_DUPLICATE and not same_number:
            continue
        similarity = _jaccard(shingles[a], shingles[b])
        if similarity >= NEAR_DUPLICATE or (same_number and similarity >= SAME_NUMBER_DUPLICATE):
            union(a, b)
    members: dict[str, list[str]] = {}
    for fiche_id in ids:
        members.setdefault(find(fiche_id), []).append(fiche_id)
    for group in members.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda i: (_STATUS_RANK.get(by_id[i].status, 3), -len(by_id[i].steps),
                                               0 if graph.nodes[i].numbers else 1, i))
        graph.groups.append(ordered)
        for duplicate in ordered[1:]:
            graph.nodes[duplicate].duplicate_of = ordered[0]
    graph.groups.sort()
    for number, holders in sorted(owners.items()):
        distinct = sorted({graph.canonical(h) for h in holders})
        if len(distinct) > 1:
            graph.conflicts[str(number)] = distinct

    # references, prerequisites and replacements, from the fiche's own sentences
    for fiche_id in ids:
        fiche, node = by_id[fiche_id], graph.nodes[fiche_id]
        own = set(node.numbers)
        prerequisite_text = " ".join(s.text for s in fiche.steps if s.role == "prerequisite")
        prerequisite_numbers = {int(m.group(1)) for m in KB_NUMBER_RE.finditer(prerequisite_text)}
        missing: set[int] = set()
        for match in KB_NUMBER_RE.finditer(fiche.text):
            number = int(match.group(1))
            if number in own:
                continue
            targets = graph.resolve_number(number)
            if not targets:
                missing.add(number)
                continue
            for target in targets:
                if target == graph.canonical(fiche_id):
                    continue
                if target not in node.references:
                    node.references.append(target)
                if number in prerequisite_numbers and target not in node.prerequisites:
                    node.prerequisites.append(target)
        if missing:
            graph.missing[fiche_id] = sorted(missing)
        for pattern, direction in ((_REPLACES_RE, "replaces"), (_REPLACED_BY_RE, "replaced_by")):
            for match in pattern.finditer(fiche.text):
                for target in graph.resolve_number(int(match.group(1))):
                    if target == graph.canonical(fiche_id):
                        continue
                    if direction == "replaces":
                        if fiche_id not in graph.nodes[target].superseded_by:
                            graph.nodes[target].superseded_by.append(fiche_id)
                    elif target not in node.superseded_by:
                        node.superseded_by.append(target)
    for node in graph.nodes.values():
        node.references.sort()
        node.prerequisites.sort()
        node.superseded_by.sort()
    return graph


__all__ = ["GRAPH_VERSION", "KB_NUMBER_RE", "NEAR_DUPLICATE", "SAME_NUMBER_DUPLICATE", "FicheNode", "KBGraph",
           "build_graph", "own_numbers"]
