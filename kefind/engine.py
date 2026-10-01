"""Le moteur kefind, exposé au harnais scoreboard.

``factory(config) -> KefindEngine`` suit exactement le contrat de
``scoreboard.engines.load_engine`` ("package.module:factory"). Les cinq temps
de la spec sont des appels simples ici : comprendre, chercher, (filtrer —
no-op pour cette tranche), décider, rédiger+vérifier.
"""

from __future__ import annotations

from pathlib import Path

from kecore.decompose import DecomposedFiche
from kecore.llm import AzureOpenAIChat, RecordingLLM, load_llm_config
from scoreboard.engines import Decision, Usage

from .compose import write_and_verify
from .decide import Thresholds
from .decide import decide as decide_outcome
from .graph_filter import filter_candidates
from .io import load_decomposed_jsonl
from .search import EmbeddingProvider, FicheSearchIndex, SearchConfig
from .understand import understand

MAX_TITLES_KEPT = 5
_CONFIG_KEYS = frozenset({
    "name", "fiches", "fiches_path", "thresholds", "alpha", "top_k", "llm_mode",
    "understand_llm_config", "understand_cache_dir", "write_llm_config", "write_cache_dir",
})


def _make_llm(config: dict | None, cache_dir: str | None, mode: str):
    if not config:
        return None
    inner = AzureOpenAIChat.from_config(config)
    return RecordingLLM(inner, cache_dir or "clients-local/kefind/llm-cache", mode=mode)


class KefindEngine:
    """Tranche 3 : comprendre, chercher, (filtrer), décider, rédiger+vérifier."""

    def __init__(self, fiches_by_client: dict[str, list[DecomposedFiche]] | None = None,
                 fiches_path: str | None = None, thresholds: Thresholds | None = None,
                 search_config: SearchConfig | None = None, embedder: EmbeddingProvider | None = None,
                 understand_llm=None, write_llm=None, name: str = ""):
        self.name = name
        self.thresholds = thresholds or Thresholds()
        self.search_config = search_config or SearchConfig()
        self.embedder = embedder
        self.understand_llm = understand_llm
        self.write_llm = write_llm
        self._fiches_path = fiches_path
        self._indexes: dict[str, FicheSearchIndex] = {}
        for client, fiches in (fiches_by_client or {}).items():
            self._indexes[client] = self._build_index(client, fiches)

    def _build_index(self, client: str, fiches: list[DecomposedFiche]) -> FicheSearchIndex:
        return FicheSearchIndex(client, fiches, embedder=self.embedder, config=self.search_config)

    def _index_for(self, client: str) -> FicheSearchIndex | None:
        if client in self._indexes:
            return self._indexes[client]
        if not self._fiches_path:
            return None
        path = Path(self._fiches_path.format(client=client))
        if not path.is_file():
            return None
        fiches = [f for f in load_decomposed_jsonl(path) if f.client == client]
        index = self._build_index(client, fiches)
        self._indexes[client] = index
        return index

    @classmethod
    def from_config(cls, config: dict) -> "KefindEngine":
        unknown = sorted(set(config) - _CONFIG_KEYS - {"comment", "_comment"})
        if unknown:
            raise ValueError(f"réglage(s) kefind inconnu(s) : {', '.join(unknown)}. Attendus : {', '.join(sorted(_CONFIG_KEYS))}")
        fiches_by_client = None
        if config.get("fiches"):
            fiches_by_client = {
                client: [DecomposedFiche.from_dict(d) for d in items] for client, items in config["fiches"].items()
            }
        mode = config.get("llm_mode", "record")
        understand_llm = _make_llm(config.get("understand_llm_config"), config.get("understand_cache_dir"), mode)
        write_llm = _make_llm(config.get("write_llm_config"), config.get("write_cache_dir"), mode)
        default_search = SearchConfig()
        return cls(
            fiches_by_client=fiches_by_client,
            fiches_path=config.get("fiches_path"),
            thresholds=Thresholds.from_dict(config.get("thresholds")),
            search_config=SearchConfig(
                alpha=float(config.get("alpha", default_search.alpha)),
                top_k=int(config.get("top_k", default_search.top_k)),
            ),
            understand_llm=understand_llm,
            write_llm=write_llm,
            name=config.get("name", ""),
        )

    def decide(self, ticket) -> Decision:
        index = self._index_for(ticket.client)
        if index is None:
            return Decision("abstain", error=f"no KB index for client {ticket.client!r}")

        understanding = understand(self.understand_llm, ticket.text)
        candidates = filter_candidates(index.search(understanding))
        usage = Usage(
            input_tokens=understanding.usage.input_tokens,
            output_tokens=understanding.usage.output_tokens,
            search_calls=1,
        )
        trace = [{
            "step": "search",
            "candidates": len(candidates),
            "top_score": candidates[0].score if candidates else None,
        }]
        if not candidates:
            return Decision("abstain", usage=usage, trace=trace)

        fiches = [c.fiche_id for c in candidates]
        titles = {c.fiche_id: c.fiche.title for c in candidates[:MAX_TITLES_KEPT]}
        outcome = decide_outcome(candidates, self.thresholds)

        if outcome.kind == "fiche":
            answer = write_and_verify(self.write_llm, outcome.top.fiche, understanding.symptom)
            usage.input_tokens += answer.usage.input_tokens
            usage.output_tokens += answer.usage.output_tokens
            trace.append({
                "step": "write", "fiche": answer.fiche_id, "step_number": answer.step_number,
                "citation_verified": answer.citation_verified,
            })
            return Decision("fiche", fiches=fiches, score=outcome.top.score, titles=titles, usage=usage, trace=trace)

        if outcome.kind == "question":
            trace.append({"step": "question", "close": [c.fiche_id for c in outcome.close]})
            return Decision("question", fiches=fiches, score=outcome.top.score, question=outcome.question,
                             titles=titles, usage=usage, trace=trace)

        return Decision("abstain", fiches=fiches, score=outcome.top.score if outcome.top else None,
                         titles=titles, usage=usage, trace=trace)

    def candidates(self, ticket, k: int) -> list[tuple[str, str]]:
        index = self._index_for(ticket.client)
        if index is None:
            return []
        understanding = understand(self.understand_llm, ticket.text)
        results = filter_candidates(index.search(understanding, k=k))
        return [(r.fiche_id, r.fiche.title) for r in results[:k]]


def factory(config: dict) -> KefindEngine:
    return KefindEngine.from_config(config)


__all__ = ["KefindEngine", "factory"]
