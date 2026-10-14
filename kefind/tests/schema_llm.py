"""A model fake for the routing passes: answers by (schema name, key), where the key is what follows the
first line's "<prefix>: " (pass A and C start with "Procedure title: <label>", the L2 mapping with
"Question: <text>", pass B with "Proposed intents: <n>"). Safe under kecore.llm.RecordingLLM (accepts
temperature and seed like the real client)."""

from __future__ import annotations

from kecore.llm import LLMResult, LLMUsage


class SchemaLLM:
    model_id = "schema-llm@test"

    def __init__(self, answers: dict | None = None, default=None):
        # answers: {(schema_name, key): data | Exception | callable(system, user, schema) -> data}
        #          or {schema_name: data | Exception | callable} for every key of that schema
        self.answers = dict(answers or {})
        self.default = default
        self.calls: list[tuple[str, str]] = []
        self.schemas: list[dict] = []

    @staticmethod
    def key_of(user: str) -> str:
        first = user.split("\n", 1)[0]
        return first.split(": ", 1)[1] if ": " in first else first

    def complete_json(self, system, user, schema, schema_name, *, temperature=None, seed=None):
        key = self.key_of(user)
        self.calls.append((schema_name, key))
        self.schemas.append(schema)
        if (schema_name, key) in self.answers:
            data = self.answers[(schema_name, key)]
        elif schema_name in self.answers:
            data = self.answers[schema_name]
        else:
            data = self.default
        if callable(data) and not isinstance(data, type):
            data = data(system, user, schema)
        if isinstance(data, Exception):
            raise data
        if data is None:
            raise AssertionError(f"SchemaLLM: no answer for ({schema_name!r}, {key!r})")
        return LLMResult(data, LLMUsage(100, 50), "schema-llm")


__all__ = ["SchemaLLM"]
