"""Fiche -> verified steps: the double decomposition and its checks.

For each fiche:

1. boilerplate lines of the client's profile are removed, e-mails and phone
   numbers masked;
2. the rules (segment.py) and the LLM (llm_segment.py) split it into steps,
   independently;
3. every LLM quote must be found in the fiche (text.NormalizedText) or it is
   dropped; a rewording is kept only if it adds no command, path, menu, key or
   code (entities.novel_technical_entities);
4. the two splits are aligned: agreement gives high confidence, disagreement
   low confidence;
5. status: 'guided' (high confidence and at least one resolution step),
   'citable' (shown as a source, never guided step by step), or 'info_only'
   (no resolution step).

A step's text is always the fiche's own span. A "si échec" link exists only
when the fiche writes it.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import asdict, dataclass, field

from .entities import extract_entities, novel_technical_entities
from .llm import LLMError, LLMUsage
from .llm_segment import llm_segment
from .profile import Profile, remove_boilerplate
from .segment import FAILURE_RE, RESOLUTION_ROLES, RuleStep, Section, keyword_role, rule_steps, split_sections, step_kind
from .text import NormalizedText, Scrubber

DECOMPOSITION_VERSION = 1
AGREEMENT_THRESHOLD = 0.8
MATCH_IOU = 0.5


@dataclass
class Step:
    n: int
    start: int
    end: int
    text: str
    kind: str
    role: str
    level: int = 0
    condition: str | None = None
    after_failure: bool = False
    on_failure: int | None = None
    goto: int | None = None
    instruction: str | None = None
    entities: list[str] = field(default_factory=list)
    refers_to: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DecomposedFiche:
    fiche_id: str
    client: str
    title: str
    source: str
    text: str
    text_sha256: str
    status: str
    confidence: str
    reasons: list[str]
    sections: list[dict]
    steps: list[Step]
    entities: list[dict]
    references: list[str]
    methods: dict
    checks: dict
    boilerplate_removed: int = 0
    masked: dict = field(default_factory=dict)
    version: int = DECOMPOSITION_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DecomposedFiche":
        values = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        values["steps"] = [Step(**step) for step in data.get("steps", [])]
        return cls(**values)


@dataclass
class _Candidate:
    start: int
    end: int
    kind: str
    role: str
    sources: list[str]
    level: int = 0
    number: int | None = None
    condition: tuple[int, int] | None = None
    after_failure: bool = False
    goto: int | None = None
    instruction: str | None = None
    failure_span: tuple[int, int] | None = None


def iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def align(first: list[tuple[int, int]], second: list[tuple[int, int]], threshold: float = MATCH_IOU) -> dict[int, int]:
    """One-to-one matching of spans by overlap: index in first -> index in second."""
    pairs = sorted(
        ((iou(a, b), i, j) for i, a in enumerate(first) for j, b in enumerate(second)),
        reverse=True,
    )
    matched: dict[int, int] = {}
    used: set[int] = set()
    for score, i, j in pairs:
        if score < threshold:
            break
        if i in matched or j in used:
            continue
        matched[i] = j
        used.add(j)
    return matched


class Decomposer:
    def __init__(self, profile: Profile | None = None, llm=None, agreement_threshold: float = AGREEMENT_THRESHOLD,
                 scrub: bool = True):
        self.profile = profile
        self.llm = llm
        self.agreement_threshold = agreement_threshold
        self.scrub = scrub
        self.usage = LLMUsage()
        self.llm_calls = 0
        self.llm_cached = 0
        self.llm_errors = 0

    def decompose(self, fiche) -> DecomposedFiche:
        text, removed = remove_boilerplate(fiche.text, self.profile)
        masked: dict = {}
        if self.scrub:
            scrubber = Scrubber()
            text = scrubber(text)
            masked = dict(scrubber.counts)
        normalized = NormalizedText(text)
        lookup = self.profile.role_lookup() if self.profile else keyword_role
        sections = split_sections(text, lookup)
        rules = rule_steps(text, sections)
        reasons: list[str] = []
        methods: dict = {"rules_steps": len(rules), "llm": None}

        llm_candidates: list[_Candidate] | None = None
        rejected = 0
        instructions_dropped = 0
        if self.llm is not None:
            try:
                segmentation = llm_segment(self.llm, fiche.title, text)
            except LLMError as exc:
                self.llm_errors += 1
                reasons.append(f"LLM pass failed: {exc}")
                segmentation = None
            if segmentation is not None and segmentation.skipped:
                reasons.append(f"LLM pass skipped: {segmentation.skipped}")
                segmentation = None
            if segmentation is not None:
                self._count(segmentation)
                llm_candidates, rejected, instructions_dropped = self._locate(segmentation, normalized, text)
                methods["llm"] = segmentation.model or "llm"
                methods["llm_steps"] = len(llm_candidates) + rejected
                methods["llm_rejected_quotes"] = rejected
                methods["llm_malformed"] = segmentation.malformed
        rule_candidates = [self._from_rule(step) for step in rules]

        if llm_candidates is None:
            final = rule_candidates
            agreement = None
            reasons.append("single method: the LLM pass did not run")
        else:
            matches = align([(c.start, c.end) for c in llm_candidates], [(c.start, c.end) for c in rule_candidates])
            for i, j in matches.items():
                self._merge(llm_candidates[i], rule_candidates[j])
            # The LLM's verified steps lead; when it found none, the rules' steps stay (the fiche stays citable).
            final = llm_candidates if llm_candidates else rule_candidates
            total = len(rule_candidates) + len(llm_candidates) + rejected
            agreement = 2 * len(matches) / total if total else 1.0
            methods["matched"] = len(matches)
        methods["agreement"] = None if agreement is None else round(agreement, 3)

        final.sort(key=lambda c: c.start)
        steps = self._build_steps(final, text)
        resolution = [s for s in steps if s.role in RESOLUTION_ROLES]

        if agreement is None:
            confidence = "low"
        elif agreement >= self.agreement_threshold:
            confidence = "high"
        else:
            confidence = "low"
            reasons.append(f"the rules and the LLM disagree (agreement {agreement:.2f})")
        if rejected:
            reasons.append(f"{rejected} LLM quote(s) not found in the fiche, dropped")
        if instructions_dropped:
            reasons.append(f"{instructions_dropped} rewording(s) adding technical content, dropped")

        if not resolution:
            status = "info_only"
            reasons.append("no resolution step")
        elif confidence == "high":
            status = "guided"
        else:
            status = "citable"

        entity_counts: Counter = Counter()
        entity_kinds: dict[str, str] = {}
        for entity in extract_entities(f"{fiche.title}\n{text}"):
            entity_counts[entity.canonical] += 1
            entity_kinds[entity.canonical] = entity.kind
        references = sorted(
            {c[len("kb:"):] for c in entity_counts if c.startswith("kb:") and c[len("kb:"):] != fiche.fiche_id}
        )
        checks = {
            "steps_verified": sum(1 for s in steps if text[s.start:s.end] == s.text),
            "steps_total": len(steps),
            "resolution_steps": len(resolution),
            "failure_links": sum(1 for s in steps if s.on_failure is not None),
            "llm_quotes_rejected": rejected,
            "instructions_dropped": instructions_dropped,
        }
        return DecomposedFiche(
            fiche_id=fiche.fiche_id,
            client=fiche.client,
            title=fiche.title,
            source=fiche.source,
            text=text,
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            status=status,
            confidence=confidence,
            reasons=reasons,
            sections=[s.to_dict() for s in sections],
            steps=steps,
            entities=[
                {"canonical": c, "kind": entity_kinds[c], "count": n} for c, n in sorted(entity_counts.items())
            ],
            references=references,
            methods=methods,
            checks=checks,
            boilerplate_removed=removed,
            masked=masked,
        )

    # --- helpers ---------------------------------------------------------------------------

    def _count(self, segmentation) -> None:
        if segmentation.cached:
            self.llm_cached += 1
        else:
            self.llm_calls += 1
            self.usage.add(segmentation.usage)

    @staticmethod
    def _from_rule(step: RuleStep) -> _Candidate:
        return _Candidate(
            start=step.start,
            end=step.end,
            kind=step.kind,
            role=step.role,
            sources=["rules"],
            level=step.level,
            number=step.number,
            condition=step.condition,
            after_failure=step.after_failure,
            goto=step.goto,
        )

    @staticmethod
    def _locate(segmentation, normalized: NormalizedText, text: str):
        candidates: list[_Candidate] = []
        rejected = 0
        dropped = 0
        cursor = 0
        for item in segmentation.steps:
            span = normalized.find(item.quote, after=cursor)
            if span is None or any(iou(span, (c.start, c.end)) > 0.0 for c in candidates):
                rejected += 1
                continue
            cursor = span[1]
            candidate = _Candidate(start=span[0], end=span[1], kind=item.kind, role=item.section_role, sources=["llm"])
            if item.condition:
                condition = normalized.find(item.condition, after=max(0, span[0] - 300))
                if condition is not None and span[0] - 300 <= condition[0] and condition[1] <= span[1]:
                    candidate.condition = condition
            if item.on_failure:
                candidate.failure_span = normalized.find(item.on_failure, after=span[1])
            if item.instruction:
                if novel_technical_entities(item.instruction, text[span[0]:span[1]]):
                    dropped += 1
                else:
                    candidate.instruction = item.instruction
            candidates.append(candidate)
        return candidates, rejected, dropped

    @staticmethod
    def _merge(llm: _Candidate, rules: _Candidate) -> None:
        llm.sources = ["llm", "rules"]
        llm.level = rules.level
        llm.number = rules.number
        if rules.condition is not None:
            llm.condition = rules.condition
        llm.after_failure = llm.after_failure or rules.after_failure
        llm.goto = rules.goto
        if rules.role not in ("other", "unknown", "preamble"):
            llm.role = rules.role

    @staticmethod
    def _build_steps(candidates: list[_Candidate], text: str) -> list[Step]:
        steps: list[Step] = []
        for n, c in enumerate(candidates, 1):
            body = text[c.start:c.end]
            entities = [e.canonical for e in extract_entities(body)]
            condition_text = text[c.condition[0]:c.condition[1]] if c.condition else None
            after_failure = c.after_failure
            if not after_failure and condition_text and c.condition[0] >= c.start:
                # only a step that itself opens with "Si le problème persiste" falls back from the one before;
                # a condition read from the heading above covers the whole section, not each step
                after_failure = bool(FAILURE_RE.match(condition_text))
            steps.append(
                Step(
                    n=n,
                    start=c.start,
                    end=c.end,
                    text=body,
                    kind=c.kind if c.kind in ("check", "action") else step_kind(body, c.role),
                    role=c.role,
                    level=c.level,
                    condition=condition_text,
                    after_failure=after_failure,
                    instruction=c.instruction,
                    entities=list(dict.fromkeys(entities)),
                    refers_to=[e[len("kb:"):] for e in dict.fromkeys(entities) if e.startswith("kb:")],
                    sources=c.sources,
                )
            )
        by_number = {c.number: step.n for c, step in zip(candidates, steps) if c.number is not None}
        for index, (candidate, step) in enumerate(zip(candidates, steps)):
            if step.after_failure and index > 0 and steps[index - 1].on_failure is None:
                steps[index - 1].on_failure = step.n
            if candidate.goto is not None:
                target = by_number.get(candidate.goto)
                if target is None and 1 <= candidate.goto <= len(steps):
                    target = candidate.goto
                if target is not None and target != step.n:
                    if step.after_failure and index > 0:
                        steps[index - 1].on_failure = target
                    else:
                        step.goto = target
            if candidate.failure_span is not None and step.on_failure is None:
                for other in steps:
                    if other.n != step.n and iou((other.start, other.end), candidate.failure_span) > 0:
                        step.on_failure = other.n
                        break
        return steps


def summarize_kb(decomposed: list[DecomposedFiche]) -> dict:
    statuses = Counter(d.status for d in decomposed)
    steps = sum(len(d.steps) for d in decomposed)
    verified = sum(d.checks.get("steps_verified", 0) for d in decomposed)
    agreements = [d.methods.get("agreement") for d in decomposed if d.methods.get("agreement") is not None]
    return {
        "fiches": len(decomposed),
        "guided": statuses.get("guided", 0),
        "citable": statuses.get("citable", 0),
        "info_only": statuses.get("info_only", 0),
        "steps": steps,
        "steps_verified": verified,
        "llm_quotes_rejected": sum(d.checks.get("llm_quotes_rejected", 0) for d in decomposed),
        "instructions_dropped": sum(d.checks.get("instructions_dropped", 0) for d in decomposed),
        "failure_links": sum(d.checks.get("failure_links", 0) for d in decomposed),
        "llm_fiches": len(agreements),
        "mean_agreement": round(sum(agreements) / len(agreements), 3) if agreements else None,
        "boilerplate_removed": sum(d.boilerplate_removed for d in decomposed),
    }


__all__ = ["DecomposedFiche", "Decomposer", "Section", "Step", "align", "iou", "summarize_kb"]
