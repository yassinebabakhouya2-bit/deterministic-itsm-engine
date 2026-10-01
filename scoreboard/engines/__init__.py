"""The contract every engine follows, and how engines are loaded by name.

An engine is any object with a ``name`` and ``decide(ticket) -> Decision``.
It may also offer ``candidates(ticket, k) -> [(fiche_id, title), ...]``, used to
pre-fill the labeling sheet.

A decision is one of three kinds:

* ``fiche``: the engine shows ``fiches[0]`` to the technician as THE fiche;
* ``question``: it asks a discriminating question instead of showing a fiche;
* ``abstain``: it says it does not know.

``fiches`` may hold ranked candidates for every kind (used for recall@5).
"""

from __future__ import annotations

import importlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

KINDS = ("fiche", "question", "abstain")

BUILTIN_ENGINES = {
    "search-baseline": "scoreboard.engines.search_baseline:SearchBaselineEngine",
    "fixture": "scoreboard.engines.fixture:FixtureEngine",
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    search_calls: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Usage":
        data = data or {}
        return cls(**{key: int(data.get(key) or 0) for key in ("input_tokens", "output_tokens", "search_calls")})


@dataclass
class Decision:
    kind: str
    fiches: list[str] = field(default_factory=list)
    score: float | None = None
    question: str | None = None
    usage: Usage = field(default_factory=Usage)
    latency_s: float | None = None
    titles: dict[str, str] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown decision kind {self.kind!r}; expected one of {', '.join(KINDS)}")
        self.fiches = [str(f) for f in self.fiches if f is not None and str(f).strip()]
        if self.kind == "fiche" and not self.fiches:
            raise ValueError("a 'fiche' decision must name at least one fiche")

    @property
    def shown(self) -> str | None:
        return self.fiches[0] if self.kind == "fiche" else None


def _float_or_none(value):
    if value is None or value == "":
        return None
    return float(value)


def decision_from_dict(data) -> Decision:
    if isinstance(data, Decision):
        return data
    if not isinstance(data, dict):
        raise TypeError(f"an engine must return a Decision or a dict, got {type(data).__name__}")
    return Decision(
        kind=data.get("kind", ""),
        fiches=list(data.get("fiches") or []),
        score=_float_or_none(data.get("score")),
        question=data.get("question"),
        usage=Usage.from_dict(data.get("usage")),
        latency_s=_float_or_none(data.get("latency_s")),
        titles=dict(data.get("titles") or {}),
        trace=list(data.get("trace") or []),
        error=data.get("error"),
    )


def load_config(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"engine config not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"engine config {path} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
    if not isinstance(config, dict):
        raise ValueError(f"engine config {path} must be a JSON object")
    return config


def load_engine(spec: str, config_path: str | Path | None = None, config: dict | None = None):
    """Load a built-in engine by name, or any 'package.module:factory'."""
    if config is None:
        config = load_config(config_path)
    target = BUILTIN_ENGINES.get(spec, spec)
    if ":" not in target:
        names = ", ".join(sorted(BUILTIN_ENGINES))
        raise ValueError(f"unknown engine {spec!r}: use one of {names} or 'package.module:factory'")
    module_name, attr = target.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    engine = factory.from_config(config) if hasattr(factory, "from_config") else factory(config)
    if not callable(getattr(engine, "decide", None)):
        raise TypeError(f"engine {spec!r} has no decide(ticket) method")
    if not getattr(engine, "name", None):
        engine.name = spec
    return engine
