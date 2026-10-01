"""Helpers partagés par les tests de kefind — même esprit que kecore.tests.helpers."""

from __future__ import annotations

from pathlib import Path

from kecore.decompose import DecomposedFiche, Decomposer
from kecore.fiches import Fiche
from kecore.llm import LLMResult, LLMUsage

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class StubLLM:
    """Répond toujours la même chose (ou lève toujours), quel que soit le schéma demandé.

    ``answers`` peut être un seul objet de données (une réponse pour tout appel) ou un dict
    ``{schema_name: data}`` pour des réponses différentes au temps 1 et au temps 5.
    """

    model_id = "stub-model@test"

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[str] = []

    def complete_json(self, system, user, schema, schema_name):
        self.calls.append(schema_name)
        data = self.answers.get(schema_name) if isinstance(self.answers, dict) and schema_name in self.answers else self.answers
        if isinstance(data, Exception):
            raise data
        return LLMResult(data, LLMUsage(10, 5), self.model_id)


def decompose_fiche(fiche_id: str, client: str, title: str, text: str) -> DecomposedFiche:
    """Une fiche décomposée par les règles seules (sans LLM) — pour des tests rapides et déterministes."""
    return Decomposer(profile=None, llm=None).decompose(Fiche(fiche_id=fiche_id, client=client, title=title, text=text))
