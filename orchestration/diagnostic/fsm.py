"""Diagnostic finite-state machine: pure transitions, injected ports.

The LLM never decides a state change: it only feeds facts (extraction, OCR
reading) and text (question wording, plan drafting) through `Ports`. Every
transition below is plain code, so a replay with the same events and the same
port outputs yields the same states (determinism), and every loop is bounded
(termination): turn budget, deadline, OCR budget, plan-failure budget,
stagnation check, question de-duplication, hard iteration guard.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .contracts import (
    CONF_THRESHOLD, TERMINAL, DiagnosticState, Event, FinalExecutionPlan, FsmState,
    KbCandidate, OcrFinding, PlanRefusal, TurnRecord, UserNextActionPrompt, Variable,
)

MARGIN_MIN = 0.6          # minimum reranker gap between first and second document
MIN_PROGRESS = 0.05       # minimum confidence gain per turn when evidence is unchanged
RERANKER_MAX = 4.0        # Azure semantic reranker scale
ITERATION_GUARD = 8       # hard cap on transitions per event, independent of the business budget
OCR_MAX_FAILURES = 2
PLAN_MAX_FAILURES = 2


@dataclass
class Ports:
    """Side effects, injected. All must be deterministic for a given input."""
    extract_variables: Callable[[str], List[Variable]]
    detect_risks: Callable[[DiagnosticState], List[str]]
    retrieve: Callable[[DiagnosticState], List[KbCandidate]]
    ocr: Callable[[List[str]], Tuple[List[OcrFinding], bool]]
    next_question: Callable[[DiagnosticState], Optional[UserNextActionPrompt]]
    load_chunks: Callable[[str], Dict[str, str]]       # chunk_id -> text of the selected document
    build_plan: Callable[[DiagnosticState, Dict[str, str]], dict]


@dataclass
class StepResult:
    state: DiagnosticState
    outbox: List[dict] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def evidence_hash(st: DiagnosticState) -> str:
    facts = sorted((v.name, v.value.strip().lower()) for v in st.variables)
    ocr = sorted(f.text for f in st.ocr_findings if f.verified)
    return hashlib.sha256(json.dumps([facts, ocr]).encode()).hexdigest()


def margin(cands: List[KbCandidate]) -> float:
    if not cands:
        return 0.0
    if len(cands) == 1:
        return cands[0].reranker_score
    return cands[0].reranker_score - cands[1].reranker_score


def compute_missing(st: DiagnosticState) -> List[str]:
    have = {v.name for v in st.variables if v.confidence >= 0.5}
    have |= {"error_code" for f in st.ocr_findings if f.kind == "error_code" and f.verified}
    return [r for r in st.required_variables if r not in have]


def score_confidence(st: DiagnosticState, cands: List[KbCandidate]) -> float:
    """0.5 * normalised reranker + 0.3 * margin score + 0.2 * variable coverage."""
    if not cands:
        return 0.0
    top = max(0.0, min(1.0, cands[0].reranker_score / RERANKER_MAX))
    m = max(0.0, min(1.0, margin(cands) / 1.0))
    req = st.required_variables
    cov = 1.0 if not req else (len(req) - len(compute_missing(st))) / len(req)
    return round(max(0.0, min(1.0, 0.5 * top + 0.3 * m + 0.2 * cov)), 4)


def compute_plan_sha256(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def merge_variables(st: DiagnosticState, new: List[Variable]) -> None:
    """A confirmed value is never overwritten by a different one; an unconfirmed
    one is replaced only by a higher-confidence or confirmed value."""
    for v in new:
        cur = next((x for x in st.variables if x.name == v.name), None)
        if cur is None:
            st.variables.append(v)
        elif cur.confirmed and cur.value != v.value:
            continue
        elif v.confirmed or v.confidence > cur.confidence:
            st.variables[st.variables.index(cur)] = v


def _record(st: DiagnosticState, before: FsmState, after: FsmState,
            event_id: Optional[str], question_id: Optional[str] = None) -> None:
    st.state = after
    st.history.append(TurnRecord(
        turn=st.turn_count, event_id=event_id, state_before=before, state_after=after,
        question_id=question_id, evidence_hash=evidence_hash(st), confidence=st.confidence,
    ))


def escalate(st: DiagnosticState, reason: str, out: List[dict]) -> None:
    before = st.state
    st.escalation_reason = reason
    _record(st, before, FsmState.HUMAN_ESCALATION, None)
    out.append({"kind": "escalation", "reason": reason, "dossier": {
        "variables": [v.model_dump() for v in st.variables],
        "ocr_findings": [f.model_dump() for f in st.ocr_findings],
        "candidates": [c.model_dump() for c in st.candidates],
        "turns": st.turn_count, "confidence": st.confidence,
        "asked_question_ids": list(st.asked_question_ids),
    }})


# ---------------------------------------------------------- state handlers

def _init_triage(st: DiagnosticState, p: Ports, out: List[dict]) -> None:
    before = st.state
    st.risk_flags = sorted(set(st.risk_flags) | set(p.detect_risks(st)))
    if st.risk_flags:
        return escalate(st, "risk:" + ",".join(st.risk_flags), out)
    if st.pending_attachments:
        st.state = FsmState.OCR_PROCESSING
        return
    st.candidates = sorted(
        (c for c in p.retrieve(st) if c.parent_id not in st.rejected_parent_ids),
        key=lambda c: (-c.reranker_score, c.parent_id),
    )
    st.missing_variables = compute_missing(st)
    st.confidence = score_confidence(st, st.candidates)
    h = evidence_hash(st)
    prev = st.history[-1] if st.history else None
    unchanged = prev is not None and prev.evidence_hash == h and st.turn_count > 0
    gain = (st.confidence - prev.confidence) if prev else 1.0

    if st.confidence >= CONF_THRESHOLD and margin(st.candidates) >= MARGIN_MIN:
        st.selected_parent_id = st.candidates[0].parent_id
        return _record(st, before, FsmState.KB_MATCHED, None)
    if st.turn_count >= st.max_turns:
        return escalate(st, "turn_budget", out)
    if unchanged and gain < MIN_PROGRESS:
        return escalate(st, "stagnation", out)
    _record(st, before, FsmState.NEED_DIAGNOSTIC_DATA, None)


def _need_data(st: DiagnosticState, p: Ports, out: List[dict]) -> None:
    """Entered with no pending event: formulate exactly one request, then wait."""
    before = st.state
    q = p.next_question(st)
    if q is None or q.question_id in st.asked_question_ids:
        return escalate(st, "no_new_question", out)
    st.asked_question_ids.append(q.question_id)
    st.turn_count += 1
    out.append({"kind": "question", "prompt": q.model_dump()})
    _record(st, before, FsmState.NEED_DIAGNOSTIC_DATA, None, question_id=q.question_id)


def _ocr(st: DiagnosticState, p: Ports, out: List[dict]) -> None:
    before = st.state
    findings, ok = p.ocr(list(st.pending_attachments))
    st.pending_attachments = []
    st.ocr_failures_in_row = 0 if ok else st.ocr_failures_in_row + 1
    if st.ocr_failures_in_row >= OCR_MAX_FAILURES:
        return escalate(st, "ocr_unreadable", out)
    st.ocr_findings.extend(findings)
    for f in findings:
        if f.kind == "error_code" and f.verified:
            merge_variables(st, [Variable(name="error_code", value=f.text, source="ocr",
                                          confidence=f.ocr_confidence, confirmed=True)])
    _record(st, before, FsmState.INIT_TRIAGE, None)


def check_plan(raw: dict, st: DiagnosticState, chunks: Dict[str, str]) -> FinalExecutionPlan:
    """Validation that the prompt cannot guarantee: raises ValueError on any violation."""
    if raw.get("applicable") is False:
        r = PlanRefusal.model_validate(raw)
        raise ValueError("refusal:" + r.reason)
    plan = FinalExecutionPlan.model_validate(raw)
    if plan.kb_parent_id != st.selected_parent_id:
        raise ValueError("plan for another document")
    if plan.plan_sha256 != compute_plan_sha256(raw):
        raise ValueError("plan_sha256 mismatch")
    for s in plan.steps:
        text = chunks.get(s.source_chunk_id)
        if text is None:
            raise ValueError(f"unknown source_chunk_id {s.source_chunk_id}")
        if s.verbatim_from_kb and s.instruction.strip() not in text:
            raise ValueError(f"step {s.order} is not verbatim")
        if s.action_type == "agent_action" and st.risk_flags:
            raise ValueError("agent_action with risk flag")
    return plan


def _kb_matched(st: DiagnosticState, p: Ports, out: List[dict]) -> None:
    before = st.state
    chunks = p.load_chunks(st.selected_parent_id)
    try:
        plan = check_plan(p.build_plan(st, chunks), st, chunks)
    except Exception:                       # schema, citation or refusal: same handling
        st.plan_failures += 1
        if st.plan_failures >= PLAN_MAX_FAILURES or st.turn_count >= st.max_turns:
            return escalate(st, "plan_invalid", out)
        # the rejected document cannot be selected again
        st.rejected_parent_ids.append(st.selected_parent_id)
        st.selected_parent_id = None
        return _record(st, before, FsmState.NEED_DIAGNOSTIC_DATA, None)
    st.final_plan = plan
    out.append({"kind": "plan", "plan": plan.model_dump()})
    _record(st, before, FsmState.ACTION_PROPOSED, None)


HANDLERS = {
    FsmState.INIT_TRIAGE: _init_triage,
    FsmState.NEED_DIAGNOSTIC_DATA: _need_data,
    FsmState.OCR_PROCESSING: _ocr,
    FsmState.KB_MATCHED: _kb_matched,
}


# ------------------------------------------------------------------ entry

def _ingest(st: DiagnosticState, evt: Event, p: Ports) -> None:
    if evt.text:
        src = "user_reply" if evt.kind == "reply" else "ticket_text"
        new = []
        for v in p.extract_variables(evt.text):
            if v.source not in ("ocr", "ticket_meta"):
                v = v.model_copy(update={"source": src, "confirmed": v.confirmed or evt.kind == "reply"})
            new.append(v)
        merge_variables(st, new)
    st.pending_attachments = list(evt.attachments)


def advance(state: DiagnosticState, evt: Event, p: Ports, now: datetime) -> StepResult:
    """Consume one event and run until the machine waits or terminates.
    Idempotent on event_id; never mutates its input."""
    if state.state in TERMINAL or evt.event_id in state.seen_event_ids:
        return StepResult(state)
    st = state.model_copy(deep=True)
    out: List[dict] = []
    st.seen_event_ids.append(evt.event_id)

    if evt.kind == "timeout":
        if st.state == FsmState.NEED_DIAGNOSTIC_DATA:
            escalate(st, "reply_timeout", out)
        return StepResult(DiagnosticState.model_validate(st.model_dump()), out)
    if now >= st.deadline_utc:
        escalate(st, "deadline", out)
        return StepResult(DiagnosticState.model_validate(st.model_dump()), out)

    _ingest(st, evt, p)
    if st.state == FsmState.NEED_DIAGNOSTIC_DATA:
        st.state = FsmState.OCR_PROCESSING if st.pending_attachments else FsmState.INIT_TRIAGE

    asked = False
    for _ in range(ITERATION_GUARD):
        if st.state in TERMINAL or (st.state == FsmState.NEED_DIAGNOSTIC_DATA and asked):
            break                            # terminal, or question posted: wait for the user
        was_need = st.state == FsmState.NEED_DIAGNOSTIC_DATA
        HANDLERS[st.state](st, p, out)
        asked = was_need and st.state == FsmState.NEED_DIAGNOSTIC_DATA
    else:
        escalate(st, "iteration_guard", out)
    return StepResult(DiagnosticState.model_validate(st.model_dump()), out)
