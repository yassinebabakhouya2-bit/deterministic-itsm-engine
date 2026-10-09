import json
import unittest

from kecore.decompose import DecomposedFiche, Step
from kefind.funnel import FunnelConfig, KBMap, fiche_view, find
from kefind.funnel_engine import FunnelEngine
from kefind.interpret import Interpretation
from kefind.io import load_decomposed_jsonl
from scoreboard.dataset import Ticket

from .helpers import EXAMPLES, StubLLM

KINDS = {"err": "error", "evt": "event", "kb": "kb", "update": "update", "app": "app", "os": "os", "path": "path",
         "reg": "registry", "cmd": "command", "url": "url", "menu": "menu", "key": "shortcut"}


def make(fiche_id, text, title=None, entities=(), status="guided", source="", client="clienta", sections=(),
         steps=None):
    """A decomposed fiche as kecore writes it; its steps default to its sentences, verbatim."""
    if steps is None:
        steps, cursor = [], 0
        for sentence in [s for s in text.split(". ") if s]:
            start = text.index(sentence, cursor)
            cursor = start + len(sentence)
            steps.append(Step(n=len(steps) + 1, start=start, end=cursor, text=sentence, kind="action", role="resolution"))
    return DecomposedFiche(
        fiche_id=fiche_id, client=client, title=title or fiche_id, source=source, text=text, text_sha256="",
        status=status, confidence="high", reasons=[], sections=list(sections), steps=steps,
        entities=[{"canonical": c, "kind": KINDS[c.split(":", 1)[0]], "count": 1} for c in entities],
        references=[], methods={}, checks={},
    )


FILLER_TOPICS = ("imprimante bourrage papier", "badge accès parking", "écran externe sans signal",
                 "clavier sans fil pile", "souris bluetooth appairage", "casque audio micro",
                 "téléphone fixe tonalité", "station accueil dock", "batterie portable charge", "wifi invité code",
                 "jeton matériel perdu", "projecteur salle réunion", "scanner réseau dossier", "tablette atelier",
                 "lecteur carte puce")


def filler() -> list[DecomposedFiche]:
    """Unrelated fiches, so that word statistics look like a real KB's and not like a 2-fiche one."""
    return [make(f"KB09{i:02d} - {topic}", f"Vérifiez {topic}. Redémarrez {topic}. Contactez le support pour {topic}.")
            for i, topic in enumerate(FILLER_TOPICS)]


def example_map(client="clienta") -> KBMap:
    return KBMap(client, load_decomposed_jsonl(EXAMPLES / "fiches" / f"{client}.jsonl"))


class ExamplesTest(unittest.TestCase):
    """The 7 example tickets of kefind/examples: the decisions the funnel must keep."""

    def test_example_tickets(self):
        maps = {"clienta": example_map("clienta"), "clientb": example_map("clientb")}
        expected = {"T-1": ("fiche", "KB0030001"), "T-2": ("fiche", "KB0030003"), "T-3": ("fiche", "KB0030004"),
                    "T-4": ("fiche", "KB0030005"), "T-5": ("question", None), "T-6": ("fiche", "KB0040001"),
                    "T-7": ("abstain", None)}
        for line in (EXAMPLES / "tickets.jsonl").read_text(encoding="utf-8").splitlines():
            ticket = json.loads(line)
            finding = find(maps[ticket["client"]], ticket["text"])
            self.assertEqual((finding.kind, finding.fiche_id), expected[ticket["ticket_id"]], ticket["ticket_id"])

    def test_an_error_code_shared_by_two_fiches_asks_for_the_application(self):
        finding = find(example_map(), "L'application reste bloquée au démarrage, erreur 0x80070005.")
        self.assertEqual(finding.kind, "question")
        self.assertEqual(finding.asks, "app")
        self.assertEqual([o["value"] for o in finding.options], ["app:outlook", "app:teams"])
        self.assertEqual(finding.question, "Pour trouver la bonne fiche : l'application concernée est-elle outlook ou teams ?")

    def test_the_answer_narrows_down_to_one_fiche(self):
        finding = find(example_map(), "L'application reste bloquée au démarrage, erreur 0x80070005.", answers=["app:teams"])
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0030002"))
        filtered = next(step for step in finding.trace if step["step"] == "filter")
        self.assertEqual(filtered["levels"]["answered"], ["app:teams"])

    def test_an_answer_unknown_to_the_map_is_ignored_and_traced(self):
        finding = find(example_map(), "Outlook ne démarre plus.", answers=["app:sap", 42])
        entities = finding.trace[0]
        self.assertEqual(entities["answers"], [])
        self.assertEqual(entities["answers_ignored"], ["app:sap"])

    def test_an_entity_unknown_to_the_map_is_ignored_and_traced(self):
        finding = find(example_map(), "Outlook ne démarre plus, erreur 0x80004005.")
        self.assertIn("err:0x80004005", finding.trace[0]["unknown_ignored"])
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0030001"))

    def test_the_shown_fiche_comes_first_among_the_candidates(self):
        finding = find(example_map(), "La caméra reste noire dans Teams.")
        self.assertEqual(finding.fiches[0], finding.fiche_id)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.kbmap = KBMap("clienta", filler() + [
            make("KB0001 - Teams accès refusé", "Teams affiche l'erreur 0x80070005. Videz le cache de Teams.",
                 entities=["app:teams", "err:0x80070005"]),
            make("KB0002 - Outlook profil", "Outlook ne charge plus le profil. Recréez le profil Outlook.",
                 entities=["app:outlook"]),
            make("KB0003 - Outlook sous Windows 11", "Outlook plante sous Windows 11. Réparez Office.",
                 entities=["app:outlook", "os:windows-11"]),
        ])

    def test_the_least_informative_level_is_dropped_first(self):
        finding = find(self.kbmap, "Outlook affiche l'erreur 0x80070005")
        filtered = next(step for step in finding.trace if step["step"] == "filter")
        self.assertEqual([a["levels"] for a in filtered["attempts"]], [["identifier", "application"], ["identifier"]])
        self.assertEqual(filtered["kept"], ["identifier"])
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0001 - Teams accès refusé"))

    def test_the_operating_system_never_filters(self):
        # "Windows 11" is in the ticket and in one Outlook fiche: both Outlook fiches stay candidates
        finding = find(self.kbmap, "Outlook ne charge plus le profil depuis Windows 11")
        filtered = next(step for step in finding.trace if step["step"] == "filter")
        self.assertEqual(filtered["levels"], {"application": ["app:outlook"]})
        self.assertEqual(filtered["attempts"], [{"levels": ["application"], "candidates": 2}])
        self.assertEqual(sorted(finding.fiches), ["KB0002 - Outlook profil", "KB0003 - Outlook sous Windows 11"])

    def test_inside_a_level_one_entity_is_enough(self):
        finding = find(self.kbmap, "Teams et Outlook ne démarrent plus")
        filtered = next(step for step in finding.trace if step["step"] == "filter")
        self.assertEqual(filtered["attempts"], [{"levels": ["application"], "candidates": 3}])

    def test_a_fiche_of_another_client_is_refused(self):
        with self.assertRaises(ValueError):
            KBMap("clienta", [make("KB9", "Texte.", client="clientb")])

    def test_info_only_fiches_are_never_returned(self):
        kbmap = KBMap("clienta", [make("KB0009 - Politique", "Politique des mots de passe Outlook.",
                                       entities=["app:outlook"], status="info_only")])
        finding = find(kbmap, "Politique des mots de passe Outlook")
        self.assertEqual((finding.kind, finding.reason), ("abstain", "empty_map"))


class GraphTest(unittest.TestCase):
    BODY = " ".join(f"mot{i}" for i in range(80))

    def test_a_cited_fiche_number_is_resolved_through_the_graph(self):
        kbmap = KBMap("clienta", filler() + [make("KB0052 - Compte verrouillé", "Déverrouillez le compte dans la console."),
                                             make("KB0053 - Autre", "Autre sujet sans rapport.")])
        finding = find(kbmap, "L'utilisateur a suivi la KB 52 sans succès")
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0052 - Compte verrouillé"))
        self.assertEqual(finding.trace[0]["fiche_numbers"], {"52": ["KB0052 - Compte verrouillé"]})

    def test_duplicates_are_merged_before_ranking(self):
        kbmap = KBMap("clienta", filler() + [
            make("KB0120 - Locked account", "Unlock the account. " + self.BODY, entities=["app:active-directory"]),
            make("KB0052 - Locked account", "Unlock the account. " + self.BODY, status="citable",
                 entities=["app:active-directory"]),
        ])
        finding = find(kbmap, "Locked account in Active Directory")
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0120 - Locked account"))
        graph = next(step for step in finding.trace if step["step"] == "graph")
        self.assertEqual(graph["changes"], [{"fiche_id": "KB0052 - Locked account", "change": "duplicate_of",
                                             "to": "KB0120 - Locked account"}])


class TextTest(unittest.TestCase):
    def test_without_entity_a_fiche_is_shown_only_if_the_ticket_names_its_title(self):
        kbmap = KBMap("clienta", filler() + [
            make("KB0044 - Scan to mail", "Configure the scanner. Enter the mail server address. Test a scan."),
            make("KB0045 - Badge", "Badge printing needs a reader. Plug the reader."),
        ])
        named = find(kbmap, "How to setup scan to mail")
        self.assertEqual((named.kind, named.fiche_id), ("fiche", "KB0044 - Scan to mail"))
        unnamed = find(kbmap, "The scanner cannot reach the mail server address")
        self.assertEqual((unnamed.kind, unnamed.asks, unnamed.reason), ("question", "fiche", "text_only_no_title_match"))
        self.assertEqual(unnamed.options[0]["fiche_id"], "KB0044 - Scan to mail")

    def test_a_template_heading_is_replaced_by_the_document_name(self):
        kbmap = KBMap("clienta", filler() + [
            make("KB0032", "Close Teams. Delete the cache folder.", title="General Information",
                 source="Kbs/KB0032 – How to clear the TEAMS cache.docx"),
            make("KB0033", "Open the portal. Reset the password.", title="General Information",
                 source="Kbs/KB0033 - Password reset.docx"),
        ])
        self.assertEqual(kbmap.label("KB0032"), "KB0032 – How to clear the TEAMS cache")
        finding = find(kbmap, "How to clear the Teams cache")
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0032"))

    def test_the_document_name_gives_entities_too(self):
        kbmap = KBMap("clienta", [make("KB0187 - INTUNE Delete Hash", "Delete the hash. Sync the device.")])
        self.assertIn("app:intune", kbmap.entities_of("KB0187 - INTUNE Delete Hash"))

    def test_nothing_close_is_an_abstention(self):
        finding = find(example_map(), "Le clavier du poste ne répond plus du tout depuis ce matin.")
        self.assertEqual(finding.kind, "abstain")

    def test_the_french_negation_non_does_not_pull_in_an_unrelated_fiche(self):
        """"non attribuée" should not drag in any fiche that happens to say "non" somewhere, the way
        "pas" (already a stopword) does not; it is a function word, not a description of the problem."""
        kbmap = KBMap("clienta", filler() + [
            make("KB0233 - Associate a phone line", "Go to the admin center. Assign a phone line to the user.",
                 entities=["app:teams"]),
            make("KB0195 - Suspension procedures", "Verifiez si le compte reste non active apres la procedure.",
                 entities=["app:teams"]),
        ])
        interpretation = Interpretation(terms=["ligne Teams non attribuée", "assign Teams line"])
        finding = find(kbmap, "Comment attribuer une ligne Teams non attribuée ?", interpretation=interpretation)
        offered = ([finding.fiche_id] if finding.fiche_id else
                   [o.get("fiche_id") for o in finding.options if o.get("fiche_id")])
        self.assertNotIn("KB0195 - Suspension procedures", offered)


class IdentifierCaseTest(unittest.TestCase):
    """The question's vector is case-folded: "kb893803" and "KB893803" share one vector, so they must
    share one reading of the ticket's identifiers too."""

    def test_an_identifier_reads_the_same_whatever_its_case(self):
        from kefind.funnel import canonical_identifiers

        self.assertEqual(canonical_identifiers("probleme kb893803 et inc0010005, erreur 0X80070005"),
                         "probleme KB893803 et INC0010005, erreur 0x80070005")
        self.assertEqual(canonical_identifiers("la taskbar et le kbd, request 12"), "la taskbar et le kbd, request 12")
        entities = [next(s for s in find(example_map(), text).trace if s["step"] == "entities")["ticket"]
                    for text in ("erreur 0X80070005 apres kb893803", "erreur 0x80070005 apres KB893803")]
        self.assertEqual(entities[0], entities[1])
        self.assertIn("err:0x80070005", entities[0])


class InterpretationTest(unittest.TestCase):
    """The LLM's search terms bridge languages; they rank, they never filter."""

    def setUp(self):
        self.kbmap = KBMap("clienta", filler() + [
            make("KB0056 - Outlook common issues",
                 "User account locked out in Active Directory: unlock it. Outlook user account locked out: reconnect.",
                 entities=["app:outlook", "app:active-directory"]),
            make("KB0120 - LOCKED ACCOUNT", "The user account is locked out in Active Directory. Unlock the account in the console.",
                 entities=["app:active-directory"]),
            make("KB0121 - Outlook signature", "Open the Outlook options. Edit the signature.", entities=["app:outlook"]),
        ])

    def test_a_french_ticket_reaches_an_english_fiche_through_the_interpretation(self):
        text = "Mon compte est bloqué, je n'arrive plus à me connecter"
        self.assertEqual(find(self.kbmap, text).kind, "abstain")
        interpretation = Interpretation(terms=["locked account", "unlock account", "compte verrouillé"])
        finding = find(self.kbmap, text, interpretation=interpretation)
        self.assertEqual((finding.kind, finding.fiche_id), ("fiche", "KB0120 - LOCKED ACCOUNT"))
        step = next(s for s in finding.trace if s["step"] == "interpret")
        self.assertEqual(step["terms"], ["locked account", "unlock account", "compte verrouillé"])

    def test_the_interpretation_never_filters(self):
        interpretation = Interpretation(terms=["Outlook signature"], application="Outlook")
        finding = find(self.kbmap, "Active Directory : compte verrouillé", interpretation=interpretation)
        filtered = next(s for s in finding.trace if s["step"] == "filter")
        self.assertEqual(filtered["levels"], {"application": ["app:active-directory"]})
        self.assertNotIn("KB0121 - Outlook signature", finding.fiches)

    def test_close_fiches_are_told_apart_by_the_title_the_ticket_names(self):
        finding = find(self.kbmap, "User account locked out in Active Directory")
        rank = next(s for s in finding.trace if s["step"] == "rank")
        self.assertLess(rank["top"][0]["text"] - rank["top"][1]["text"], FunnelConfig().gap)  # close by text
        self.assertEqual((finding.kind, finding.reason, finding.fiche_id),
                         ("fiche", "entities_close_title_match", "KB0120 - LOCKED ACCOUNT"))


class DictionaryTest(unittest.TestCase):
    def test_the_client_dictionary_is_applied_to_the_ticket(self):
        fiches = filler() + [make("KB0070 - Harmony ne répond plus", "Redémarrez le service Harmony. Videz le cache.",
                                  entities=["app:harmony"])]
        with_dictionary = find(KBMap("clienta", fiches, {"harmony": ["Harmony"]}), "Harmony ne répond plus")
        self.assertEqual(with_dictionary.trace[0]["known"], ["app:harmony"])
        self.assertEqual(with_dictionary.fiche_id, "KB0070 - Harmony ne répond plus")
        without = find(KBMap("clienta", fiches), "Harmony ne répond plus")
        self.assertEqual(without.trace[0]["ticket"], [])


class ContractTest(unittest.TestCase):
    def test_same_input_same_finding(self):
        fiches = load_decomposed_jsonl(EXAMPLES / "fiches" / "clienta.jsonl")
        text = "L'application reste bloquée au démarrage, erreur 0x80070005."
        first = find(KBMap("clienta", fiches), text).to_dict()
        self.assertEqual(find(KBMap("clienta", fiches), text).to_dict(), first)
        self.assertEqual(find(KBMap("clienta", list(reversed(fiches))), text).to_dict(), first)

    def test_config_rejects_unknown_settings(self):
        self.assertEqual(FunnelConfig.from_dict({"gap": "0.2"}).gap, 0.2)
        with self.assertRaises(ValueError):
            FunnelConfig.from_dict({"min_score": 0.1})

    def test_fiche_view_gives_the_verified_steps(self):
        kbmap = example_map()
        view = fiche_view(kbmap, "KB0030001")
        fiche = kbmap.fiches["KB0030001"]
        self.assertEqual([s["text"] for s in view["steps"]], [s.text for s in fiche.steps])
        for step in view["steps"]:
            self.assertIn(step["text"], fiche.text)

    def test_scoreboard_engine(self):
        engine = FunnelEngine({"clienta": example_map()})
        decision = engine.decide(Ticket("T-1", "clienta", "Outlook reste bloqué au démarrage, erreur 0x80070005."))
        self.assertEqual((decision.kind, decision.shown), ("fiche", "KB0030001"))
        self.assertEqual(engine.decide(Ticket("T-9", "other", "x")).error, "no KB map for client 'other'")
        self.assertEqual(engine.candidates(Ticket("T-2", "clienta", "Caméra noire dans Teams"), 2)[0][0], "KB0030003")

    def test_scoreboard_engine_with_interpretation_counts_its_tokens(self):
        llm = StubLLM({"terms": ["Outlook does not start"], "application": "Outlook"})
        engine = FunnelEngine({"clienta": example_map()}, llm=llm)
        decision = engine.decide(Ticket("T-8", "clienta", "Ma messagerie ne se lance plus"))
        self.assertEqual((decision.usage.input_tokens, decision.usage.output_tokens), (10, 5))
        self.assertIn("interpret", [step["step"] for step in decision.trace])



def cited_map() -> KBMap:
    return KBMap("clienta", filler() + [make("KB0052 - Compte verrouillé", "Déverrouillez le compte dans la console."),
                                        make("KB0053 - Autre", "Autre sujet sans rapport.")])


class MinShowTest(unittest.TestCase):
    """The calibrated floor (slice 4): under it a fiche is offered, not shown; a designated fiche is always shown."""

    TICKET = "Outlook reste bloqué au démarrage, erreur 0x80070005."

    def test_off_by_default(self):
        self.assertEqual(FunnelConfig().min_show, 0.0)
        self.assertEqual(FunnelConfig.from_dict({"min_show": "0.3"}).min_show, 0.3)

    def test_a_fiche_under_the_floor_is_offered_first_in_a_choice(self):
        shown = find(example_map(), self.TICKET)
        self.assertEqual((shown.kind, shown.fiche_id, shown.designated), ("fiche", "KB0030001", False))
        offered = find(example_map(), self.TICKET, config=FunnelConfig(min_show=shown.score + 0.01))
        self.assertEqual((offered.kind, offered.asks), ("question", "fiche"))
        self.assertTrue(offered.reason.endswith("_below_min_show"), offered.reason)
        self.assertEqual(offered.options[0]["fiche_id"], "KB0030001")
        self.assertEqual(offered.fiches[0], "KB0030001")

    def test_a_fiche_at_the_floor_is_still_shown(self):
        shown = find(example_map(), self.TICKET)
        again = find(example_map(), self.TICKET, config=FunnelConfig(min_show=shown.score))
        self.assertEqual((again.kind, again.fiche_id), ("fiche", "KB0030001"))

    def test_a_fiche_the_ticket_designates_is_shown_whatever_the_floor(self):
        finding = find(cited_map(), "L'utilisateur a suivi la KB 52 sans succès", config=FunnelConfig(min_show=0.99))
        self.assertEqual((finding.kind, finding.fiche_id, finding.designated), ("fiche", "KB0052 - Compte verrouillé", True))

    def test_the_scoreboard_sees_no_score_on_a_designated_fiche(self):
        engine = FunnelEngine({"clienta": cited_map()})
        decision = engine.decide(Ticket("T-10", "clienta", "L'utilisateur a suivi la KB 52 sans succès"))
        self.assertEqual((decision.kind, decision.score), ("fiche", None))
        scored = FunnelEngine({"clienta": example_map()}).decide(Ticket("T-1", "clienta", self.TICKET))
        self.assertIsNotNone(scored.score)


if __name__ == "__main__":
    unittest.main()
