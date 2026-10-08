"""The guide with the deterministic engine in front of the search index (V10 slice 5)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from guide.contracts import Event, GuideState, KbCandidate, Phase  # noqa: E402
from guide.fsm import Ports, Thresholds, advance  # noqa: E402
from guide.kefind_ports import KefindPorts, observe_key, split_parent, step_title, with_engine  # noqa: E402

T0 = datetime(2026, 10, 8, tzinfo=timezone.utc)
STEPS = [{"n": 1, "text": "Ouvrez la console Active Directory."},
         {"n": 2, "text": "Clic droit sur le compte, puis Propriétés."},
         {"n": 3, "text": "Cochez Déverrouiller le compte et validez."}]
FICHES = {
    "KB0120": {"fiche_id": "KB0120", "label": "KB0120- LOCKED ACCOUNT", "steps": STEPS,
               "text": "Compte verrouillé.\n" + "\n".join(s["text"] for s in STEPS), "prerequisites": []},
    "KB0200": {"fiche_id": "KB0200", "label": "KB0200- VPN", "steps": [{"n": 1, "text": "Relancez le client VPN."}],
               "text": "Relancez le client VPN.", "prerequisites": []},
}


class Engine:
    def __init__(self, decision=None, down=False, no_map=False, run_id="r1", interpreted=True):
        self.decision = decision
        self.down, self.no_map = down, no_map
        self.run_id = run_id
        self.interpreted = interpreted
        self.calls = []

    def find(self, client, text, answers=(), interpret=True, observe=None):
        self.calls.append(("find", text, observe))
        if self.down:
            raise ConnectionError("engine down")
        if self.no_map:
            return None
        return {"client": client, "run_id": self.run_id, "interpreted": self.interpreted, "decision": self.decision,
                "candidates": [{"fiche_id": f, "label": FICHES[f]["label"]} for f in self.decision.get("fiches", [])]}

    def fiche(self, client, fiche_id, run_id=None):
        self.calls.append(("fiche", fiche_id, run_id))
        return FICHES[fiche_id]


def fiche_decision(shown="KB0120"):
    return {"kind": "fiche", "reason": "entities_clear_lead", "fiche_id": shown, "fiches": [shown, "KB0200"]}


def question_decision():
    return {"kind": "question", "reason": "entities_close_choice", "fiche_id": None, "asks": "fiche",
            "options": [{"fiche_id": "KB0120"}, {"fiche_id": "KB0200"}], "fiches": ["KB0120", "KB0200"]}


def fallback(cands=None):
    cands = cands if cands is not None else [KbCandidate(parent_id="IDX-1", title="Index fiche", reranker_score=3.5)]
    return Ports(
        extract_variables=lambda t: [], detect_risks=lambda st: [], retrieve=lambda st: list(cands),
        ocr=lambda refs: ([], False), judge=lambda st, c: None,
        load_chunks=lambda pid: {"k1": "1. Ouvrez Paramètres.\n2. Cliquez sur Comptes."},
        build_guide=lambda st, c, ch: {"applicable": True, "summary": "Index", "preconditions": [], "verification": [],
                                       "steps": [{"title": "Ouvrir", "instruction": "Ouvrez Paramètres.",
                                                  "source_chunk_id": "k1", "verbatim_from_kb": True}]},
        help_step=lambda st, step, ch, t: {"text": "aide " + ",".join(sorted(ch)), "found_in_kb": True,
                                           "source_chunk_id": next(iter(ch))},
        open_answer=lambda st, q: {"answer": "ouvert: " + q},
        thresholds=Thresholds())


def new():
    return GuideState(session_id="s1", client_id="client-s", created_utc=T0)


_n = [0]


def ev(text="", action=None, kind="reply"):
    _n[0] += 1
    return Event(event_id=f"k{_n[0]}", kind=kind, text=text, action=action)


def test_a_fiche_the_engine_shows_is_guided_with_its_verified_steps():
    engine = Engine(fiche_decision())
    r = advance(new(), ev("Compte bloqué", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    g = r.state.guide
    assert r.state.phase == Phase.GUIDING and g.parent_id == "kefind:r1:KB0120" and not g.approximate
    assert [s.instruction for s in g.steps] == [s["text"] for s in STEPS]
    assert all(s.verbatim_from_kb for s in g.steps) and g.title == "KB0120- LOCKED ACCOUNT"
    assert ("fiche", "KB0120", "r1") in engine.calls


def test_a_question_of_the_engine_becomes_a_choice_and_the_pick_is_guided():
    engine = Engine(question_decision())
    p = with_engine(fallback(), engine, "client-s")
    r1 = advance(new(), ev("problème de compte", kind="created"), p, T0)
    assert r1.state.phase == Phase.LOCATE
    assert [c.parent_id for c in r1.state.choices] == ["kefind:r1:KB0120", "kefind:r1:KB0200"]
    engine.run_id = "r2"  # a new kecore run between the question and the pick
    r2 = advance(r1.state, ev(action="pick:2"), with_engine(fallback(), engine, "client-s"), T0)
    assert r2.state.guide.parent_id == "kefind:r1:KB0200" and ("fiche", "KB0200", "r1") in engine.calls
    r3 = advance(r2.state, ev(action="explain"), with_engine(fallback(), engine, "client-s"), T0)
    assert r3.outbox[-1]["kind"] == "help" and all(c[2] == "r1" for c in engine.calls if c[0] == "fiche")


def test_abstention_no_map_or_engine_down_fall_back_to_the_search_index():
    for engine in (Engine({"kind": "abstain", "reason": "text_only_weak", "fiches": []}), Engine(no_map=True),
                   Engine(down=True)):
        r = advance(new(), ev("problème", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
        assert r.state.guide.parent_id == "IDX-1", engine


def test_a_text_only_decision_whose_interpretation_failed_falls_back_to_the_index():
    shown = {"kind": "fiche", "reason": "text_only_close_title_match", "fiche_id": "KB0120", "fiches": ["KB0120"]}
    noisy = {"kind": "question", "reason": "text_only_close_choice", "fiche_id": None, "asks": "fiche",
             "options": [{"fiche_id": "KB0120"}, {"fiche_id": "KB0200"}], "fiches": ["KB0120", "KB0200"]}
    for decision in (shown, noisy):
        engine = Engine(decision, interpreted=False)
        r = advance(new(), ev("mot de passe expiré", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
        assert r.state.guide.parent_id == "IDX-1", decision["reason"]
    # interpreted, not asked (an engine without the field), or backed by an entity: the engine's decision stands
    for engine in (Engine(shown, interpreted=True), Engine(shown, interpreted=None),
                   Engine(fiche_decision(), interpreted=False)):
        r = advance(new(), ev("mot de passe expiré", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
        assert r.state.guide.parent_id == "kefind:r1:KB0120", engine.decision["reason"]


def test_a_rejected_engine_fiche_is_never_shown_again():
    engine = Engine(fiche_decision())
    p = with_engine(fallback(), engine, "client-s")
    st = advance(new(), ev("Compte bloqué", kind="created"), p, T0).state
    r = advance(st, ev(action="wrong_fiche"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and "kefind:r1:KB0120" in r.state.rejected_parent_ids
    assert [c.parent_id for c in r.state.choices] == ["kefind:r1:KB0200"]
    engine.run_id = "r2"  # rejected stays rejected whatever run proposes it again
    r2 = advance(r.state, ev("toujours bloqué"), with_engine(fallback(), engine, "client-s"), T0)
    assert all(split_parent(c.parent_id)[1] != "KB0120" for c in r2.state.candidates)


def test_when_every_engine_fiche_is_rejected_the_index_is_asked():
    engine = Engine(fiche_decision())
    st = new()
    st.rejected_parent_ids = ["kefind:KB0120", "kefind:KB0200"]
    kp = KefindPorts(engine, "client-s", fallback())
    st.conversation_text = "Compte bloqué"
    assert [c.parent_id for c in kp.retrieve(st)] == ["IDX-1"]


def test_help_on_an_engine_step_sees_the_whole_fiche():
    engine = Engine(fiche_decision())
    st = advance(new(), ev("Compte bloqué", kind="created"), with_engine(fallback(), engine, "client-s"), T0).state
    r = advance(st, ev(action="explain"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.outbox[-1]["kind"] == "help" and "fiche" in r.outbox[-1]["text"]


def test_each_branch_of_the_engine_question_gets_a_choice():
    decision = {"kind": "question", "reason": "entities_close_entity", "asks": "app",
                "options": [{"entity": "app:outlook", "fiches": ["O1", "O2", "O3"]},
                            {"entity": "app:teams", "fiches": ["T1"]}, {"entity": "app:sap", "fiches": ["S1", "S2"]}],
                "fiches": ["O1", "O2", "O3", "T1", "S1", "S2"]}
    kp = KefindPorts(Engine(decision), "client-s", fallback())
    answer = {"run_id": "r1", "decision": decision, "candidates": []}
    assert [split_parent(c.parent_id)[1] for c in kp.candidates(answer, new())][:3] == ["O1", "T1", "S1"]


def test_an_answer_without_a_run_is_not_used_and_the_index_answers():
    kp = KefindPorts(Engine(fiche_decision(), run_id=None), "client-s", fallback())
    st = new()
    st.conversation_text = "Compte bloqué"
    assert [c.parent_id for c in kp.retrieve(st)] == ["IDX-1"]


def test_every_question_carries_a_hash_of_its_session_never_the_id():
    engine = Engine(fiche_decision())
    advance(new(), ev("Compte bloqué", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    keys = {c[2] for c in engine.calls if c[0] == "find"}
    assert keys == {observe_key("client-s", "s1")} and "s1" not in next(iter(keys))
    assert len(next(iter(keys))) == 32 and observe_key("client-s", "s1") != observe_key("client-s", "s2")


def test_legacy_and_pinned_ids_are_read_alike():
    assert split_parent("kefind:r1:KB0120") == ("r1", "KB0120")
    assert split_parent("kefind:KB0120") == (None, "KB0120")
    assert split_parent("kefind:20261007T204049Z-215423:Erreur : accès refusé") == ("20261007T204049Z-215423",
                                                                                     "Erreur : accès refusé")


def test_without_an_engine_the_ports_are_unchanged():
    fb = fallback()
    assert with_engine(fb, None, "client-s") is fb


def test_open_answer_always_delegates_to_the_fallback_never_the_engine():
    engine = Engine(fiche_decision())
    p = with_engine(fallback(), engine, "client-s")
    st = new()
    assert p.open_answer(st, "une question ouverte") == {"answer": "ouvert: une question ouverte"}
    assert not any(c[0] == "find" for c in engine.calls)   # open_answer never asks the engine to decide


def test_step_titles_are_short_and_whole():
    assert step_title("Ouvrez la console. Puis cliquez.") == "Ouvrez la console."
    assert len(step_title("x" * 300)) <= 90
