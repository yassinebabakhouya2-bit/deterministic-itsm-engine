"""Test doubles for the semantic mode: an embedder that knows a few concepts in French and English
(so "ligne" and "line" meet, as with a real model), and a model that writes cards and exam questions
by fiche title. Deterministic, no network."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from kecore.llm import LLMError, LLMResult, LLMUsage

CONCEPTS = {
    "line": ("line", "ligne", "lignes", "numero", "number", "telephonique", "phone", "telephone"),
    "assign": ("assign", "associate", "attribuer", "associer", "affecter", "attribution", "attribuee", "attribue"),
    "forward": ("forward", "forwarding", "transfert", "transferer", "renvoi", "redirect", "rediriger"),
    "call": ("call", "calls", "appel", "appels", "appeler"),
    "teams": ("teams",),
    "password": ("password", "passe", "mdp"),
    "reset": ("reset", "reinitialiser", "reinitialisation", "changer"),
    "account": ("account", "compte"),
    "locked": ("locked", "bloque", "verrouille", "lock", "bloquee"),
    "printer": ("printer", "imprimante", "imprimer", "print"),
    "jam": ("jam", "bourrage", "coince", "papier"),
    "vpn": ("vpn",),
    "drop": ("deconnexion", "deconnecte", "drops", "disconnect", "coupe"),
}
DIMS = 24


def fold(text: str) -> list[str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", plain)


class ConceptEmbedder:
    """``embed(texts, dimensions=None)``: one count per known concept, unknown words hashed into the
    remaining dimensions with a small weight."""

    model_id = "concept-embed@test"

    def __init__(self):
        self.calls: list[list[str]] = []

    def vector(self, text: str) -> list[float]:
        v = [0.0] * DIMS
        names = list(CONCEPTS)
        for token in fold(text):
            hits = [i for i, name in enumerate(names) if token in CONCEPTS[name]]
            for i in hits:
                v[i] += 1.0
            if not hits:
                h = len(names) + int(hashlib.sha256(token.encode()).hexdigest(), 16) % (DIMS - len(names))
                v[h] += 0.25
        if not any(v):
            v[-1] = 1.0
        return v

    def embed(self, texts, dimensions=None):
        texts = list(texts)
        self.calls.append(texts)
        return [self.vector(t) for t in texts]


FRENCH_WORDS = {"le", "la", "les", "une", "des", "du", "de", "comment", "est", "un", "pour", "mon", "ma", "mes",
                "dans", "vers", "doit", "etre", "ouvrez", "configurez", "autre", "pas"}
BIASED_DIMS = DIMS + 2


class LanguageBiasedEmbedder(ConceptEmbedder):
    """The same concepts plus a pull between texts of the same language -- what real embeddings do: a
    French question and a French fiche look closer than the French question and its English fiche."""

    model_id = "biased-embed@test"

    def vector(self, text: str) -> list[float]:
        french = any(token in FRENCH_WORDS for token in fold(text))
        return super().vector(text) + ([2.0, 0.0] if french else [0.0, 2.0])


class BrokenEmbedder:
    model_id = "broken@test"

    def embed(self, texts, dimensions=None):
        raise LLMError("the embedding service is down")


class TitleLLM:
    """Answers by the "Procedure title: ..." line of the request; ``answers[title]`` is a dict (or an
    exception). Records the schema of every call; accepts temperature and seed like the real client."""

    model_id = "title-llm@test"

    def __init__(self, answers: dict, exam: dict | None = None):
        self.answers = answers
        self.exam = exam or {}
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system, user, schema, schema_name, *, temperature=None, seed=None):
        title = user.split("\n", 1)[0][len("Procedure title: "):]
        self.calls.append((schema_name, title))
        source = self.exam if schema_name == "heldout_queries" else self.answers
        data = source.get(title)
        if isinstance(data, Exception):
            raise data
        if data is None:
            data = {"messages": []} if schema_name == "heldout_queries" else {"solves_fr": "", "solves_en": "", "questions": []}
        return LLMResult(data, LLMUsage(100, 50), "title-llm")
