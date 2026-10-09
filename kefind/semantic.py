"""The frozen semantic index of a client's KB: fiches found by meaning, decided by code.

Built once per kecore run, offline (kecore_func/semantic_service.py), from three kinds of entries per
fiche: its label (document name or title), what it solves (one French and one English sentence) and
the questions people ask when they need it (kefind.cards: written once by the model from the fiche
itself, checked by code). Every entry is embedded once (text-embedding-3-large, through
kecore.llm.RecordingEmbeddings: a text keeps its vector forever) and the vectors are frozen in the
run's folder with their sha256. Nothing in a run's index changes after it is written.

At question time the ticket is embedded once (recorded too: the same text always gets the same
vector), and everything else is exact arithmetic in pure Python: float64 products summed with
``math.fsum`` (correctly rounded, so the same on every machine and every Python version -- plain
``sum`` changed its algorithm in 3.12), no BLAS: a fiche's score is the best cosine similarity
between the question and one of its entries. The decision (``decide``) is three comparisons against
thresholds calibrated on the KB itself (kefind.calibrate), never on a model's opinion:

* show the first fiche when its score reaches ``floor`` AND it strictly leads the next one by at
  least ``margin`` (an exact tie is never broken by a fiche's name);
* otherwise offer the closest fiches when the first reaches ``offer``;
* otherwise abstain: nothing in the KB is close to the question.

Uncalibrated or withheld thresholds (``floor`` above any cosine) never show a fiche on a score --
not even helped by an error code: only a fiche the ticket names itself, alone, is shown.

Same question text + same run = the same vector, the same scores, the same decision, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import unicodedata
from array import array
from dataclasses import asdict, dataclass, field
from operator import mul
from typing import Iterable, Sequence

INDEX_VERSION = 1
ENTRY_KINDS = ("label", "solves", "question")
CHECKED_KINDS = ("solves", "question")  # written by the model: kept only if closer to their own fiche
MAX_ANCHOR_CHARS = 1_500  # a fiche's own text used to check what the model wrote (never indexed)
MAX_ENTRY_CHARS = 400
MAX_QUERY_CHARS = 2_000
ROUND = 6  # every score is rounded before any comparison: the decision never rests on float noise
INDEX_BLOB = "semantic/index.json"
VECTORS_BLOB = "semantic/vectors.f32"
CALIBRATION_BLOB = "semantic/calibration.json"


def normalize_text(text: str, limit: int = MAX_QUERY_CHARS) -> str:
    """The exact text that is embedded, on both sides (fiches and questions): Unicode-compatible
    forms folded (NFKC), case folded, whitespace collapsed. "Ligne  TEAMS" and "ligne teams" are the
    same question, so they get the same vector and the same answer."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(folded.split())[:limit]


def query_text(text: str) -> str:
    """What is embedded for a question: the ticket cleaned like any kecore text (HTML, entities,
    signatures' layout), e-mail addresses and phone numbers replaced (they never help find a fiche,
    and the question's vector is recorded: an embedding can be partly inverted), then normalized like
    the entries. The live question, the scoreboard and the calibration all embed exactly this."""
    from kecore.text import EMAIL_RE, PHONE_RE, clean_text  # the base library; imported here to keep this light

    cleaned = PHONE_RE.sub(" [téléphone] ", EMAIL_RE.sub(" [e-mail] ", clean_text(text or "")))
    return normalize_text(cleaned, MAX_QUERY_CHARS)


def unit(vector: Sequence[float]) -> list[float]:
    """The vector scaled to length 1 (float64, correctly rounded sum). A zero vector stays zero."""
    values = [float(x) for x in vector]
    norm = math.sqrt(math.fsum(v * v for v in values))
    return [v / norm for v in values] if norm > 0 else values


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """Correctly rounded (math.fsum): independent of summation order and of the Python version."""
    return math.fsum(map(mul, a, b))


@dataclass(frozen=True)
class Entry:
    fiche_id: str
    kind: str  # "label" | "solves" | "question"
    text: str  # the normalized text that was embedded


@dataclass(frozen=True)
class Thresholds:
    floor: float  # the first fiche is shown only at or above this similarity...
    margin: float  # ...and only with this lead over the next fiche
    offer: float  # under it nothing is close enough to be offered: abstain
    source: str = "uncalibrated"

    def to_dict(self) -> dict:
        return asdict(self)


# Without a calibration the engine never shows a fiche on its own (floor above any cosine): it can
# only offer the closest ones. Safe, honest, and what the run reports until calibration succeeds.
UNCALIBRATED = Thresholds(floor=1.01, margin=0.05, offer=0.35, source="uncalibrated")


def decide(scored: Sequence[tuple[float, str, int]], thresholds: Thresholds, strong: bool = False,
           designated: bool = False) -> str:
    """"show", "offer" or "abstain" -- the whole decision, comparisons only.

    ``scored``: (score, fiche_id, entry) sorted best first. ``designated``: the ticket named the fiche
    (cited number, a technician's answer): a single candidate is shown whatever its score. ``strong``:
    an error code, an event id or an update kept the candidates; with calibrated thresholds the offer
    level is then enough to show a clear leader, since the entity already did most of the designating.
    With uncalibrated or withheld thresholds (``floor`` > 1) nothing is shown on a score, strong or
    not. A lead is strictly positive: two fiches at the same score are offered, never shown."""
    if not scored:
        return "abstain"
    if designated and len(scored) == 1:
        return "show"
    first = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else None
    gap = None if second is None else round(first - second, ROUND)
    lead = gap is None or (gap > 0 and gap >= thresholds.margin)
    never = thresholds.floor > 1.0
    floor = thresholds.floor if never or not (strong or designated) else thresholds.offer
    if first >= floor and lead:
        return "show"
    if first >= thresholds.offer:
        return "offer"
    return "abstain"


@dataclass
class SemanticIndex:
    model: str  # "<deployment>@<host>": a vector only compares with vectors of the same model
    dimensions: int
    entries: list[Entry]
    vectors: array  # float32, row i = entry i, each row of length 1
    calibration: dict | None = None
    sha256: str = ""
    stats: dict = field(default_factory=dict)

    def __post_init__(self):
        if len(self.vectors) != len(self.entries) * self.dimensions:
            raise ValueError("the vectors do not match the entries and dimensions of this index")
        d = self.dimensions
        self._rows = [self.vectors[i * d:(i + 1) * d] for i in range(len(self.entries))]
        self.fiche_ids = frozenset(e.fiche_id for e in self.entries)
        if not self.sha256:
            self.sha256 = self.fingerprint()

    # ----------------------------------------------------------------- identity
    def _meta(self) -> dict:
        return {"version": INDEX_VERSION, "model": self.model, "dimensions": self.dimensions,
                "entries": [asdict(e) for e in self.entries]}

    def fingerprint(self) -> str:
        meta = json.dumps(self._meta(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(meta + b"\0" + _le_bytes(self.vectors)).hexdigest()

    # ------------------------------------------------------------------ storage
    def to_blobs(self) -> dict[str, bytes]:
        meta = {**self._meta(), "sha256": self.sha256, "stats": self.stats}
        return {
            INDEX_BLOB: (json.dumps(meta, ensure_ascii=False, indent=1) + "\n").encode("utf-8"),
            VECTORS_BLOB: _le_bytes(self.vectors),
        }

    @classmethod
    def from_blobs(cls, index_json: bytes, vectors: bytes, calibration_json: bytes | None = None) -> "SemanticIndex":
        meta = json.loads(index_json.decode("utf-8"))
        if meta.get("version") != INDEX_VERSION:
            raise ValueError(f"semantic index version {meta.get('version')!r} is not {INDEX_VERSION}")
        values = array("f")
        values.frombytes(vectors)
        if sys.byteorder == "big":
            values.byteswap()
        index = cls(model=meta["model"], dimensions=int(meta["dimensions"]),
                    entries=[Entry(**e) for e in meta["entries"]], vectors=values, stats=meta.get("stats") or {})
        if index.sha256 != meta.get("sha256"):
            raise ValueError("the semantic index does not match its sha256: refusing a damaged or mixed index")
        if calibration_json is not None:
            calibration = json.loads(calibration_json.decode("utf-8"))
            if calibration.get("index_sha256") != index.sha256:
                raise ValueError("this calibration was made for another index: refusing it")
            index.calibration = calibration
        return index

    # ------------------------------------------------------------------ ranking
    def thresholds(self) -> Thresholds:
        th = (self.calibration or {}).get("thresholds")
        if not th:
            return UNCALIBRATED
        return Thresholds(floor=float(th["floor"]), margin=float(th["margin"]), offer=float(th["offer"]),
                          source=str(th.get("source") or "calibrated"))

    def rank(self, query_vector: Sequence[float], pool: Iterable[str] | None = None) -> list[tuple[float, str, int]]:
        """(score, fiche_id, best entry) for each fiche of ``pool`` (all fiches when None), best first;
        ties broken by fiche id. A fiche's score is its best entry's cosine with the question."""
        if len(query_vector) != self.dimensions:
            raise ValueError(f"the question's vector has {len(query_vector)} dimensions, the index {self.dimensions}")
        allowed = None if pool is None else frozenset(pool)
        q = unit(query_vector)
        best: dict[str, tuple[float, int]] = {}
        for i, entry in enumerate(self.entries):
            if allowed is not None and entry.fiche_id not in allowed:
                continue
            score = dot(q, self._rows[i])
            current = best.get(entry.fiche_id)
            if current is None or score > current[0]:
                best[entry.fiche_id] = (score, i)
        scored = [(round(score, ROUND), fiche_id, i) for fiche_id, (score, i) in best.items()]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored


def _le_bytes(values: array) -> bytes:
    if sys.byteorder == "big":
        copy = array(values.typecode, values)
        copy.byteswap()
        return copy.tobytes()
    return values.tobytes()


def pack(vectors: Sequence[Sequence[float]]) -> array:
    """Unit rows as one float32 array (the frozen form of the index)."""
    flat = array("f")
    for vector in vectors:
        flat.extend(unit(vector))
    return flat


def build(entries: list[Entry], embedder, model: str, dimensions: int,
          anchors: list[Entry] | None = None) -> tuple[SemanticIndex, dict]:
    """Embeds every entry (``embedder.embed``: recorded, so a rebuild is free and identical), then drops
    each line the model wrote (what a fiche solves, its questions) that is closer to another fiche than
    to its own: a question written for fiche A that reads like fiche B would pull B's tickets to A.
    The yardstick is code-derived text only -- each fiche's label and ``anchors`` (its own description
    and verified steps, embedded but never indexed) -- so a card the model got wrong cannot vouch for
    itself. A line is kept when its closest anchor belongs to its own fiche."""
    if not entries:
        raise ValueError("no entry to index")
    anchors = list(anchors or [])
    vectors = [unit(v) for v in embedder.embed([e.text for e in entries] + [a.text for a in anchors])]
    if any(len(v) != dimensions for v in vectors):
        raise ValueError("the embedder answered vectors of another size")
    yardstick = [(e.fiche_id, vectors[i]) for i, e in enumerate(entries) if e.kind not in CHECKED_KINDS]
    yardstick += [(a.fiche_id, vectors[len(entries) + j]) for j, a in enumerate(anchors)]
    kept: list[int] = []
    dropped: list[dict] = []
    for i, entry in enumerate(entries):
        if entry.kind not in CHECKED_KINDS:
            kept.append(i)
            continue
        best_own, best_other, other_fiche = -2.0, -2.0, None
        for fiche_id, anchor in yardstick:
            score = dot(vectors[i], anchor)
            if fiche_id == entry.fiche_id:
                best_own = max(best_own, score)
            elif score > best_other:
                best_other, other_fiche = score, fiche_id
        if round(best_other, ROUND) > round(best_own, ROUND):
            dropped.append({"fiche_id": entry.fiche_id, "kind": entry.kind, "text": entry.text,
                            "closer_to": other_fiche, "own": round(best_own, ROUND), "other": round(best_other, ROUND)})
        else:
            kept.append(i)
    final = [entries[i] for i in kept]
    index = SemanticIndex(model=model, dimensions=dimensions, entries=final, vectors=pack([vectors[i] for i in kept]))
    by_kind = {kind: sum(1 for e in final if e.kind == kind) for kind in ENTRY_KINDS}
    stats = {"fiches": len(index.fiche_ids), "entries": len(final), "by_kind": by_kind, "anchors": len(anchors),
             "dropped_closer_to_another_fiche": {kind: sum(1 for d in dropped if d["kind"] == kind)
                                                 for kind in CHECKED_KINDS}}
    index.stats = stats
    return index, {"stats": stats, "dropped": dropped}


__all__ = ["Entry", "SemanticIndex", "Thresholds", "UNCALIBRATED", "decide", "build", "pack", "unit", "dot",
           "normalize_text", "query_text", "INDEX_BLOB", "VECTORS_BLOB", "CALIBRATION_BLOB", "MAX_ENTRY_CHARS",
           "MAX_QUERY_CHARS", "MAX_ANCHOR_CHARS", "CHECKED_KINDS"]
