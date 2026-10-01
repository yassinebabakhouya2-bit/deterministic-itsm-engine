"""Replays decisions written in a JSON file.

Used by the tests and the demo, and to score decisions produced elsewhere.
Config:

    {
      "name": "demo",
      "decisions": {
        "clienta/T-1": {"kind": "fiche", "fiches": ["KB0010001"], "score": 2.4},
        "clienta/T-2": [{"kind": "fiche", "fiches": ["KB1"]}, {"kind": "abstain"}]
      },
      "candidates": {"clienta/T-1": [{"id": "KB0010001", "title": "VPN"}]},
      "default": {"kind": "abstain"}
    }

A list of decisions is replayed one per run, in order, to simulate an unstable
engine. Keys are "client/ticket_id" (or the bare ticket id).
"""

from __future__ import annotations

from . import Decision, decision_from_dict


class FixtureEngine:
    def __init__(self, decisions: dict, candidates: dict | None = None, name: str = "fixture", default: dict | None = None):
        self.name = name
        self._decisions = decisions or {}
        self._candidates = candidates or {}
        self._default = default or {"kind": "abstain"}
        self._calls: dict[str, int] = {}

    @classmethod
    def from_config(cls, config: dict) -> "FixtureEngine":
        return cls(config.get("decisions", {}), config.get("candidates"), config.get("name", "fixture"), config.get("default"))

    def _lookup(self, table: dict, ticket):
        if ticket.key in table:
            return table[ticket.key]
        return table.get(ticket.ticket_id)

    def decide(self, ticket) -> Decision:
        entry = self._lookup(self._decisions, ticket)
        if entry is None:
            entry = self._default
        if isinstance(entry, list):
            count = self._calls.get(ticket.key, 0)
            self._calls[ticket.key] = count + 1
            entry = entry[count % len(entry)]
        return decision_from_dict(entry)

    def candidates(self, ticket, k: int) -> list[tuple[str, str]]:
        items = self._lookup(self._candidates, ticket) or []
        pairs = []
        for item in items:
            if isinstance(item, dict):
                pairs.append((str(item["id"]), str(item.get("title", ""))))
            else:
                pairs.append((str(item), ""))
        return pairs[:k]
