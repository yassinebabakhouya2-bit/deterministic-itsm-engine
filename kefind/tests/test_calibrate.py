"""Calibration on the KB itself (kefind.calibrate): exam questions written from each fiche, never
indexed; thresholds chosen on one half, measured on the other; byte-identical on replay."""

import json
import unittest

from kecore.llm import LLMError
from kefind import calibrate as cal
from kefind import semantic as sem

from .semantic_helpers import ConceptEmbedder, TitleLLM
from .test_semantic import build_map


def samples(n, first, second, top="a", expected="a", loo=((0.5, "x"), (0.45, "y")), split="calibration"):
    return [cal.Sample(expected, split, [(first, top), (second, "z")], False, False, list(loo)) for _ in range(n)]


class SplitAndNoiseTest(unittest.TestCase):
    def test_the_split_is_stable_and_roughly_even(self):
        ids = [f"KB{n:04d}" for n in range(200)]
        self.assertEqual([cal.split_of(i) for i in ids], [cal.split_of(i) for i in ids])
        self.assertTrue(70 < sum(cal.split_of(i) == "test" for i in ids) < 130)

    def test_noise_is_seeded_by_its_key(self):
        text = "mon téléphone professionnel ne sonne plus du tout"
        outputs = {cal.noise(text, f"key{n}") for n in range(40)}
        self.assertEqual(cal.noise(text, "key1"), cal.noise(text, "key1"))
        self.assertGreater(len(outputs), 1)
        for out in outputs:
            self.assertEqual(len(out.split(" ")), len(text.split(" ")))
            self.assertEqual(out[0], "m")


class HeldoutTest(unittest.TestCase):
    def test_exam_questions_are_checked_and_noised(self):
        kbmap, _ = build_map()
        llm = TitleLLM({}, exam={"Associate a phone line": {"messages": [
            "le nouveau n'a pas de numéro pour appeler",
            "associate phone line please",  # reuses the title
            "erreur 0x80070005 sur teams",  # invented error code
            "ok",  # too short
        ]}})
        heldout = cal.heldout_for(llm, kbmap, "KB0233 - Associate a phone line")
        self.assertEqual(len(heldout.queries), 1)
        self.assertEqual(sorted(d["reason"] for d in heldout.dropped),
                         ["adds err:0x80070005", "reuses the title", "too short"])
        self.assertEqual(heldout.split, cal.split_of("KB0233 - Associate a phone line"))
        self.assertEqual(cal.Heldout.from_dict(heldout.to_dict()), heldout)

    def test_a_model_failure_is_no_question_for_that_fiche(self):
        kbmap, _ = build_map()
        llm = TitleLLM({}, exam={"Associate a phone line": LLMError("timeout")})
        heldout = cal.heldout_for(llm, kbmap, "KB0233 - Associate a phone line")
        self.assertEqual((heldout.queries, "timeout" in heldout.error), ([], True))


class ChooseTest(unittest.TestCase):
    def test_the_thresholds_show_the_clear_leads_and_never_the_close_wrong_ones(self):
        data = samples(120, 0.9, 0.5) + samples(30, 0.62, 0.6, top="b")
        th, search = cal.choose(data)
        self.assertTrue(search["feasible"])
        measured = cal.measure(data, th)
        self.assertEqual((measured["right_shown"]["k"], measured["wrong_shown"]["k"]), (120, 0))
        self.assertTrue(th.source.startswith("calibrated"))

    def test_a_tie_never_gets_a_zero_margin(self):
        self.assertGreater(min(cal.MARGINS), 0)
        data = samples(300, 0.83, 0.83) + samples(300, 0.9, 0.5)
        th, _ = cal.choose(data)
        self.assertGreater(th.margin, 0)
        self.assertEqual(cal.measure(samples(5, 0.83, 0.83), th)["right_shown"]["k"], 0)

    def test_no_exam_offers_like_an_uncalibrated_index_instead_of_abstaining_on_everything(self):
        th, search = cal.choose([])
        self.assertEqual((th.floor, th.offer), (cal.NEVER, sem.UNCALIBRATED.offer))
        self.assertFalse(search["feasible"])

    def test_too_few_questions_never_show_alone(self):
        th, search = cal.choose(samples(10, 0.9, 0.5))  # 0 error out of 10 still allows ~28% at the top
        self.assertFalse(search["feasible"])
        self.assertEqual(th.floor, cal.NEVER)

    def test_a_fiche_shown_when_the_right_one_is_absent_counts_against_the_thresholds(self):
        # removing the expected fiche leaves a clear leader at 0.85: showing at 0.85 would show it
        data = samples(150, 0.9, 0.5, loo=((0.85, "x"), (0.3, "y")))
        th, _ = cal.choose(data)
        self.assertLessEqual(cal.measure(data, th)["loo_shown"]["rate"], cal.MAX_LOO_SHOWN)
        self.assertGreater(th.floor, 0.85)


class WithholdTest(unittest.TestCase):
    CHOSEN = sem.Thresholds(0.7, 0.05, 0.35, "calibrated on 300 KB questions")

    def test_thresholds_that_fail_a_safety_check_on_the_test_half_show_nothing_alone(self):
        checks = {name: True for name in cal.TARGETS}
        self.assertIs(cal.withhold(self.CHOSEN, checks), self.CHOSEN)
        for failed in cal.SAFETY_CHECKS:
            th = cal.withhold(self.CHOSEN, {**checks, failed: False})
            self.assertEqual((th.floor, th.margin, th.offer), (cal.NEVER, 0.05, 0.35))
            self.assertIn(failed, th.source)
            # offers and abstentions are unchanged: the person still gets the closest fiches
            self.assertEqual(sem.decide([(0.95, "a", 0), (0.2, "b", 0)], th), "offer")

    def test_usefulness_targets_missed_do_not_withhold(self):
        checks = {name: True for name in cal.TARGETS}
        self.assertIs(cal.withhold(self.CHOSEN, {**checks, "right_shown_min": False, "questions_max": False}),
                      self.CHOSEN)


class CalibrateTest(unittest.TestCase):
    def exam(self):
        return {
            "Associate a phone line": {"messages": ["le nouveau n'a pas de numéro pour appeler",
                                                    "besoin d'un numéro teams pour un arrivant"]},
            "Transfert d'appels TEAMS": {"messages": ["je veux que mes appels aillent sur mon portable",
                                                      "rediriger les appels quand je suis absent"]},
            "LOCKED ACCOUNT": {"messages": ["impossible de me connecter mon compte est bloqué",
                                            "compte verrouillé ce matin"]},
        }

    def run_once(self):
        kbmap, _ = build_map(calibrated=False)
        llm = TitleLLM({}, exam=self.exam())
        heldouts = [cal.heldout_for(llm, kbmap, f) for f in kbmap.ranked]
        return kbmap, cal.calibrate(kbmap.semantic, kbmap, heldouts, ConceptEmbedder())

    def test_the_calibration_names_its_index_and_replays_byte_for_byte(self):
        kbmap, first = self.run_once()
        _, again = self.run_once()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(again, sort_keys=True))
        self.assertEqual(first["index_sha256"], kbmap.semantic.sha256)
        self.assertEqual(first["exam"]["questions"], 6)
        self.assertIn("not a measure on real tickets", first["note"])
        # 6 questions cannot bound the error rate: honest outcome, never shown alone, acceptance failed
        self.assertFalse(first["feasible"])
        self.assertFalse(first["acceptance"]["passed"])
        self.assertEqual(first["thresholds"], first["chosen"])  # nothing to withhold: it never shows anyway
        self.assertFalse(first["withheld"])
        blobs = kbmap.semantic.to_blobs()
        loaded = sem.SemanticIndex.from_blobs(blobs[sem.INDEX_BLOB], blobs[sem.VECTORS_BLOB],
                                              json.dumps(first).encode())
        self.assertEqual(loaded.thresholds().floor, cal.NEVER)


if __name__ == "__main__":
    unittest.main()
