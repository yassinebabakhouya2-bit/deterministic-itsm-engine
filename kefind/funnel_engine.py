"""The funnel (``kefind.funnel``) behind the scoreboard's engine contract: ``kefind.funnel_engine:factory``.

Kept apart from ``kefind.funnel`` so that the funnel never imports scoreboard: the Azure Function
ships kecore and kefind only. Loading from files here serves the tests and the scoreboard; in Azure
the map is read from the run's blobs (``kecore_func/kecore_pipeline.py``).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kecore.profile import Profile
from scoreboard.engines import Decision, Usage

from .funnel import FunnelConfig, KBMap, find
from .io import load_decomposed_jsonl
from .route import answer, question_vector

_CONFIG_KEYS = frozenset({"name", "fiches_path", "profile_path", "funnel"})


class FunnelEngine:
    """Entities first, graph next, text to break ties; the decision is code (``kefind.funnel.find``). With
    routing tables, the same engine routes first (``kefind.route.answer``: L0 to L4), exactly as live /find
    does once the tables are activated -- the scoreboard measures both side by side."""

    def __init__(self, maps: dict[str, KBMap] | None = None, fiches_path: str | None = None,
                 profile_path: str | None = None, config: FunnelConfig | None = None, name: str = "kefind-funnel",
                 llm=None, embedder=None, routing=None, intent_llm=None, current: dict | None = None):
        self.name = name
        self.config = config or FunnelConfig()
        self.llm = llm  # interprets each ticket (kefind.interpret); None: code only
        self.embedder = embedder  # the semantic mode's question vectors (recorded); None: words only
        self.routing = routing  # kefind.routing.RoutingTables of the map's run; None: no routing (today's engine)
        self.intent_llm = intent_llm  # the L2 mapping (kefind.route.map_question, recorded); None: no L2
        self.current = current  # {fiche_id: source_sha256} of the run: the tables must be the KB's (kb_sha256)
        self._maps = dict(maps or {})
        self._fiches_path = fiches_path
        self._profile_path = profile_path

    def _map_for(self, client: str) -> KBMap | None:
        if client in self._maps:
            return self._maps[client]
        if not self._fiches_path:
            return None
        path = Path(self._fiches_path.format(client=client))
        if not path.is_file():
            return None
        dictionary: dict[str, list[str]] = {}
        if self._profile_path:
            profile_file = Path(self._profile_path.format(client=client))
            if profile_file.is_file():
                dictionary = Profile.load(profile_file).dictionary
        fiches = [f for f in load_decomposed_jsonl(path) if f.client == client]
        self._maps[client] = KBMap(client, fiches, dictionary)
        return self._maps[client]

    def decide(self, ticket) -> Decision:
        kbmap = self._map_for(ticket.client)
        if kbmap is None:
            return Decision("abstain", error=f"no KB map for client {ticket.client!r}")
        result = answer(kbmap, ticket.text, config=self.config, llm=self.llm, embedder=self.embedder,
                        routing=self.routing, current=self.current, intent_llm=self.intent_llm)
        finding = result.finding
        usage = Usage(input_tokens=result.usage["input_tokens"], output_tokens=result.usage["output_tokens"],
                      search_calls=1)
        # a designated fiche has no score for the scoreboard: no calibrated floor ever hides it
        # (kefind.funnel._show), so the threshold sweep must not count on hiding it either
        score = None if finding.designated else finding.score
        return Decision(finding.kind, fiches=finding.fiches, score=score, question=finding.question,
                        titles={f: kbmap.label(f) for f in finding.fiches}, usage=usage, trace=finding.trace,
                        route=result.route, degraded=result.degraded)

    def candidates(self, ticket, k: int) -> list[tuple[str, str]]:
        kbmap = self._map_for(ticket.client)
        if kbmap is None:
            return []
        finding = find(kbmap, ticket.text, config=replace(self.config, top_k=k), query_vector=self._vector(kbmap, ticket.text))
        return [(f, kbmap.label(f)) for f in finding.fiches[:k]]

    def _vector(self, kbmap: KBMap, text: str):
        """The question's vector when the map has an index and the embedder is the index's model; None
        otherwise or on failure (the finding then says it was decided by words: ``degraded``)."""
        return question_vector(kbmap, self.embedder, text)[0]

    @classmethod
    def from_config(cls, config: dict) -> "FunnelEngine":
        unknown = sorted(set(config) - _CONFIG_KEYS - {"comment", "_comment"})
        if unknown:
            raise ValueError(f"réglage(s) inconnu(s) : {', '.join(unknown)}. Attendus : {', '.join(sorted(_CONFIG_KEYS))}")
        return cls(fiches_path=config.get("fiches_path"), profile_path=config.get("profile_path"),
                   config=FunnelConfig.from_dict(config.get("funnel")), name=config.get("name", "kefind-funnel"))


def factory(config: dict) -> FunnelEngine:
    return FunnelEngine.from_config(config)


__all__ = ["FunnelEngine", "factory"]
