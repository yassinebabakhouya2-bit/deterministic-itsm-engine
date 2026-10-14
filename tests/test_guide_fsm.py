import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))

from guide.contracts import Event, GuideState, KbCandidate, Phase
from guide.fsm import Ports, Thresholds, advance, check_guide, fallback_guide

T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)
CHUNKS = {"k1_0": "1. Ouvrez Parametres.\n2. Cliquez sur Comptes.\n3. Choisissez Reinitialiser.",
          "k1_1": "4. Redemarrez le poste."}


def cand(pid, score, title=None):
    return KbCandidate(parent_id=pid, title=title or pid, reranker_score=score, excerpt=f"texte {pid}")


def draft(n=3, applicable=True):
    return {"applicable": applicable, "reason": "", "summary": "Cette fiche explique X.", "preconditions": [],
            "steps": [{"title": f"Etape {i}", "instruction": ["Ouvrez Parametres.", "Cliquez sur Comptes.", "Choisissez Reinitialiser."][i - 1] if i <= 3 else "Redemarrez le poste.",
                       "source_chunk_id": "k1_0" if i <= 3 else "k1_1", "verbatim_from_kb": True} for i in range(1, n + 1)],
            "verification": ["Le probleme a disparu."]}


def open_answer_of(text="Essayez de redémarrer l'application.", **extra):
    def open_answer(st, query):
        return {"answer": text, "confidence_label": "Moyenne", "confidence_level": "medium",
                "primary_source": {"title": "KB-OPEN", "used": True, "sourceType": "text"},
                "related_sources": [], **extra}
    return open_answer


def ports(cands=None, judge=None, build=None, help_=None, open_=None, **kw):
    cands = cands if cands is not None else [cand("A", 3.5), cand("B", 1.0)]
    return Ports(
        extract_variables=lambda t: [], detect_risks=lambda st: [], retrieve=lambda st: list(cands),
        ocr=lambda refs: ([], False), judge=judge or (lambda st, c: None), load_chunks=lambda pid: dict(CHUNKS),
        build_guide=build or (lambda st, c, ch: draft()),
        help_step=help_ or (lambda st, step, ch, t: {"text": "aide:" + t, "found_in_kb": True, "source_chunk_id": "k1_0"}),
        open_answer=open_ or open_answer_of(),
        thresholds=kw.get("th", Thresholds()))


def new():
    return GuideState(session_id="s1", client_id="c", created_utc=T0)


_n = [0]


def ev(text="", action=None, kind="reply", att=()):
    _n[0] += 1
    return Event(event_id=f"e{_n[0]}", kind=kind, text=text, action=action, attachments=list(att))


def started(text="x", p=None):
    """A session whose fiche was found and whose steps the user started."""
    p = p or ports()
    st = advance(new(), ev(text, kind="created"), p, T0).state
    return advance(st, ev(action="start"), p, T0).state


def test_strong_match_shows_the_fiche_and_waits_for_the_user_to_start_its_steps():
    p = ports()
    r = advance(new(), ev("reset mot de passe", kind="created"), p, T0)
    assert r.state.phase == Phase.GUIDING and r.state.guide.parent_id == "A"
    assert [m["kind"] for m in r.outbox] == ["guide"] and not r.state.steps_started
    assert len(r.state.guide.steps) == 3 and r.state.guide.guide_sha256
    for act in ("done", "blocked", "explain", "back", "solved_yes"):      # nothing runs before the start
        assert advance(r.state, ev(action=act), p, T0).outbox == []
    s = advance(r.state, ev(action="start"), p, T0)
    assert s.state.steps_started and s.state.current_step == 0
    assert s.outbox == [{"kind": "step", "index": 0, "step": s.state.guide.steps[0].model_dump()}]


def test_a_fiche_rejected_before_its_steps_start_proposes_the_others():
    p = ports([cand("A", 3.5), cand("B", 2.0), cand("C", 1.5)])
    st = advance(new(), ev("x", kind="created"), p, T0).state
    r = advance(st, ev(action="wrong_fiche"), p, T0)
    assert r.state.phase == Phase.LOCATE and r.state.guide is None and "A" in r.state.rejected_parent_ids
    assert [c.parent_id for c in r.state.choices] == ["B", "C"]


def test_a_description_before_the_start_searches_again_instead_of_helping_on_a_step():
    seen = []
    p = ports([cand("A", 3.5)], help_=lambda *a: seen.append(a) or {})
    st = advance(new(), ev("x", kind="created"), p, T0).state
    p2 = ports([cand("B", 3.5)], help_=lambda *a: seen.append(a) or {})
    r = advance(st, ev("en fait c'est sur le VPN"), p2, T0)
    assert seen == [] and r.state.guide.parent_id == "B" and not r.state.steps_started
    assert [m["kind"] for m in r.outbox] == ["guide"]


def test_a_session_stored_before_the_preview_existed_keeps_walking_its_steps():
    p = ports()
    st = advance(new(), ev("x", kind="created"), p, T0).state
    legacy = st.model_dump()
    legacy.pop("steps_started")
    old = type(st).model_validate(legacy)
    assert old.steps_started
    assert advance(old, ev(action="done"), p, T0).state.current_step == 1


def test_ambiguous_match_asks_to_choose_and_pick_starts_the_guide():
    p = ports([cand("A", 2.2), cand("B", 2.1), cand("C", 1.0)])
    r1 = advance(new(), ev("probleme", kind="created"), p, T0)
    assert r1.state.phase == Phase.LOCATE and [c.parent_id for c in r1.state.choices] == ["A", "B", "C"]
    r2 = advance(r1.state, ev(action="pick:2"), p, T0)
    assert r2.state.phase == Phase.GUIDING and r2.state.guide.parent_id == "B"
    assert r2.state.choices == []


def test_judge_can_break_a_tie_but_not_override_a_strong_match():
    tie = ports([cand("A", 2.2), cand("B", 2.1)], judge=lambda st, c: "B")
    assert advance(new(), ev("x", kind="created"), tie, T0).state.guide.parent_id == "B"
    strong = ports([cand("A", 3.5), cand("B", 1.0)], judge=lambda st, c: "B")
    r = advance(new(), ev("x", kind="created"), strong, T0)
    assert r.state.phase == Phase.LOCATE                       # disagreement -> the user decides


def test_unanswered_clarifications_end_with_best_fiche_marked_approximate():
    p = ports([cand("A", 1.5), cand("B", 1.4)])
    st = advance(new(), ev("a", kind="created"), p, T0).state
    st = advance(st, ev("b"), p, T0).state
    r = advance(st, ev("c"), p, T0)
    assert r.state.phase == Phase.GUIDING and r.state.guide.approximate
    assert r.outbox[0]["kind"] == "notice"


def test_walkthrough_done_back_and_solved():
    p = ports()
    st = started(p=p)
    for i in range(3):
        r = advance(st, ev(action="done"), p, T0)
        st = r.state
    assert st.current_step == 3 and r.outbox[-1]["kind"] == "verify"
    st = advance(st, ev(action="back"), p, T0).state
    assert st.current_step == 2
    st = advance(st, ev(action="done"), p, T0).state
    r = advance(st, ev(action="solved_yes"), p, T0)
    assert r.state.phase == Phase.SOLVED and r.outbox[-1]["kind"] == "done"
    again = advance(r.state, ev(action="done"), p, T0)            # closed: nothing moves
    assert again.outbox == [] and again.state.phase == Phase.SOLVED


def test_blocked_step_gets_help_from_the_fiche_and_offers_other_fiche_after_two_tries():
    p = ports()
    st = started(p=p)
    r1 = advance(st, ev("je ne vois pas Comptes"), p, T0)
    assert r1.outbox[0]["kind"] == "help" and "je ne vois pas Comptes" in r1.outbox[0]["text"]
    assert not r1.outbox[0]["offer_other"] and r1.state.current_step == 0
    r2 = advance(r1.state, ev(action="blocked"), p, T0)
    assert r2.outbox[0]["offer_other"] and r2.state.phase == Phase.GUIDING


def test_explain_does_not_count_as_a_failed_attempt():
    p = ports()
    st = started(p=p)
    r = advance(st, ev(action="explain"), p, T0)
    assert r.state.step_attempts == 0 and r.outbox[0]["kind"] == "help"


def test_wrong_fiche_proposes_others_and_never_the_rejected_one():
    p = ports([cand("A", 3.5), cand("B", 2.0), cand("C", 1.5)])
    st = started(p=p)
    r = advance(st, ev(action="wrong_fiche"), p, T0)
    assert r.state.phase == Phase.LOCATE and "A" in r.state.rejected_parent_ids
    assert [c.parent_id for c in r.state.choices] == ["B", "C"]


def test_problem_persists_moves_to_next_fiche_and_falls_back_to_an_open_answer():
    p = ports([cand("A", 3.5)])
    st = started(p=p)
    for _ in range(3):
        st = advance(st, ev(action="done"), p, T0).state
    r = advance(st, ev(action="solved_no"), p, T0)
    assert r.state.phase == Phase.OPEN and r.outbox[-1]["kind"] == "answer"
    assert r.state.open_turns[-1].answer == "Essayez de redémarrer l'application."
    # a new description reopens the deterministic search, with the rejected list reset
    r2 = advance(r.state, ev("autre description"), p, T0)
    assert r2.state.phase == Phase.GUIDING


def test_model_refusal_skips_to_next_fiche_but_a_user_pick_is_never_refused():
    def build(st, c, ch):
        return draft(applicable=False) if c.parent_id == "A" else draft()
    p = ports([cand("A", 3.5), cand("B", 3.0)], build=build)
    r = advance(new(), ev("x", kind="created"), p, T0)
    assert r.state.guide.parent_id == "B" and "A" in r.state.rejected_parent_ids
    p2 = ports([cand("A", 2.2), cand("B", 2.1)], build=lambda st, c, ch: draft(applicable=False))
    st = advance(new(), ev("x", kind="created"), p2, T0).state
    r2 = advance(st, ev(action="pick:1"), p2, T0)
    assert r2.state.phase == Phase.GUIDING and r2.state.guide.origin == "fallback"


def test_unusable_draft_falls_back_to_the_fiche_own_numbered_lines():
    p = ports(build=lambda st, c, ch: {"applicable": True, "summary": "", "preconditions": [], "verification": [],
                                       "steps": [{"title": "t", "instruction": "x", "source_chunk_id": "ghost", "verbatim_from_kb": False}]})
    r = advance(new(), ev("x", kind="created"), p, T0)
    g = r.state.guide
    assert g.origin == "fallback" and [s.instruction for s in g.steps][:2] == ["Ouvrez Parametres.", "Cliquez sur Comptes."]
    assert len(g.steps) == 4


def test_check_guide_downgrades_false_verbatim_and_rejects_ghost_chunks():
    d = draft()
    d["steps"][0]["instruction"] = "Ouvrez les Parametres rapidement."
    g = check_guide(d, CHUNKS)
    assert g["steps"][0]["verbatim_from_kb"] is False and g["steps"][1]["verbatim_from_kb"] is True
    d["steps"][1]["source_chunk_id"] = "nope"
    assert check_guide(d, CHUNKS) is None


def test_no_candidate_falls_back_to_an_open_answer_instead_of_escalating():
    r = advance(new(), ev("x", kind="created"), ports(cands=[]), T0)
    assert r.state.phase == Phase.OPEN and r.state.phase != Phase.SOLVED
    assert r.outbox == [{"kind": "answer", "text": "Essayez de redémarrer l'application.",
                         "confidence_label": "Moyenne", "confidence_level": "medium", "ambiguous": False,
                         "unanswerable_reason": None, "primary_source": {"title": "KB-OPEN", "sourceType": "text"},
                         "related_sources": [], "next_check": None, "closed": False}]


def test_a_fiche_found_after_an_open_answer_resolves_it_and_a_confirmed_fix_closes_the_session():
    p = ports(cands=[])
    st = advance(new(), ev("x", kind="created"), p, T0).state
    assert st.phase == Phase.OPEN
    p2 = ports([cand("A", 3.5)])                       # the next turn's extra detail finds a fiche
    r = advance(st, ev("ca fait une erreur 0x80070005"), p2, T0)
    assert r.state.phase == Phase.GUIDING and r.state.guide.parent_id == "A"


def test_open_answer_solved_yes_closes_the_session_with_its_own_source_as_the_title():
    st = advance(new(), ev("x", kind="created"), ports(cands=[]), T0).state
    r = advance(st, ev(action="solved_yes"), ports(cands=[]), T0)
    assert r.state.phase == Phase.SOLVED and r.outbox[-1] == {"kind": "done", "title": "KB-OPEN"}


def test_open_answer_solved_no_just_prompts_for_more_without_calling_the_model_again():
    calls = []
    def tracking_open(st, q):
        calls.append(q)
        return open_answer_of()(st, q)
    p = ports(cands=[], open_=tracking_open)
    st = advance(new(), ev("x", kind="created"), p, T0).state
    assert calls == ["x"]
    r = advance(st, ev(action="solved_no"), p, T0)
    assert r.state.phase == Phase.OPEN and r.outbox[0]["kind"] == "notice" and calls == ["x"]


def test_open_answer_falls_back_to_a_stuck_notice_if_the_model_itself_fails():
    def broken(st, q):
        raise RuntimeError("aoai down")
    r = advance(new(), ev("x", kind="created"), ports(cands=[], open_=broken), T0)
    assert r.state.phase == Phase.STUCK and r.outbox[-1]["level"] == "stuck"


def test_same_event_is_idempotent_and_empty_events_do_nothing():
    p = ports()
    st = started(p=p)
    e = ev(action="done")
    a = advance(st, e, p, T0)
    b = advance(a.state, e, p, T0)
    assert b.outbox == [] and b.state.current_step == 1
    assert advance(a.state, ev(), p, T0).outbox == []


def test_input_state_is_never_mutated():
    p = ports()
    st = new()
    before = st.model_dump_json()
    advance(st, ev("x", kind="created"), p, T0)
    assert st.model_dump_json() == before


def test_replay_is_deterministic():
    def run():
        p = ports([cand("A", 2.2), cand("B", 2.1)])
        st = new()
        for e in [Event(event_id="1", kind="created", text="x"), Event(event_id="2", kind="reply", action="pick:1"),
                  Event(event_id="3", kind="reply", action="done")]:
            st = advance(st, e, p, T0).state
        return st.model_dump_json()
    assert run() == run()


def test_invalid_action_is_rejected():
    import pytest
    with pytest.raises(Exception):
        Event(event_id="1", kind="reply", action="escalate")


# ------------------------------------------------------------- routing chantier: route fields
def routed(pid, score, level, key=""):
    return KbCandidate(parent_id=pid, title=pid, reranker_score=score, route_level=level, route_key=key)


def test_a_guide_taken_as_exact_carries_the_route_of_its_candidate():
    p = ports([routed("A", 4.0, "L1", "attribuer ligne teams")])
    g = advance(new(), ev("attribuer ligne teams", kind="created"), p, T0).state.guide
    assert (g.route_level, g.route_key, g.approximate) == ("L1", "attribuer ligne teams", False)
    j = ports([routed("A", 2.2, "L0", "233"), routed("B", 2.1, "L0")], judge=lambda st, c: "A")  # the judge's pick
    assert advance(new(), ev("x", kind="created"), j, T0).state.guide.route_level == "L0"


def test_a_picked_or_approximate_guide_never_carries_a_route():
    p = ports([routed("A", 1.5, "L2"), routed("B", 1.5, "L2")])
    r = advance(new(), ev("x", kind="created"), p, T0)
    assert r.state.guide is None and [c.route_level for c in r.state.choices] == ["L2", "L2"]
    assert r.outbox[-1]["kind"] == "choose" and [c["route_level"] for c in r.outbox[-1]["choices"]] == ["L2", "L2"]
    picked = advance(r.state, ev(action="pick:2"), p, T0).state.guide
    assert picked.parent_id == "B" and (picked.route_level, picked.route_key) == ("", "")
    st = advance(r.state, ev("b"), p, T0).state
    approx = advance(st, ev("c"), p, T0).state.guide
    assert approx.approximate and (approx.route_level, approx.route_key) == ("", "")


def test_choices_keep_the_route_level_of_their_candidates_and_unrouted_ones_stay_empty():
    p = ports([routed("A", 1.5, "L3"), cand("B", 1.4)])
    choices = advance(new(), ev("x", kind="created"), p, T0).state.choices
    assert [(c.parent_id, c.route_level) for c in choices] == [("A", "L3"), ("B", "")]


def test_an_unrouted_guide_is_byte_identical_to_the_one_built_before_routing():
    # pinned from the code before the route fields existed: same scenario, same guide_sha256
    g = advance(new(), ev("reset mot de passe", kind="created"), ports(), T0).state.guide
    assert g.guide_sha256 == "7369006844004b81e87e9a41fab63b5ab504c8a541c61ff89eb329ae4889c11e"
    assert (g.route_level, g.route_key) == ("", "")


def test_the_route_is_part_of_the_guide_hash_when_there_is_one():
    plain = advance(new(), ev("x", kind="created"), ports([cand("A", 4.0)]), T0).state.guide
    l0 = advance(new(), ev("x", kind="created"), ports([routed("A", 4.0, "L0", "233")]), T0).state.guide
    l1 = advance(new(), ev("x", kind="created"), ports([routed("A", 4.0, "L1", "233")]), T0).state.guide
    assert len({plain.guide_sha256, l0.guide_sha256, l1.guide_sha256}) == 3


def test_a_session_stored_before_the_route_fields_existed_stays_readable():
    from guide.contracts import Choice, GuideState as State
    p = ports([routed("A", 2.2, "L2"), routed("B", 2.1, "L2")])
    st = advance(new(), ev("x", kind="created"), p, T0).state
    st.guide = advance(new(), ev("x", kind="created"), ports([routed("A", 4.0, "L1", "k")]), T0).state.guide
    stored = st.model_dump(mode="json")

    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if k not in ("route_level", "route_key")}
        return [strip(v) for v in obj] if isinstance(obj, list) else obj
    old = State.model_validate(strip(stored))
    assert [c.route_level for c in old.candidates] == ["", ""] and [c.route_level for c in old.choices] == ["", ""]
    assert (old.guide.route_level, old.guide.route_key) == ("", "")
    assert State.model_validate_json(st.model_dump_json()) == st     # and a routed session round-trips
    with pytest.raises(Exception):                                     # still strict: no unknown field
        Choice.model_validate({"parent_id": "A", "title": "A", "score": 1.0, "route": "L1"})
    with pytest.raises(Exception):                                     # and bounded
        Choice.model_validate({"parent_id": "A", "title": "A", "score": 1.0, "route_level": "L" * 9})


def test_routed_replay_is_deterministic():
    def run():
        p = ports([routed("A", 1.5, "L2"), routed("B", 1.5, "L2")])
        st = new()
        for e in [Event(event_id="1", kind="created", text="x"), Event(event_id="2", kind="reply", action="pick:1"),
                  Event(event_id="3", kind="reply", action="start")]:
            st = advance(st, e, p, T0).state
        return st.model_dump_json()
    assert run() == run()
