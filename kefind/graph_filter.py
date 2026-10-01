"""Temps 3 — Filtrer par le graphe : point d'extension pour la tranche 5.

Le graphe de connaissance (doublons, contradictions, fiches alternatives)
n'est pas encore construit. ``filter_candidates`` ne fait rien pour
l'instant ; un futur filtre (détection de doublons, résolution de
contradictions...) prend la même forme : des candidats triés, un graphe
optionnel, et renvoie des candidats triés.
"""

from __future__ import annotations

from typing import Protocol

from .search import SearchResult


class CandidateFilter(Protocol):
    def __call__(self, candidates: list[SearchResult], graph: object | None = None) -> list[SearchResult]: ...


def filter_candidates(candidates: list[SearchResult], graph: object | None = None) -> list[SearchResult]:
    """No-op pour cette tranche : renvoie les candidats inchangés, dans leur ordre."""
    return list(candidates)


__all__ = ["CandidateFilter", "filter_candidates"]
