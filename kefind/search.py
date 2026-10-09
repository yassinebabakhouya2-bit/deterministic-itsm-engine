"""Temps 2 — Chercher : du code, jamais de LLM.

Recherche dans les fiches décomposées d'un seul client (jamais une autre :
chaque index est construit pour un client et le vérifie), statut "guided" ou
"citable" seulement (jamais "info_only"). Deux classements sont fusionnés :

* lexical (BM25) sur le texte de la fiche et sa "carte d'identité" (ses
  entités canoniques) ;
* "sens", par une interface injectable (``EmbeddingProvider``). Ce sandbox
  n'a pas accès à un vrai service d'embeddings : l'implémentation par défaut
  est un TF-IDF cosinus, déterministe et testable hors-ligne, mais tout
  fournisseur réel qui a la même interface se branche sans toucher à
  ``FicheSearchIndex``.

Les deux classements sont normalisés puis fusionnés (``alpha`` pondère le
lexical contre le sens), puis un bonus est appliqué quand un code d'erreur,
un identifiant d'événement, une application ou un OS du ticket se retrouve
dans la fiche. Le résultat est trié et borné à ``top_k`` candidats.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from kecore.decompose import DecomposedFiche

from .understand import TicketUnderstanding

SEARCHABLE_STATUSES = frozenset({"guided", "citable"})
TOP_K = 20

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "le la les l un une des de du d et ou pour avec sans sur sous dans par au aux ce cette ces cet est "
    "sont a ont ete etre que qui quoi comment pourquoi quand ou en se ne pas non oui plus tres the a an of to in on for "
    "with and or is are".split()
)


def tokenize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return [tok for tok in _TOKEN_RE.findall(folded) if len(tok) > 1 and tok not in _STOPWORDS]


# --- "sens" : TF-IDF cosinus, derrière une interface qu'un vrai fournisseur d'embeddings peut remplacer ---


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Du texte à un vecteur. ``fit`` peut être un no-op pour un vrai service d'embeddings."""

    def fit(self, corpus: Sequence[str]) -> None: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class TfidfEmbeddingProvider:
    """Vecteurs TF-IDF sur un vocabulaire appris, comparés au cosinus.

    Remplaçant local et déterministe du "sens" : un vrai fournisseur
    d'embeddings (API, modèle local) implémente la même interface — ``fit``
    peut n'y rien faire — et se substitue à celui-ci sans changer
    ``FicheSearchIndex``.
    """

    vocabulary: dict[str, int] = field(default_factory=dict)
    idf: list[float] = field(default_factory=list)

    def fit(self, corpus: Sequence[str]) -> None:
        document_frequency: Counter = Counter()
        n = 0
        for text in corpus:
            n += 1
            document_frequency.update(set(tokenize(text)))
        terms = sorted(document_frequency)
        self.vocabulary = {term: i for i, term in enumerate(terms)}
        self.idf = [math.log((n + 1) / (document_frequency[term] + 1)) + 1.0 for term in terms]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            counts = Counter(tokenize(text))
            vector = [0.0] * len(self.vocabulary)
            total = sum(counts.values()) or 1
            for term, count in counts.items():
                index = self.vocabulary.get(term)
                if index is not None:
                    vector[index] = (count / total) * self.idf[index]
            vectors.append(vector)
        return vectors


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- lexical : BM25 ---

BM25_K1 = 1.5
BM25_B = 0.75


@dataclass
class _Bm25Doc:
    terms: Counter
    length: int


class Bm25Index:
    def __init__(self, documents: Sequence[str]):
        self.docs = [_Bm25Doc(Counter(tokenize(text)), len(tokenize(text))) for text in documents]
        self.n = len(self.docs)
        self.avg_len = (sum(d.length for d in self.docs) / self.n) if self.n else 0.0
        self.document_frequency: Counter = Counter()
        for doc in self.docs:
            self.document_frequency.update(doc.terms.keys())

    def _idf(self, term: str) -> float:
        df = self.document_frequency.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query_terms: Sequence[str]) -> list[float]:
        scores = [0.0] * self.n
        for term in set(query_terms):
            idf = self._idf(term)
            for i, doc in enumerate(self.docs):
                freq = doc.terms.get(term, 0)
                if not freq:
                    continue
                denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * doc.length / (self.avg_len or 1))
                scores[i] += idf * (freq * (BM25_K1 + 1)) / denom
        return scores


# --- fusion, bonus d'entités, résultats ---

BONUS_BY_PREFIX = {"err": 0.25, "evt": 0.10, "app": 0.12, "os": 0.05}


@dataclass
class SearchResult:
    fiche_id: str
    fiche: DecomposedFiche
    score: float
    lexical_score: float
    sense_score: float
    bonus: float
    matched_entities: list[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    alpha: float = 0.5  # poids du lexical contre le sens dans la fusion (0..1)
    top_k: int = TOP_K


LEXICAL_SATURATION = 4.0  # échelle d'un bon score BM25 sur ce genre de corpus


def _saturate(value: float, scale: float) -> float:
    """value/(value+scale) : ramène un score non borné vers [0, 1) sans écraser sa magnitude absolue.

    Contrairement à un min-max sur les seuls candidats de la requête, un score faible reste faible
    même quand c'est le meilleur du lot — nécessaire pour que le plancher d'abstention (temps 4)
    veuille dire quelque chose.
    """
    if value <= 0:
        return 0.0
    return value / (value + scale)


def _fiche_text(fiche: DecomposedFiche) -> str:
    identity = " ".join(entry["canonical"] for entry in fiche.entities)
    return f"{fiche.title}\n{fiche.text}\n{identity}"


def _entity_bonus(ticket_canonicals: set[str], fiche_canonicals: set[str]) -> tuple[float, list[str]]:
    matched = sorted(ticket_canonicals & fiche_canonicals)
    bonus = sum(BONUS_BY_PREFIX.get(c.split(":", 1)[0], 0.0) for c in matched)
    return bonus, matched


class FicheSearchIndex:
    """Index de recherche d'un seul client ; jamais une fiche d'un autre client ici."""

    def __init__(self, client: str, fiches: Sequence[DecomposedFiche], embedder: EmbeddingProvider | None = None,
                 config: SearchConfig | None = None):
        for fiche in fiches:
            if fiche.client != client:
                raise ValueError(f"fiche {fiche.fiche_id} appartient au client {fiche.client!r}, pas à {client!r}")
        self.client = client
        self.config = config or SearchConfig()
        self.fiches = [f for f in fiches if f.status in SEARCHABLE_STATUSES]
        self._texts = [_fiche_text(f) for f in self.fiches]
        self._bm25 = Bm25Index(self._texts)
        self.embedder = embedder or TfidfEmbeddingProvider()
        self.embedder.fit(self._texts)
        self._vectors = self.embedder.embed(self._texts) if self.fiches else []
        self._entity_sets = [{e["canonical"] for e in f.entities} for f in self.fiches]

    def search(self, understanding: TicketUnderstanding, k: int | None = None) -> list[SearchResult]:
        k = self.config.top_k if k is None else k
        if not self.fiches:
            return []
        lexical = self._bm25.score(tokenize(understanding.text))
        query_vectors = self.embedder.embed([understanding.text])
        query_vector = query_vectors[0] if query_vectors else []
        sense = [cosine(query_vector, vector) for vector in self._vectors]
        lexical_norm = [_saturate(v, LEXICAL_SATURATION) for v in lexical]
        sense_norm = list(sense)  # déjà dans [0, 1] (cosinus de vecteurs positifs)
        ticket_entities = set(understanding.canonical_entities)
        alpha = self.config.alpha
        results = []
        for i, fiche in enumerate(self.fiches):
            bonus, matched = _entity_bonus(ticket_entities, self._entity_sets[i])
            merged = alpha * lexical_norm[i] + (1 - alpha) * sense_norm[i] + bonus
            results.append(SearchResult(fiche.fiche_id, fiche, merged, lexical[i], sense[i], bonus, matched))
        results.sort(key=lambda r: (r.score, r.lexical_score), reverse=True)
        return results[:k]


__all__ = [
    "SEARCHABLE_STATUSES", "TOP_K", "tokenize", "EmbeddingProvider", "TfidfEmbeddingProvider", "cosine",
    "Bm25Index", "SearchResult", "SearchConfig", "FicheSearchIndex", "BONUS_BY_PREFIX",
]
