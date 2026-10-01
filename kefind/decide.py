"""Temps 4 — Décider par le code : le cœur de la porte de cette tranche.

Jamais de LLM ici. Avec des seuils calibrés (``Thresholds``, calibrables
depuis des tickets étiquetés avec ``scoreboard.metrics.calibrate`` — voir
``kefind.calibration``) :

* une fiche nettement devant (écart avec la deuxième au-dessus de ``gap``)
  → décision "fiche" ;
* plusieurs fiches proches (écart sous ``gap``) → décision "question", une
  question fermée générée à partir de l'attribut qui les discrimine le
  mieux (entités des fiches : application, code d'erreur... jamais le LLM) ;
* le meilleur score sous ``min_score`` → décision "abstain".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .search import SearchResult

# Ordre de préférence des attributs utilisés pour discriminer des fiches proches.
QUESTION_PREFIXES = ("app", "err", "evt", "os")
PREFIX_LABELS = {
    "app": "l'application",
    "err": "le code d'erreur",
    "evt": "l'identifiant d'événement",
    "os": "le système d'exploitation",
}


@dataclass
class Thresholds:
    min_score: float = 0.18  # sous le meilleur score, rien n'est assez sûr : abstain
    gap: float = 0.12  # écart entre le 1er et le 2e nécessaire pour trancher "fiche"

    @classmethod
    def from_dict(cls, data: dict | None) -> "Thresholds":
        data = data or {}
        allowed = {"min_score", "gap"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"seuil(s) inconnu(s) : {', '.join(unknown)}. Attendus : {', '.join(sorted(allowed))}")
        return cls(min_score=float(data.get("min_score", cls.min_score)), gap=float(data.get("gap", cls.gap)))


@dataclass
class Outcome:
    kind: str  # "fiche" | "question" | "abstain"
    candidates: list[SearchResult] = field(default_factory=list)
    close: list[SearchResult] = field(default_factory=list)
    question: str | None = None
    top: SearchResult | None = None


def _discriminator(close: list[SearchResult]) -> tuple[str, dict[str, list[SearchResult]]] | None:
    """Le préfixe d'entité qui sépare le mieux les fiches proches (au moins deux groupes)."""
    for prefix in QUESTION_PREFIXES:
        groups: dict[str, list[SearchResult]] = {}
        for result in close:
            canonicals = {e["canonical"] for e in result.fiche.entities}
            values = sorted(c for c in canonicals if c.startswith(f"{prefix}:"))
            if values:
                groups.setdefault(values[0], []).append(result)
        if len(groups) >= 2:
            return prefix, groups
    return None


def build_question(close: list[SearchResult]) -> str:
    """Question fermée, déterministe, à partir des entités des fiches proches — pas de LLM."""
    found = _discriminator(close)
    if found is None:
        titles = " ou ".join(dict.fromkeys(r.fiche.title for r in close))
        return f"Pouvez-vous préciser de laquelle de ces fiches il s'agit : {titles} ?"
    prefix, groups = found
    label = PREFIX_LABELS.get(prefix, "l'élément concerné")
    options = " ou ".join(value.split(":", 1)[1].replace("-", " ") for value in sorted(groups))
    return f"Pour départager ces fiches, {label} concerné est-il {options} ?"


def decide(candidates: list[SearchResult], thresholds: Thresholds) -> Outcome:
    if not candidates:
        return Outcome(kind="abstain")
    top = candidates[0]
    if top.score < thresholds.min_score:
        return Outcome(kind="abstain", candidates=candidates, top=top)
    if len(candidates) == 1:
        return Outcome(kind="fiche", candidates=candidates, top=top)
    gap = top.score - candidates[1].score
    if gap >= thresholds.gap:
        return Outcome(kind="fiche", candidates=candidates, top=top)
    close = [c for c in candidates if top.score - c.score < thresholds.gap]
    return Outcome(kind="question", candidates=candidates, close=close, question=build_question(close), top=top)


__all__ = ["Thresholds", "Outcome", "decide", "build_question"]
