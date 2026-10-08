"""Guided resolution state machine: pure transitions, injected ports, no escalation.

LOCATE  -> find the exact fiche (retrieval score + margin, cross-checked by an LLM
           judge; otherwise the user picks among <= 3 fiches; after `max_rounds`
           unanswered clarifications the best fiche is taken as "approximate").
GUIDING -> the fiche's steps are shown, then walked one by one (done / blocked /
           explain / back); help comes from the fiche only.
OPEN    -> no fiche matches (deterministically or by search): the free-form
           diagnostic answers instead (the classic assistant's own engine,
           reused -- never a second, duplicated answer path), grounded in the
           KB and this conversation; the next message gives the deterministic
           search another try before falling back to OPEN again.
SOLVED  -> the user confirmed. A fiche that does not solve it, or a wrong fiche,
           leads to the next candidate; with none left OPEN answers instead.
           Nothing is ever handed off automatically.

Termination: every event triggers a bounded amount of work (<= 5 fiche attempts,
2 guide drafts each); the session itself only advances on user events."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .contracts import (TERMINAL, Choice, Event, Guide, GuideState, GuideStep, KbCandidate,
                        OcrFinding, OpenTurn, Phase, Variable)
from .textutil import kb_text, norm

MAX_SEEN = 60
MAX_ATTEMPTS = 5          # fiches tried automatically within one event
DRAFT_TRIES = 2


@dataclass
class Thresholds:
    """Not calibrated yet (see runbook); overridable per client under `diagnostic:`."""
    exact_score: float = 2.0      # reranker score (0-4) under which a fiche is never "exact"
    margin_min: float = 0.5       # gap to the 2nd fiche needed to be exact without the judge
    judge_min_score: float = 1.0  # the judge may not pick a fiche scoring below this
    max_rounds: int = 2           # clarification rounds before the best fiche is taken


@dataclass
class Ports:
    extract_variables: Callable[[str], List[Variable]]
    detect_risks: Callable[[GuideState], List[str]]
    retrieve: Callable[[GuideState], List[KbCandidate]]
    ocr: Callable[[List[str]], Tuple[List[OcrFinding], bool]]
    judge: Callable[[GuideState, List[KbCandidate]], Optional[str]]       # parent_id or None
    load_chunks: Callable[[str], Dict[str, str]]
    build_guide: Callable[[GuideState, KbCandidate, Dict[str, str]], dict]
    help_step: Callable[[GuideState, GuideStep, Dict[str, str], str], dict]
    open_answer: Callable[[GuideState, str], dict]
    thresholds: Thresholds = field(default_factory=Thresholds)


@dataclass
class StepResult:
    state: GuideState
    outbox: List[dict] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def guide_sha256(guide: dict) -> str:
    body = {k: v for k, v in guide.items() if k != "guide_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def margin(cands: List[KbCandidate]) -> float:
    if not cands:
        return 0.0
    return cands[0].reranker_score - (cands[1].reranker_score if len(cands) > 1 else 0.0)


def _merge_variables(st: GuideState, new: List[Variable]) -> None:
    for v in new:
        cur = next((x for x in st.variables if x.name == v.name), None)
        if cur is None:
            st.variables.append(v)
        elif cur.confirmed and cur.value != v.value:
            continue
        elif v.confirmed or v.confidence > cur.confidence:
            st.variables[st.variables.index(cur)] = v


def _ingest(st: GuideState, evt: Event, p: Ports) -> str:
    """Adds the user's text and screenshots to the state; returns the text to reason on
    (typed text + what the screenshots say)."""
    text = (evt.text or "").strip()
    readable_ocr = ""
    if evt.attachments:
        findings, readable = p.ocr(evt.attachments)
        known = {(f.image_sha256, f.kind, f.text) for f in st.ocr_findings}
        for f in findings:
            if (f.image_sha256, f.kind, f.text) not in known:
                st.ocr_findings.append(f)
        if readable:
            readable_ocr = "\n".join(f.text for f in findings if f.kind != "cli_output")
    if text:
        st.conversation_text = (st.conversation_text + "\n" + text).strip()[-4000:]
    feed = "\n".join(x for x in (text, readable_ocr) if x)
    if feed:
        _merge_variables(st, p.extract_variables(feed))
    st.risk_flags = sorted(set(st.risk_flags) | set(p.detect_risks(st)))
    return feed


# ---------------------------------------------------------------- guide build

_NUMBERED = re.compile(r"^\s*(?:\d{1,2}\s*[\.\)]|[-•*]|(?:[ée]tape|step)\s*\d{1,2}\s*[:.\-]?)\s+(\S.*)$", re.I)


def fallback_guide(cand: KbCandidate, chunks: Dict[str, str]) -> dict:
    """Deterministic guide built from the fiche's own lines when the model draft is unusable."""
    found: List[Tuple[str, str]] = []
    for cid, raw in chunks.items():
        for line in kb_text(raw, limit=10**6).splitlines():
            m = _NUMBERED.match(line)
            if m:
                found.append((cid, m.group(1).strip()))
    if not found:
        for cid, raw in chunks.items():
            for para in re.split(r"\n{1,}", kb_text(raw, limit=10**6)):
                if len(para.strip()) >= 25:
                    found.append((cid, para.strip()))
    if not found and chunks:
        cid = next(iter(chunks))
        found = [(cid, kb_text(chunks[cid], limit=600) or cand.title)]
    steps = [{"order": i, "title": t[:60].rstrip(" .,:;"), "instruction": t[:700],
              "source_chunk_id": cid, "verbatim_from_kb": True}
             for i, (cid, t) in enumerate(found[:25], start=1)]
    return {"summary": cand.title, "preconditions": [], "steps": steps, "verification": []}


def check_guide(draft: dict, chunks: Dict[str, str]) -> Optional[dict]:
    """Code-side validation of a model draft: applicable, >= 1 step, every step cites an
    existing chunk; 'verbatim' is kept only when the text really is in the fiche."""
    if not draft or draft.get("applicable") is False:
        return None
    steps = draft.get("steps") or []
    if not steps or len(steps) > 25:
        return None
    corpus = {cid: norm(t) for cid, t in chunks.items()}
    out = []
    for i, s in enumerate(steps, start=1):
        cid = s.get("source_chunk_id")
        instr = (s.get("instruction") or "").strip()
        if cid not in chunks or not instr:
            return None
        verb = bool(s.get("verbatim_from_kb")) and norm(instr) in corpus[cid]
        out.append({"order": i, "title": ((s.get("title") or instr)[:100]).strip(),
                    "instruction": instr[:700], "source_chunk_id": cid, "verbatim_from_kb": verb})
    return {"summary": (draft.get("summary") or "")[:500], "preconditions": list(draft.get("preconditions") or [])[:10],
            "steps": out, "verification": list(draft.get("verification") or [])[:8]}


def _make_guide(st: GuideState, cand: KbCandidate, chunks: Dict[str, str], p: Ports,
                forced: bool, approximate: bool) -> Optional[Guide]:
    """None only when the model says 'not applicable' and the user did not force the fiche."""
    body, origin, refused = None, "llm", False
    for _ in range(DRAFT_TRIES):
        try:
            draft = p.build_guide(st, cand, chunks)
        except Exception:
            draft = None
        if draft and draft.get("applicable") is False:
            refused = True
            break
        body = check_guide(draft, chunks)
        if body:
            break
    if body is None:
        if refused and not forced:
            return None
        body, origin = check_guide(fallback_guide(cand, chunks), chunks), "fallback"
    if body is None:                              # fiche without any text: one pointer step
        cid = next(iter(chunks), cand.chunk_ids[0] if cand.chunk_ids else "c0")
        body = {"summary": cand.title, "preconditions": [], "verification": [],
                "steps": [{"order": 1, "title": "Consulter la fiche", "instruction": f"Ouvrez la fiche « {cand.title} » et suivez-la.",
                           "source_chunk_id": cid, "verbatim_from_kb": False}]}
    g = {"parent_id": cand.parent_id, "title": cand.title, "source_system": cand.source_system,
         "source_url": cand.source_url, "origin": origin, "approximate": approximate, **body}
    g["guide_sha256"] = guide_sha256(g)
    return Guide.model_validate(g)


# ------------------------------------------------------------------- locate

def _choices(cands: List[KbCandidate]) -> List[Choice]:
    return [Choice(parent_id=c.parent_id, title=c.title[:120], score=c.reranker_score) for c in cands[:3]]


def _emit_step(st: GuideState, out: List[dict]) -> None:
    g = st.guide
    if st.current_step >= len(g.steps):
        out.append({"kind": "verify", "verification": g.verification})
    else:
        out.append({"kind": "step", "index": st.current_step, "step": g.steps[st.current_step].model_dump()})


def _start_guiding(st: GuideState, cand: KbCandidate, p: Ports, out: List[dict],
                   forced: bool, approximate: bool) -> bool:
    chunks = p.load_chunks(cand.parent_id)
    g = _make_guide(st, cand, chunks, p, forced, approximate)
    if g is None:
        st.rejected_parent_ids.append(cand.parent_id)
        return False
    st.guide, st.selected_parent_id, st.choices = g, cand.parent_id, []
    st.phase, st.current_step, st.step_attempts, st.locate_rounds = Phase.GUIDING, 0, 0, 0
    out.append({"kind": "guide", "guide": g.model_dump()})
    _emit_step(st, out)
    return True


def _stuck(st: GuideState, out: List[dict], text: str) -> None:
    st.phase, st.choices, st.guide, st.selected_parent_id = Phase.STUCK, [], None, None
    out.append({"kind": "notice", "level": "stuck", "text": text})


def _turn_query(evt: Event) -> str:
    """What this turn asked, for the free-form fallback's own query -- it reasons on the
    conversation's full text already (open_answer is handed the state too), but the model
    does better grounded on what was JUST said than on the whole history squashed flat."""
    text = (evt.text or "").strip()
    return text or ("Capture d'écran jointe : que montre-t-elle ?" if evt.attachments else "")


def _open_answer(st: GuideState, p: Ports, out: List[dict], evt: Event) -> None:
    """No fiche matches, deterministically (kefind) or by search: the free-form diagnostic
    answers instead of a dead end -- the classic assistant's own engine (orchestration/answer.py's
    diagnostic_query_core_keyless), reused rather than duplicated. Still never decides a fiche;
    it only reasons in prose, grounded in the KB and this conversation's own history."""
    st.phase, st.choices, st.guide, st.selected_parent_id = Phase.OPEN, [], None, None
    query = _turn_query(evt)
    try:
        res = p.open_answer(st, query) or {}
    except Exception:
        _stuck(st, out, "Je n'ai plus de fiche à vous proposer et l'assistant ouvert ne répond pas "
                        "pour l'instant. Décrivez le problème autrement ou joignez une capture.")
        return
    answer = (res.get("answer") or "").strip()
    primary = res.get("primary_source") or {}
    st.open_turns = (st.open_turns + [OpenTurn(
        query=query[:1000], answer=answer[:1500],
        primary_title=primary.get("title") if primary.get("used") else None,
        error_codes=list(res.get("detected_error_codes") or [])[:10],
        screen_reading=(res.get("screen_reading") or "")[:500] or None,
    )])[-6:]
    out.append({"kind": "answer", "text": answer[:4000] or "Je n'ai pas trouvé de réponse grounded "
                "dans la base : décrivez le problème autrement ou joignez une capture.",
                "confidence_label": res.get("confidence_label"), "confidence_level": res.get("confidence_level"),
                "ambiguous": bool(res.get("ambiguous")), "unanswerable_reason": res.get("unanswerable_reason"),
                "primary_source": ({"title": primary.get("title"), "sourceType": primary.get("sourceType")}
                                    if primary.get("used") and primary.get("title") else None),
                "related_sources": [{"title": s.get("title"), "sourceType": s.get("sourceType")}
                                    for s in (res.get("related_sources") or []) if s.get("used") and s.get("title")][:3],
                "next_check": res.get("next_check"), "closed": bool(res.get("closed"))})


def _locate(st: GuideState, evt: Event, p: Ports, out: List[dict], force_choice: bool = False,
            picked: Optional[KbCandidate] = None) -> None:
    th = p.thresholds
    if picked is not None:
        if _start_guiding(st, picked, p, out, forced=True, approximate=False):
            return
    for _ in range(MAX_ATTEMPTS):
        cands = [c for c in p.retrieve(st) if c.parent_id not in st.rejected_parent_ids][:5]
        st.candidates = cands
        if not cands:
            _open_answer(st, p, out, evt)
            return
        top = cands[0]
        strong = top.reranker_score >= th.exact_score and (len(cands) == 1 or margin(cands) >= th.margin_min)
        judged = None
        if not force_choice and (len(cands) > 1 or not strong):
            try:
                judged = p.judge(st, cands[:3])
            except Exception:
                judged = None
        by_id = {c.parent_id: c for c in cands[:3]}
        exact = None
        if not force_choice:
            if strong and judged in (None, top.parent_id):
                exact = top
            elif not strong and judged in by_id and by_id[judged].reranker_score >= th.judge_min_score:
                exact = by_id[judged]
        if exact is not None:
            if _start_guiding(st, exact, p, out, forced=False, approximate=False):
                return
            continue                                   # model says "not applicable": next fiche
        if not force_choice and st.locate_rounds >= th.max_rounds:
            if _start_guiding(st, top, p, out, forced=False, approximate=True):
                out.insert(0, {"kind": "notice", "level": "info",
                               "text": "Je prends la fiche la plus proche. Dites-moi à l'étape suivante si ce n'est pas la bonne."})
                return
            continue
        st.choices, st.phase = _choices(cands), Phase.LOCATE
        st.locate_rounds += 1
        out.append({"kind": "choose", "choices": [c.model_dump() for c in st.choices],
                    "strong": strong, "round": st.locate_rounds})
        return
    _open_answer(st, p, out, evt)


# ------------------------------------------------------------------ guiding

def _next_fiche(st: GuideState, evt: Event, p: Ports, out: List[dict], text: str) -> None:
    if st.selected_parent_id:
        st.rejected_parent_ids.append(st.selected_parent_id)
    st.guide, st.selected_parent_id, st.current_step, st.step_attempts = None, None, 0, 0
    st.phase, st.locate_rounds = Phase.LOCATE, 0
    out.append({"kind": "notice", "level": "info", "text": text})
    _locate(st, evt, p, out, force_choice=True)


def _guiding(st: GuideState, evt: Event, p: Ports, out: List[dict]) -> None:
    g, n, act = st.guide, len(st.guide.steps), evt.action
    if act == "done":
        if st.current_step < n:
            st.current_step, st.step_attempts = st.current_step + 1, 0
        _emit_step(st, out)
    elif act == "back":
        st.current_step, st.step_attempts = max(0, min(st.current_step, n) - 1), 0
        _emit_step(st, out)
    elif act == "solved_yes":
        st.phase = Phase.SOLVED
        out.append({"kind": "done", "title": g.title})
    elif act == "solved_no":
        _next_fiche(st, evt, p, out, "D'accord, essayons une autre piste.")
    elif act == "wrong_fiche":
        _next_fiche(st, evt, p, out, "Compris, ce n'est pas la bonne fiche. Voici d'autres pistes.")
    elif act in ("blocked", "explain") or act is None:
        said = _ingest(st, evt, p)
        if act is None and not said and not evt.attachments:
            return
        step = g.steps[min(st.current_step, n - 1)]
        ask = said or ("Explique-moi cette étape plus simplement." if act == "explain"
                       else "Cette étape ne fonctionne pas.")
        chunks = p.load_chunks(g.parent_id)
        try:
            res = p.help_step(st, step, chunks, ask)
        except Exception:
            res = {}
        text = (res.get("text") or "").strip() or ("Je n'ai pas pu formuler d'aide : relisez la consigne de la fiche ci-dessus.")
        if act != "explain":
            st.step_attempts += 1
        out.append({"kind": "help", "step": step.order, "text": text[:1200],
                    "from_kb": bool(res.get("found_in_kb")), "source_chunk_id": res.get("source_chunk_id"),
                    "offer_other": st.step_attempts >= 2})


def _open(st: GuideState, evt: Event, p: Ports, out: List[dict]) -> None:
    """Free-form fallback mode: solved_yes/solved_no close the loop exactly like GUIDING's
    verify step; anything else is a new turn -- rejected fiches are cleared (same as leaving
    STUCK always did) so the deterministic search gets a genuinely fresh shot with the extra
    context, falling back to another open answer if it still finds nothing."""
    act = evt.action
    if act == "solved_yes":
        st.phase = Phase.SOLVED
        title = st.open_turns[-1].primary_title if st.open_turns and st.open_turns[-1].primary_title else "votre diagnostic"
        out.append({"kind": "done", "title": title})
        return
    if act == "solved_no":
        out.append({"kind": "notice", "level": "info",
                    "text": "D'accord, décrivez ce qui se passe maintenant ou joignez une nouvelle capture : "
                            "je poursuis le diagnostic."})
        return
    said = _ingest(st, evt, p)
    if not said and not evt.attachments:
        return
    st.rejected_parent_ids, st.locate_rounds = [], 0
    _locate(st, evt, p, out)


# ------------------------------------------------------------------ entry

def advance(state: GuideState, evt: Event, p: Ports, now=None) -> StepResult:
    st = state.model_copy(deep=True)
    out: List[dict] = []
    if evt.event_id in st.seen_event_ids or st.phase in TERMINAL:
        return StepResult(st, out)
    st.seen_event_ids = (st.seen_event_ids + [evt.event_id])[-MAX_SEEN:]
    act = evt.action
    if not (act or (evt.text or "").strip() or evt.attachments):
        return StepResult(st, out)
    if st.phase == Phase.GUIDING and st.guide:
        _guiding(st, evt, p, out)
    elif st.phase == Phase.OPEN:
        _open(st, evt, p, out)
    else:
        if st.phase == Phase.STUCK:
            st.rejected_parent_ids, st.locate_rounds, st.phase = [], 0, Phase.LOCATE
        picked = None
        if act and act.startswith("pick:"):
            i = int(act[5:]) - 1
            if 0 <= i < len(st.choices):
                picked = next((c for c in st.candidates if c.parent_id == st.choices[i].parent_id), None)
        if act == "none":
            st.rejected_parent_ids += [c.parent_id for c in st.choices]
        elif picked is None:
            _ingest(st, evt, p)
        _locate(st, evt, p, out, picked=picked)
    return StepResult(st, out)
