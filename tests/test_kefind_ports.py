"""The guide with the deterministic engine in front of the search index (V10 slice 5)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from guide.contracts import Event, GuideState, KbCandidate, Phase  # noqa: E402
from guide.fsm import Ports, Thresholds, advance  # noqa: E402
from guide.kefind_ports import (OFFERED_SCORE, OTHER_SCORE, SHOWN_SCORE, KefindPorts, observe_key,  # noqa: E402
                                route_level, route_of, shown_fiche, split_parent, step_title, with_engine)

T0 = datetime(2026, 10, 8, tzinfo=timezone.utc)
STEPS = [{"n": 1, "text": "Ouvrez la console Active Directory."},
         {"n": 2, "text": "Clic droit sur le compte, puis Propriétés."},
         {"n": 3, "text": "Cochez Déverrouiller le compte et validez."}]
FICHES = {
    "KB0120": {"fiche_id": "KB0120", "label": "KB0120- LOCKED ACCOUNT", "steps": STEPS,
               "text": "Compte verrouillé.\n" + "\n".join(s["text"] for s in STEPS), "prerequisites": []},
    "KB0200": {"fiche_id": "KB0200", "label": "KB0200- VPN", "steps": [{"n": 1, "text": "Relancez le client VPN."}],
               "text": "Relancez le client VPN.", "prerequisites": []},
    "KB0233": {"fiche_id": "KB0233", "label": "KB0233 -  Associate a phone line",
               "steps": [{"n": 1, "text": "Ouvrez le centre d'administration Teams."}],
               "text": "Ouvrez le centre d'administration Teams.", "prerequisites": []},
    "KB0300": {"fiche_id": "KB0300", "label": "KB0300- Transfert d'appels", "steps": [{"n": 1, "text": "Ouvrez Teams."}],
               "text": "Ouvrez Teams.", "prerequisites": []},
}


_NO_ROUTE = object()


class Engine:
    def __init__(self, decision=None, down=False, no_map=False, run_id="r1", interpreted=True, mode=None,
                 route=_NO_ROUTE):
        self.decision = decision
        self.down, self.no_map = down, no_map
        self.run_id = run_id
        self.interpreted = interpreted
        self.mode = mode
        self.route = route  # _NO_ROUTE: an answer of a Function that knows nothing of routing
        self.calls = []

    def find(self, client, text, answers=(), interpret=True, observe=None):
        self.calls.append(("find", text, observe))
        if self.down:
            raise ConnectionError("engine down")
        if self.no_map:
            return None
        answer = {"client": client, "run_id": self.run_id, "interpreted": self.interpreted, "decision": self.decision,
                  "candidates": [{"fiche_id": f, "label": FICHES[f]["label"]} for f in self.decision.get("fiches", [])]}
        if self.mode is not None:
            answer["mode"] = self.mode
        if self.route is not _NO_ROUTE:
            answer["route"] = self.route
        return answer

    def fiche(self, client, fiche_id, run_id=None):
        self.calls.append(("fiche", fiche_id, run_id))
        return FICHES[fiche_id]


def fiche_decision(shown="KB0120"):
    return {"kind": "fiche", "reason": "entities_clear_lead", "fiche_id": shown, "fiches": [shown, "KB0200"]}


def question_decision():
    return {"kind": "question", "reason": "entities_close_choice", "fiche_id": None, "asks": "fiche",
            "options": [{"fiche_id": "KB0120"}, {"fiche_id": "KB0200"}], "fiches": ["KB0120", "KB0200"]}


def fallback(cands=None, asked=None):
    """The search index ports; ``asked`` (a list) records every call to its retrieve and judge."""
    cands = cands if cands is not None else [KbCandidate(parent_id="IDX-1", title="Index fiche", reranker_score=3.5)]
    asked = asked if asked is not None else []
    return Ports(
        extract_variables=lambda t: [], detect_risks=lambda st: [],
        retrieve=lambda st: asked.append("retrieve") or list(cands),
        ocr=lambda refs: ([], False), judge=lambda st, c: asked.append("judge") or None,
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
    st = advance(r2.state, ev(action="start"), with_engine(fallback(), engine, "client-s"), T0).state
    r3 = advance(st, ev(action="explain"), with_engine(fallback(), engine, "client-s"), T0)
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


def test_a_semantic_abstention_is_the_answer_no_index_and_no_model_pick_a_fiche():
    engine = Engine({"kind": "abstain", "reason": "semantic_nothing_close", "fiches": []}, interpreted=None,
                    mode="semantic")
    r = advance(new(), ev("la machine à café fuit", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.OPEN and r.state.guide is None
    assert all(c.parent_id != "IDX-1" for c in r.state.candidates)


def test_when_every_semantic_fiche_is_rejected_the_index_is_not_asked():
    engine = Engine(fiche_decision(), interpreted=None, mode="semantic")
    st = new()
    st.rejected_parent_ids = ["kefind:KB0120", "kefind:KB0200"]
    st.conversation_text = "Compte bloqué"
    assert KefindPorts(engine, "client-s", fallback()).retrieve(st) == []


def test_a_fiche_decided_by_words_during_an_embedding_outage_is_offered_not_shown():
    engine = Engine(fiche_decision(), mode="degraded")
    r = advance(new(), ev("Compte bloqué", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and r.state.guide is None
    assert [c.parent_id for c in r.state.choices][0] == "kefind:r1:KB0120"
    assert KefindPorts(engine, "client-s", fallback()).judge(r.state, r.state.candidates) is None
    # the same decision made by meaning is shown and guided at once
    shown = advance(new(), ev("Compte bloqué", kind="created"),
                    with_engine(fallback(), Engine(fiche_decision(), mode="semantic"), "client-s"), T0)
    assert shown.state.phase == Phase.GUIDING and shown.state.guide.parent_id == "kefind:r1:KB0120"


def test_help_on_an_engine_step_sees_the_whole_fiche():
    engine = Engine(fiche_decision())
    st = advance(new(), ev("Compte bloqué", kind="created"), with_engine(fallback(), engine, "client-s"), T0).state
    st = advance(st, ev(action="start"), with_engine(fallback(), engine, "client-s"), T0).state
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


# ------------------------------------------------------------- routing chantier: the engine's route levels
KEYS = {"L0": "233", "L1": "attribuer ligne teams", "L2": "ATTRIBUER_LIGNE|teams"}
RULES = {"L0": "fiche_number", "L1": "alias", "L2": "intent_app", "L3": "semantic", "L4": "none"}


def route(level, fiche_id=None, key=None):
    """The ``route`` object of a /find answer (DESIGN §6), as kefind.route.answer builds it."""
    return {"version": 1, "level": level, "rule": RULES[level], "key": key, "fiche_id": fiche_id,
            "tables_sha256": "t" * 64, "content_sha256": "c" * 64, "build_id": "b1", "run_id": "r1",
            "l0": None, "l1": None, "l2": None}


def exact_decision(fiche_id="KB0233", level="L1"):
    """An exact L0/L1/L2 route: the fiche alone, designated, no embedding and no interpretation."""
    reason = {"L0": "route_l0_fiche_number", "L1": "route_l1_alias", "L2": "route_l2_intent_app"}[level]
    return {"kind": "fiche", "reason": reason, "fiche_id": fiche_id, "fiches": [fiche_id], "designated": True}


def choice_decision(offered, fiches, reason):
    """An L2 choice or an L3 downgrade: a question between fiches (funnel._choice), every candidate in fiches."""
    return {"kind": "question", "reason": reason, "fiche_id": None, "asks": "fiche",
            "options": [{"fiche_id": f, "label": FICHES[f]["label"]} for f in offered], "fiches": fiches}


def asked_state(text="x"):
    st = new()
    st.conversation_text = text
    return st


def test_an_exact_route_guides_its_fiche_in_every_mode_even_during_an_embedding_outage():
    for level in ("L0", "L1", "L2"):
        for mode in ("degraded", "semantic", "words", None):
            engine = Engine(exact_decision(level=level), mode=mode, interpreted=None,
                            route=route(level, "KB0233", KEYS[level]))
            r = advance(new(), ev("comment attribuer une ligne teams", kind="created"),
                        with_engine(fallback(), engine, "client-s"), T0)
            g = r.state.guide
            assert r.state.phase == Phase.GUIDING and g.parent_id == "kefind:r1:KB0233", (level, mode)
            assert not g.approximate and (g.route_level, g.route_key) == (level, KEYS[level]), (level, mode)
            assert [s.instruction for s in g.steps] == ["Ouvrez le centre d'administration Teams."]


def test_the_fiche_of_an_exact_route_carries_its_level_and_key_and_the_judge_confirms_it():
    decision = {**exact_decision(level="L1"), "fiches": ["KB0233", "KB0300"]}
    kp = KefindPorts(Engine(decision, mode="degraded", route=route("L1", "KB0233", KEYS["L1"])), "client-s",
                     fallback())
    st = asked_state()
    cands = kp.retrieve(st)
    assert [(c.parent_id, c.reranker_score, c.route_level, c.route_key) for c in cands] == [
        ("kefind:r1:KB0233", SHOWN_SCORE, "L1", KEYS["L1"]),
        ("kefind:r1:KB0300", OTHER_SCORE, "", "")]  # another fiche of an exact answer came by no route
    assert kp.judge(st, cands) == "kefind:r1:KB0233"


def test_an_l3_fiche_is_offered_never_shown_and_the_judge_never_picks_it():
    downgraded = choice_decision(["KB0120", "KB0200"], ["KB0120", "KB0200", "KB0300"], "route_l3_semantic_clear_lead")
    as_fiche = {"kind": "fiche", "reason": "semantic_clear_lead", "fiche_id": "KB0120",  # never sent: still not shown
                "fiches": ["KB0120", "KB0200"]}
    for decision in (downgraded, as_fiche):
        for mode in ("semantic", "degraded", "words"):
            kp = KefindPorts(Engine(decision, mode=mode, interpreted=None, route=route("L3")), "client-s", fallback())
            st = asked_state()
            cands = kp.retrieve(st)
            assert split_parent(cands[0].parent_id)[1] == "KB0120", (decision["reason"], mode)
            assert all((c.reranker_score, c.route_level, c.route_key) == (OFFERED_SCORE, "L3", "") for c in cands)
            assert kp.judge(st, cands) is None and kp.judge(st, cands[:1]) is None


def test_an_l3_route_never_guides_as_identified_a_fiche_the_person_did_not_pick():
    decision = choice_decision(["KB0120", "KB0200"], ["KB0120", "KB0200"], "route_l3_semantic_close_choice")
    engine = Engine(decision, mode="semantic", interpreted=None, route=route("L3"))
    r = advance(new(), ev("problème de compte", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and r.state.guide is None
    assert [(c.score, c.route_level) for c in r.state.choices] == [(OFFERED_SCORE, "L3"), (OFFERED_SCORE, "L3")]
    # one offered fiche alone is not "strong" either: the person confirms it
    one = Engine(choice_decision(["KB0120"], ["KB0120"], "route_l3_semantic_clear_lead"), mode="semantic",
                 route=route("L3"))
    alone = advance(new(), ev("x", kind="created"), with_engine(fallback(), one, "client-s"), T0).state
    assert alone.phase == Phase.LOCATE and alone.guide is None and len(alone.choices) == 1
    # the clarification rounds end on the closest fiche: marked approximate, carrying no route
    st = advance(r.state, ev("toujours bloqué"), with_engine(fallback(), engine, "client-s"), T0).state
    last = advance(st, ev("encore"), with_engine(fallback(), engine, "client-s"), T0).state
    assert last.phase == Phase.GUIDING and last.guide.approximate and last.guide.route_level == ""
    # a pick is the person's: guided, never labelled by a route
    picked = advance(r.state, ev(action="pick:2"), with_engine(fallback(), engine, "client-s"), T0).state.guide
    assert picked.parent_id == "kefind:r1:KB0200" and not picked.approximate
    assert (picked.route_level, picked.route_key) == ("", "")


def test_an_l2_choice_offers_every_validated_fiche_alike_and_a_pick_carries_no_route():
    decision = choice_decision(["KB0233", "KB0300"], ["KB0233", "KB0300"], "route_l2_choice")
    engine = Engine(decision, mode="degraded", interpreted=None, route=route("L2", key=KEYS["L2"]))
    r = advance(new(), ev("attribuer une ligne", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and r.state.guide is None
    assert [(c.parent_id, c.score, c.route_level) for c in r.state.choices] == [
        ("kefind:r1:KB0233", OFFERED_SCORE, "L2"), ("kefind:r1:KB0300", OFFERED_SCORE, "L2")]
    assert all(c.route_key == "" for c in r.state.candidates)  # only THE fiche carries the key that decided
    assert KefindPorts(engine, "client-s", fallback()).judge(r.state, r.state.candidates) is None
    g = advance(r.state, ev(action="pick:1"), with_engine(fallback(), engine, "client-s"), T0).state.guide
    assert g.parent_id == "kefind:r1:KB0233" and (g.route_level, g.route_key) == ("", "")


def test_every_fiche_of_a_routed_choice_is_offered_not_only_its_options():
    decision = choice_decision(["KB0120"], ["KB0120", "KB0200", "KB0300"], "route_l2_choice")
    engine = Engine(decision, route=route("L2"))
    cands = KefindPorts(engine, "client-s", fallback()).candidates(engine.find("client-s", "x"), new())
    assert [(split_parent(c.parent_id)[1], c.reranker_score) for c in cands] == [
        ("KB0120", OFFERED_SCORE), ("KB0200", OFFERED_SCORE), ("KB0300", OFFERED_SCORE)]
    plain = Engine(decision)  # unrouted, the same question keeps its other fiches below its option, as before
    assert [c.reranker_score for c in KefindPorts(plain, "client-s", fallback()).candidates(
        plain.find("client-s", "x"), new())] == [OFFERED_SCORE, OTHER_SCORE, OTHER_SCORE]


def test_l4_opens_the_free_answer_without_ever_asking_the_search_index():
    for mode in (None, "words", "degraded", "semantic"):
        asked = []
        engine = Engine({"kind": "abstain", "reason": "text_only_weak", "fiches": []}, mode=mode, interpreted=False,
                        route=route("L4"))
        r = advance(new(), ev("la machine à café fuit", kind="created"),
                    with_engine(fallback(asked=asked), engine, "client-s"), T0)
        assert r.state.phase == Phase.OPEN and r.state.guide is None and r.state.candidates == [], mode
        assert asked == [] and r.outbox[-1]["kind"] == "answer", mode


def test_a_routed_answer_with_nothing_left_to_offer_opens_the_free_answer_not_the_index():
    asked = []
    engine = Engine(exact_decision(level="L1"), mode="words", route=route("L1", "KB0233", KEYS["L1"]))
    st = advance(new(), ev("attribuer ligne teams", kind="created"),
                 with_engine(fallback(asked=asked), engine, "client-s"), T0).state
    assert st.phase == Phase.GUIDING
    r = advance(st, ev(action="wrong_fiche"), with_engine(fallback(asked=asked), engine, "client-s"), T0)
    assert r.state.phase == Phase.OPEN and "kefind:r1:KB0233" in r.state.rejected_parent_ids and asked == []
    # a routed answer whose run cannot be pinned has no candidate either: still never the index
    no_run = KefindPorts(Engine(exact_decision(level="L0"), run_id=None, route=route("L0", "KB0233", "233")),
                         "client-s", fallback(asked=asked))
    assert no_run.retrieve(asked_state("KB0233")) == [] and asked == []


def test_a_route_naming_another_fiche_than_the_decision_is_offered_not_shown():
    engine = Engine({**exact_decision("KB0233", "L1"), "fiches": ["KB0233", "KB0300"]},
                    route=route("L1", "KB0300", KEYS["L1"]))
    kp = KefindPorts(engine, "client-s", fallback())
    st = asked_state()
    cands = kp.retrieve(st)
    assert [(split_parent(c.parent_id)[1], c.reranker_score, c.route_key) for c in cands] == [
        ("KB0233", OFFERED_SCORE, ""), ("KB0300", OFFERED_SCORE, "")]
    assert kp.judge(st, cands) is None
    r = advance(new(), ev("x", kind="created"), with_engine(fallback(), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and r.state.guide is None


def test_an_unknown_or_malformed_route_is_never_exact_and_never_the_index():
    asked = []
    for bad in ({"level": "L9", "fiche_id": "KB0233"}, {"level": 1}, {"level": "l1", "fiche_id": "KB0233"}, {},
                "L1", ["L1"], 0):
        kp = KefindPorts(Engine(exact_decision(level="L1"), route=bad), "client-s", fallback(asked=asked))
        st = asked_state()
        cands = kp.retrieve(st)
        assert [(c.reranker_score, c.route_level, c.route_key) for c in cands] == [(OFFERED_SCORE, "", "")], bad
        assert kp.judge(st, cands) is None, bad
        abstain = KefindPorts(Engine({"kind": "abstain", "reason": "x", "fiches": []}, route=bad), "client-s",
                              fallback(asked=asked))
        assert abstain.retrieve(st) == [], bad
    assert asked == []


def test_a_null_route_is_exactly_an_answer_without_one():
    decisions = [fiche_decision(), question_decision(),
                 {"kind": "fiche", "reason": "text_only_close_title_match", "fiche_id": "KB0120", "fiches": ["KB0120"]},
                 {"kind": "abstain", "reason": "text_only_weak", "fiches": []}]
    for decision in decisions:
        for mode in (None, "semantic", "degraded", "words"):
            for interpreted in (None, True, False):
                seen = []
                for r in (_NO_ROUTE, None):
                    kp = KefindPorts(Engine(decision, mode=mode, interpreted=interpreted, route=r), "client-s",
                                     fallback())
                    st = asked_state()
                    cands = kp.retrieve(st)
                    assert all((c.route_level, c.route_key) == ("", "") for c in cands)
                    seen.append(([c.model_dump() for c in cands], kp.judge(st, cands)))
                assert seen[0] == seen[1], (decision["reason"], mode, interpreted)


def test_a_routed_choice_made_by_words_after_a_failed_interpretation_is_offered_not_sent_to_the_index():
    asked = []
    decision = {"kind": "question", "reason": "text_only_close_choice", "fiche_id": None, "asks": "fiche",
                "options": [{"fiche_id": "KB0120"}, {"fiche_id": "KB0200"}], "fiches": ["KB0120", "KB0200"]}
    engine = Engine(decision, interpreted=False, mode="degraded", route=route("L3"))
    r = advance(new(), ev("mot de passe expiré", kind="created"),
                with_engine(fallback(asked=asked), engine, "client-s"), T0)
    assert r.state.phase == Phase.LOCATE and [c.route_level for c in r.state.choices] == ["L3", "L3"]
    assert asked == []


def test_the_route_key_is_collapsed_capped_and_always_text():
    for key, want in (("  attribuer \n ligne\tteams ", "attribuer ligne teams"), (233, "233"), (None, ""),
                      ("x" * 500, "x" * 200), ("<b>« ligne »</b>", "<b>« ligne »</b>")):
        engine = Engine(exact_decision(level="L1"), route=route("L1", "KB0233", key))
        cands = KefindPorts(engine, "client-s", fallback()).candidates(engine.find("client-s", "x"), new())
        assert cands[0].route_key == want, key


def test_routed_candidates_are_deterministic_and_keep_the_engine_order():
    decision = choice_decision(["KB0300", "KB0120"], ["KB0120", "KB0200", "KB0300"], "route_l2_choice")

    def once():
        engine = Engine(decision, route=route("L2"))
        return [c.model_dump_json() for c in
                KefindPorts(engine, "client-s", fallback()).candidates(engine.find("client-s", "x"), new())]
    assert once() == once()
    assert [KbCandidate.model_validate_json(c).parent_id for c in once()] == [
        "kefind:r1:KB0300", "kefind:r1:KB0120", "kefind:r1:KB0200"]


def test_route_helpers():
    assert route_of({}) is None and route_of({"route": None}) is None and route_of(None) is None
    assert route_of({"route": "L1"}) == {} and route_of({"route": {"level": "L0"}}) == {"level": "L0"}
    assert route_level({"level": "L2"}) == "L2" and route_level({"level": "L5"}) == "" and route_level(None) == ""
    exact = {"decision": {"kind": "fiche", "fiche_id": "F"}, "mode": "degraded"}
    assert shown_fiche(exact) is None                                  # unrouted: an outage demotes the fiche
    assert shown_fiche({**exact, "mode": "semantic"}) == "F"
    assert shown_fiche({**exact, "route": {"level": "L0"}}) == "F"     # a route naming no fiche: the decision's
    assert shown_fiche({**exact, "route": {"level": "L0", "fiche_id": None}}) is None
    assert shown_fiche({**exact, "route": {"level": "L3", "fiche_id": "F"}}) is None
    assert shown_fiche({**exact, "route": {"level": "L4", "fiche_id": "F"}}) is None
    assert shown_fiche({"decision": {"kind": "question", "fiche_id": "F"}, "route": {"level": "L1", "fiche_id": "F"}}) is None
    assert shown_fiche({"decision": {"kind": "fiche", "fiche_id": ""}, "route": {"level": "L1"}}) is None
    assert shown_fiche(None) is None and shown_fiche({"route": {"level": "L1"}}) is None
