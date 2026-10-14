"""Ports of the guided resolution backed by the deterministic engine (V10 slice 5).

The guide's LOCATE step used to rest on the search index alone (hybrid search + reranker score,
cross-checked by an LLM judge) and its steps were drafted by a model from the fiche's chunks. With
the engine (fn-kecore, ``POST /api/kecore/find``), the code decides which fiche and the steps shown
are the fiche's own sentences, verified at decomposition:

- the engine shows a fiche -> it is THE fiche (score 4, the only "strong" candidate), and the judge
  confirms the engine's choice instead of asking a model;
- the engine asks (a choice between fiches, or which application) -> the fiches it offers are the
  choices (score 1.5, never strong), one per branch of its question in turn, the person picks; no
  model overrides the question;
- the engine abstains, has no map for the client, or cannot be reached -> the search index takes
  over, exactly as before (``fallback`` ports): no question goes unanswered because of the engine;
- the engine's interpretation failed (a model error) and no entity of the ticket backed its decision
  (a ``text_only`` reason) -> the search index too: a French question and English fiches share no
  word, so the fiches closest by the question's own words are noise ("mot de passe expiré" gave
  "How to share mobile phone connection" without the interpretation, the SSPR password reset fiche
  with it);
- a fiche of the engine is guided with its verified steps, in order (25 at most), every step marked
  verbatim; help on a step comes from the same help port, with the fiche's full text as context;
- the engine decided by meaning (``mode: "semantic"``, a run with a calibrated semantic index) and
  found nothing close, or every fiche it offered was rejected: that is the answer -- no search index
  and no model judge pick a fiche instead (the free-form OPEN answer follows, labelled as such);
- the engine decided by words because the embedding service was down (``mode: "degraded"``): its
  fiche is offered, never shown as THE fiche -- the same question gets a semantic decision once the
  service is back, so an outage never guides anyone on a fiche the person did not confirm.

Routing (the answer carries a ``route`` object once a person activated routing tables for the run): the
route level, not the mode, says whether the engine decided --

- L0 (the fiche's number or a validated identifier), L1 (a validated phrasing), L2 (a validated intent
  and application) with a "fiche" decision: THE fiche, even in ``mode: "degraded"`` -- these levels need
  no embedding, so an outage never demotes them; the route's own fiche must be the decision's;
- an L2 choice (several validated fiches) or L3 (closeness by meaning, never exact): every fiche is
  offered (score 1.5, never "strong" under the default thresholds) and the judge returns None -- the
  person picks, no model confirms an offered fiche (and should a client's thresholds still let the
  FSM take one, its guide carries the level and the page says "à confirmer", never "identifiée");
- L4, or a routed answer with nothing left to offer: no candidate, the free-form OPEN answer follows;
  never the search index and its model judge, which could label an index fiche "identifiée" once the
  engine has said no validated route leads anywhere.

Each candidate keeps its level (and the shown fiche its key) so the page says why: "par son numéro",
"par une formulation validée « … »", "Plusieurs fiches validées correspondent", "Fiches proches par le
sens". An answer without ``route`` (or ``route: null``: an older Function, routing not activated, a
routed answer that failed and was answered again without routing) behaves exactly as before.

A candidate of the engine is ``kefind:<run id>:<fiche id>``: the KB map that made the decision is
pinned in the session, so a pick or a help request later reads the same fiche even after a new
kecore run. Rejected fiches are compared by fiche id, so a rejected fiche is never shown again,
whatever run proposes it; when the engine has nothing left, the search index is asked.

Every question carries an ``observe`` key, a hash of the session (never its id): the engine counts
a new software name once per distinct session for its dictionary review (kecore_func/
dictionary_service.py), however many times one session asks again.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Tuple

from .contracts import GuideState, GuideStep, KbCandidate
from .fsm import Ports
from .ports import problem_text

KEFIND_PREFIX = "kefind:"
SHOWN_SCORE = 4.0
OFFERED_SCORE = 1.5
OTHER_SCORE = 1.0
MAX_CANDIDATES = 5
MAX_GUIDE_STEPS = 25
ROUTE_LEVELS = ("L0", "L1", "L2", "L3", "L4")
EXACT_LEVELS = ("L0", "L1", "L2")            # the only levels that may decide THE fiche
MAX_ROUTE_KEY_CHARS = 200
_RUN_RE = re.compile(r"[0-9A-Za-z-]{1,64}")  # always fullmatch: a kecore run id never holds ":"
_FIRST_CLAUSE = re.compile(r"^(.{8,90}?[.:;!?])(?:\s|$)")


def is_kefind(parent_id: str) -> bool:
    return parent_id.startswith(KEFIND_PREFIX)


def parent_id_of(fiche_id: str, run_id: str) -> str:
    return f"{KEFIND_PREFIX}{run_id}:{fiche_id}"


def split_parent(parent_id: str) -> Tuple[Optional[str], str]:
    """(run id, fiche id) of an engine candidate; run id None for an id without a pinned run."""
    rest = parent_id[len(KEFIND_PREFIX):]
    run_id, sep, fiche_id = rest.partition(":")
    if sep and fiche_id and _RUN_RE.fullmatch(run_id):
        return run_id, fiche_id
    return None, rest


def fiche_id_of(parent_id: str) -> str:
    return split_parent(parent_id)[1]


def step_title(text: str) -> str:
    text = " ".join(text.split())
    match = _FIRST_CLAUSE.match(text)
    title = match.group(1) if match else text
    return title if len(title) <= 90 else title[:89].rstrip() + "…"


def observe_key(client_id: str, session_id: str) -> str:
    return hashlib.sha256(f"{client_id}|{session_id}".encode("utf-8")).hexdigest()[:32]


def route_of(answer: Optional[dict]) -> Optional[dict]:
    """The answer's route, or None when the answer was not routed (no "route", or null). A route that is
    not an object is read as a route without a level: still routed -- never exact, never the index."""
    route = answer.get("route") if isinstance(answer, dict) else None
    if route is None:
        return None
    return route if isinstance(route, dict) else {}


def route_level(route: Optional[dict]) -> str:
    """The route's level when it is one of ROUTE_LEVELS, else "" (unrouted, or a level this code does not
    know: it is then never exact, and nothing unknown is stored in a session)."""
    level = (route or {}).get("level")
    return level if isinstance(level, str) and level in ROUTE_LEVELS else ""


def shown_fiche(answer: Optional[dict]) -> Optional[str]:
    """THE fiche the engine shows -- the only one guided without the person's pick -- or None. The single
    rule both ``candidates`` (its score) and ``judge`` (its confirmation) read, so they cannot disagree.

    Routed: an exact level (L0-L2) and a "fiche" decision, whatever the mode, and the route's own fiche
    (when it names one) is the decision's -- an inconsistent answer is offered, never shown. Not routed:
    a "fiche" decision, unless it was made by words during an embedding outage (``mode: "degraded"``)."""
    answer = answer if isinstance(answer, dict) else {}
    decision = answer.get("decision") or {}
    fiche_id = decision.get("fiche_id")
    if decision.get("kind") != "fiche" or not fiche_id:
        return None
    route = route_of(answer)
    if route is None:
        return fiche_id if answer.get("mode") != "degraded" else None
    if route_level(route) in EXACT_LEVELS and route.get("fiche_id", fiche_id) == fiche_id:
        return fiche_id
    return None


def _route_key(route: dict) -> str:
    key = route.get("key")
    return "" if key is None else " ".join(str(key).split())[:MAX_ROUTE_KEY_CHARS]


class KefindPorts:
    """``engine``: find(client, text, observe=...) -> the /kecore/find answer, or None when the client
    has no map; fiche(client, fiche_id, run_id) -> the fiche view with "steps" and "text"."""

    def __init__(self, engine, client_id: str, fallback: Ports):
        self.engine = engine
        self.client_id = client_id
        self.fallback = fallback
        self._finds: Dict[str, Optional[dict]] = {}
        self._fiches: Dict[Tuple[Optional[str], str], dict] = {}

    # ------------------------------------------------------------- engine
    def _find(self, st: GuideState) -> Optional[dict]:
        text = problem_text(st)
        if text not in self._finds:
            try:
                self._finds[text] = (self.engine.find(self.client_id, text,
                                                      observe=observe_key(self.client_id, st.session_id))
                                     if text else None)
            except Exception:  # the engine is unreachable: the search index answers this question
                self._finds[text] = None
        return self._finds[text]

    def _fiche(self, parent_id: str) -> dict:
        key = split_parent(parent_id)
        if key not in self._fiches:
            self._fiches[key] = self.engine.fiche(self.client_id, key[1], key[0])
        return self._fiches[key]

    def candidates(self, answer: Optional[dict], st: GuideState) -> List[KbCandidate]:
        decision = (answer or {}).get("decision") or {}
        kind, run_id = decision.get("kind"), (answer or {}).get("run_id")
        if kind not in ("fiche", "question") or not isinstance(run_id, str) or not _RUN_RE.fullmatch(run_id):
            return []
        route = route_of(answer)
        if route is None and answer.get("interpreted") is False \
                and str(decision.get("reason") or "").startswith("text_only"):
            return []  # interpretation failed, nothing but the question's own words: the search index answers
        labels = {c.get("fiche_id"): c.get("label") or c.get("fiche_id") for c in answer.get("candidates") or []}
        shown = shown_fiche(answer)
        branches = [[option["fiche_id"]] if option.get("fiche_id") else list(option.get("fiches") or [])
                    for option in decision.get("options") or []]
        # a "fiche" decision the engine does not show (words during an outage, or not an exact route) is offered first
        offered: List[str] = [decision["fiche_id"]] if kind == "fiche" and not shown and decision.get("fiche_id") else []
        for rank in range(max((len(b) for b in branches), default=0)):  # one fiche per branch in turn
            for branch in branches:
                if rank < len(branch) and branch[rank] and branch[rank] not in offered:
                    offered.append(branch[rank])
        ordered: List[str] = []
        for fiche_id in ([shown] if shown else []) + offered + list(decision.get("fiches") or []):
            if fiche_id and fiche_id not in ordered:
                ordered.append(fiche_id)
        # routed and not exact (an L2 choice, L3): every fiche is offered alike, the person picks
        offer_all = route is not None and not shown
        level = route_level(route)
        rejected = {fiche_id_of(p) for p in st.rejected_parent_ids if is_kefind(p)}
        out = []
        for fiche_id in ordered:
            if fiche_id in rejected:
                continue
            score = (SHOWN_SCORE if fiche_id == shown else
                     OFFERED_SCORE if fiche_id in offered or offer_all else OTHER_SCORE)
            # the level says how THIS fiche came: the shown one (with the key that decided), or every offered
            # one; the other fiches of an exact answer came by no route
            routed = route is not None and (fiche_id == shown or offer_all)
            out.append(KbCandidate(parent_id=parent_id_of(fiche_id, run_id), title=(labels.get(fiche_id) or fiche_id)[:300],
                                   reranker_score=score, source_system="sharepoint",
                                   route_level=level if routed else "",
                                   route_key=_route_key(route) if routed and fiche_id == shown else ""))
        return out[:MAX_CANDIDATES]

    # --------------------------------------------------------------- ports
    def retrieve(self, st: GuideState) -> List[KbCandidate]:
        answer = self._find(st)
        found = self.candidates(answer, st)
        if found:
            return found
        if route_of(answer) is not None:
            return []  # routed (L4, or every fiche it offered rejected): no validated route IS the answer --
            #            never the search index, whose model judge could label one of its fiches "identifiée"
        if (answer or {}).get("mode") == "semantic":
            return []  # decided by meaning on the run's calibrated index: nothing (left) close IS the answer
        return self.fallback.retrieve(st)

    def judge(self, st: GuideState, cands: List[KbCandidate]) -> Optional[str]:
        if cands and all(is_kefind(c.parent_id) for c in cands):
            shown = shown_fiche(self._find(st))
            for c in cands:
                if shown and fiche_id_of(c.parent_id) == shown:
                    return c.parent_id
            return None  # the code did not decide (a question, an outage, an L2 choice, L3): the person chooses
        return self.fallback.judge(st, cands)

    def load_chunks(self, parent_id: str) -> Dict[str, str]:
        if not is_kefind(parent_id):
            return self.fallback.load_chunks(parent_id)
        view = self._fiche(parent_id)
        chunks = {f"s{step['n']}": step["text"] for step in view.get("steps") or [] if (step.get("text") or "").strip()}
        if view.get("text"):
            chunks["fiche"] = view["text"]
        return chunks

    def build_guide(self, st: GuideState, cand: KbCandidate, chunks: Dict[str, str]) -> dict:
        if not is_kefind(cand.parent_id):
            return self.fallback.build_guide(st, cand, chunks)
        view = self._fiche(cand.parent_id)
        steps = [s for s in view.get("steps") or [] if f"s{s['n']}" in chunks]
        if not steps:  # a fiche without a verified step: drafted from its own text, checked as usual
            return self.fallback.build_guide(st, cand, {k: v for k, v in chunks.items() if k == "fiche"} or chunks)
        return {
            "applicable": True, "reason": "", "summary": (view.get("label") or cand.title)[:500],
            "preconditions": [p.get("label") for p in view.get("prerequisites") or [] if p.get("label")][:5],
            "verification": [],
            "steps": [{"title": step_title(s["text"]), "instruction": s["text"][:700],
                       "source_chunk_id": f"s{s['n']}", "verbatim_from_kb": True} for s in steps[:MAX_GUIDE_STEPS]],
        }

    def help_step(self, st: GuideState, step: GuideStep, chunks: Dict[str, str], user_text: str) -> dict:
        return self.fallback.help_step(st, step, chunks, user_text)

    def open_answer(self, st: GuideState, query: str) -> dict:
        """The free-form fallback is not something the engine does -- it's the classic
        assistant's own answer, so this always delegates straight through."""
        return self.fallback.open_answer(st, query)

    def ports(self) -> Ports:
        fb = self.fallback
        return Ports(extract_variables=fb.extract_variables, detect_risks=fb.detect_risks, retrieve=self.retrieve,
                     ocr=fb.ocr, judge=self.judge, load_chunks=self.load_chunks, build_guide=self.build_guide,
                     help_step=self.help_step, open_answer=self.open_answer, thresholds=fb.thresholds)


def with_engine(fallback: Ports, engine, client_id: str) -> Ports:
    """The guide's ports with the engine in front of the search index; the index alone without an engine."""
    return fallback if engine is None else KefindPorts(engine, client_id, fallback).ports()


__all__ = ["KefindPorts", "with_engine", "KEFIND_PREFIX", "is_kefind", "parent_id_of", "split_parent", "fiche_id_of",
           "observe_key", "step_title", "route_of", "route_level", "shown_fiche", "ROUTE_LEVELS", "EXACT_LEVELS"]
