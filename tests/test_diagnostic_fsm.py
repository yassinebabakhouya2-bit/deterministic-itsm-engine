import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))

from diagnostic.contracts import (DiagnosticState, Event, FsmState, KbCandidate, OcrFinding,
                                  UserNextActionPrompt, Variable)
from diagnostic.fsm import Ports, advance, compute_plan_sha256

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
CHUNKS = {"c1": "Ouvrez Parametres puis Comptes.", "c2": "Cliquez sur Enregistrer."}


def cand(pid, score):
    return KbCandidate(parent_id=pid, title=pid, reranker_score=score, chunk_ids=["c1", "c2"])


def good_plan(st, chunks, verbatim=True, chunk="c1"):
    plan = {
        "schema_version": "1.0", "ticket_id": None, "session_id": st.session_id,
        "kb_parent_id": st.selected_parent_id, "kb_title": "T", "kb_version": "1",
        "source_system": "servicenow_kb", "source_url": "https://x", "confidence": max(st.confidence, 0.95),
        "preconditions": [],
        "steps": [{"order": 1, "instruction": "Ouvrez Parametres puis Comptes.", "action_type": "user_instruction",
                   "source_chunk_id": chunk, "verbatim_from_kb": verbatim, "requires_confirmation": True,
                   "rollback": None}],
        "verification": ["ok"], "closure_code": "pending_user_confirmation", "evidence_hash": "h",
    }
    plan["plan_sha256"] = compute_plan_sha256(plan)
    return plan


def make_ports(**over):
    qn = {"n": 0}

    def next_question(st):
        qn["n"] += 1
        return UserNextActionPrompt(question_id=f"q{qn['n']}", kind="free_text_short",
                                    target_variable="application", text_fr="Quelle application ?",
                                    why_needed="cible la fiche")
    base = dict(
        extract_variables=lambda t: [Variable(name="application", value="Outlook", source="ticket_text",
                                              confidence=0.9)] if "outlook" in t.lower() else [],
        detect_risks=lambda st: [],
        retrieve=lambda st: [cand("KB1", 3.6), cand("KB2", 2.0)],
        ocr=lambda atts: ([], True),
        next_question=next_question,
        load_chunks=lambda pid: CHUNKS,
        build_plan=good_plan,
    )
    base.update(over)
    return Ports(**base)


def new_state(**kw):
    return DiagnosticState(session_id="s1", client_id="client-s", created_utc=T0,
                           deadline_utc=T0 + timedelta(hours=24), **kw)


def ev(i, text="Outlook ne demarre pas", kind="created", att=()):
    return Event(event_id=f"e{i}", kind=kind, text=text, attachments=list(att))


def test_high_confidence_goes_straight_to_plan():
    r = advance(new_state(), ev(1), make_ports(), T0)
    assert r.state.state == FsmState.ACTION_PROPOSED
    assert r.outbox[-1]["kind"] == "plan"
    assert r.state.final_plan.kb_parent_id == "KB1"


def test_low_confidence_asks_one_question_then_matches_after_reply():
    weak = [cand("KB1", 2.0), cand("KB2", 1.9)]
    strong = [cand("KB1", 3.8), cand("KB2", 1.5)]
    calls = {"n": 0}

    def retrieve(st):
        calls["n"] += 1
        return weak if calls["n"] == 1 else strong
    p = make_ports(retrieve=retrieve)
    r1 = advance(new_state(), ev(1), p, T0)
    assert r1.state.state == FsmState.NEED_DIAGNOSTIC_DATA
    assert [m["kind"] for m in r1.outbox] == ["question"]
    assert r1.state.turn_count == 1
    r2 = advance(r1.state, ev(2, "Outlook classique", "reply"), p, T0)
    assert r2.state.state == FsmState.ACTION_PROPOSED


def test_same_event_id_is_ignored():
    p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
    r1 = advance(new_state(), ev(1), p, T0)
    r2 = advance(r1.state, ev(1), p, T0)
    assert r2.state == r1.state and r2.outbox == []


def test_never_loops_forever_without_progress():
    p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
    st = advance(new_state(), ev(1), p, T0).state
    for i in range(2, 12):
        if st.state == FsmState.HUMAN_ESCALATION:
            break
        st = advance(st, ev(i, "toujours pareil", "reply"), p, T0).state
    assert st.state == FsmState.HUMAN_ESCALATION
    assert st.turn_count <= 4
    assert st.escalation_reason in ("stagnation", "turn_budget", "no_new_question")


def test_turn_budget_is_hard_limit_even_if_evidence_keeps_changing():
    n = {"i": 0}

    def extract(t):
        n["i"] += 1
        return [Variable(name="symptom", value=f"s{n['i']}", source="user_reply", confidence=0.9)]
    p = make_ports(extract_variables=extract, retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.95)])
    st = advance(new_state(), ev(1), p, T0).state
    for i in range(2, 20):
        if st.state == FsmState.HUMAN_ESCALATION:
            break
        st = advance(st, ev(i, "x", "reply"), p, T0).state
    assert st.state == FsmState.HUMAN_ESCALATION and st.turn_count <= 4


def test_risk_flag_escalates_immediately():
    p = make_ports(detect_risks=lambda st: ["mfa_reset"])
    r = advance(new_state(), ev(1, "reinitialiser mfa"), p, T0)
    assert r.state.state == FsmState.HUMAN_ESCALATION
    assert r.state.escalation_reason == "risk:mfa_reset" and r.state.final_plan is None


def test_plan_with_unknown_chunk_is_rejected_then_escalates():
    p = make_ports(build_plan=lambda st, ch: good_plan(st, ch, chunk="zzz"))
    r = advance(new_state(), ev(1), p, T0)
    assert r.state.final_plan is None
    assert r.state.state in (FsmState.NEED_DIAGNOSTIC_DATA, FsmState.HUMAN_ESCALATION)
    assert "KB1" in r.state.rejected_parent_ids


def test_non_verbatim_claim_is_rejected():
    p = make_ports(build_plan=lambda st, ch: good_plan(st, ch) | {"steps": [{
        "order": 1, "instruction": "Faites autre chose", "action_type": "user_instruction",
        "source_chunk_id": "c1", "verbatim_from_kb": True, "requires_confirmation": True, "rollback": None}]})
    r = advance(new_state(), ev(1), p, T0)
    assert r.state.final_plan is None


def test_tampered_plan_hash_is_rejected():
    def bad(st, ch):
        pl = good_plan(st, ch)
        pl["verification"] = ["autre"]
        return pl
    r = advance(new_state(), ev(1), make_ports(build_plan=bad), T0)
    assert r.state.final_plan is None


def test_deadline_forces_escalation():
    r = advance(new_state(), ev(1), make_ports(), T0 + timedelta(hours=25))
    assert r.state.state == FsmState.HUMAN_ESCALATION and r.state.escalation_reason == "deadline"


def test_timeout_event_while_waiting_escalates():
    p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
    st = advance(new_state(), ev(1), p, T0).state
    r = advance(st, Event(event_id="t", kind="timeout"), p, T0)
    assert r.state.escalation_reason == "reply_timeout"


def test_two_unreadable_screenshots_escalate():
    p = make_ports(ocr=lambda atts: ([], False),
                   retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
    st = advance(new_state(), ev(1, att=["a.png"]), p, T0).state
    assert st.state != FsmState.HUMAN_ESCALATION or st.escalation_reason == "ocr_unreadable"
    if st.state != FsmState.HUMAN_ESCALATION:
        st = advance(st, ev(2, "voila", "reply", att=["b.png"]), p, T0).state
    assert st.state == FsmState.HUMAN_ESCALATION and st.escalation_reason == "ocr_unreadable"


def test_verified_ocr_error_code_becomes_confirmed_variable():
    f = OcrFinding(image_sha256="a" * 64, kind="error_code", text="0x80070005", ocr_confidence=0.97,
                   bbox=(0, 0, 10, 10), verified=True)
    p = make_ports(ocr=lambda atts: ([f], True))
    st = advance(new_state(required_variables=["application", "error_code"]), ev(1, att=["a.png"]), p, T0).state
    v = next(v for v in st.variables if v.name == "error_code")
    assert v.value == "0x80070005" and v.confirmed and v.source == "ocr"


def test_repeated_question_is_never_asked_twice():
    k = {"i": 0}

    def extract(t):
        k["i"] += 1
        return [Variable(name="symptom", value=f"s{k['i']}", source="user_reply", confidence=0.9)]
    p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)], extract_variables=extract,
                   next_question=lambda st: UserNextActionPrompt(
                       question_id="same", kind="free_text_short", target_variable="application",
                       text_fr="?", why_needed="x"))
    st = advance(new_state(), ev(1), p, T0).state
    st = advance(st, ev(2, "autre info", "reply"), p, T0).state
    assert st.state == FsmState.HUMAN_ESCALATION and st.escalation_reason == "no_new_question"


def test_confirmed_value_is_not_overwritten():
    p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
    st = advance(new_state(), ev(1), p, T0).state
    p2 = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)],
                    extract_variables=lambda t: [Variable(name="application", value="Teams",
                                                          source="user_reply", confidence=1.0)])
    st = advance(st, ev(2, "Outlook", "reply"), make_ports(
        extract_variables=lambda t: [Variable(name="application", value="Outlook", source="user_reply",
                                              confidence=0.9)],
        retrieve=lambda s: [cand("KB1", 2.0), cand("KB2", 1.9)]), T0).state
    st = advance(st, ev(3, "Teams", "reply"), p2, T0).state
    assert next(v for v in st.variables if v.name == "application").value == "Outlook"


def test_terminal_state_is_frozen():
    r = advance(new_state(), ev(1), make_ports(), T0)
    r2 = advance(r.state, ev(2, "autre", "reply"), make_ports(), T0)
    assert r2.state == r.state and r2.outbox == []


def test_replay_is_deterministic():
    def run():
        p = make_ports(retrieve=lambda st: [cand("KB1", 2.0), cand("KB2", 1.9)])
        st = advance(new_state(), ev(1), p, T0).state
        st = advance(st, ev(2, "Outlook classique", "reply"), p, T0).state
        return st.model_dump_json()
    assert run() == run()


def test_input_state_is_not_mutated():
    s = new_state()
    before = s.model_dump_json()
    advance(s, ev(1), make_ports(), T0)
    assert s.model_dump_json() == before
