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
from .interpret import interpret
from .io import load_decomposed_jsonl

_CONFIG_KEYS = frozenset({"name", "fiches_path", "profile_path", "funnel"})


class FunnelEngine:
    """Entities first, graph next, text to break ties; the decision is code (``kefind.funnel.find``)."""

    def __init__(self, maps: dict[str, KBMap] | None = None, fiches_path: str | None = None,
                 profile_path: str | None = None, config: FunnelConfig | None = None, name: str = "kefind-funnel",
                 llm=None):
        self.name = name
        self.config = config or FunnelConfig()
        self.llm = llm  # interprets each ticket (kefind.interpret); None: code only
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
        interpretation = interpret(self.llm, ticket.text, kbmap.dictionary) if self.llm is not None else None
        finding = find(kbmap, ticket.text, config=self.config, interpretation=interpretation)
        usage = Usage(search_calls=1)
        if interpretation is not None:
            usage.input_tokens, usage.output_tokens = interpretation.usage.input_tokens, interpretation.usage.output_tokens
        return Decision(finding.kind, fiches=finding.fiches, score=finding.score, question=finding.question,
                        titles={f: kbmap.label(f) for f in finding.fiches}, usage=usage, trace=finding.trace)

    def candidates(self, ticket, k: int) -> list[tuple[str, str]]:
        kbmap = self._map_for(ticket.client)
        if kbmap is None:
            return []
        finding = find(kbmap, ticket.text, config=replace(self.config, top_k=k))
        return [(f, kbmap.label(f)) for f in finding.fiches[:k]]

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
