import unittest

from kecore.llm import LLMError
from kefind.interpret import interpret

from .helpers import StubLLM


class InterpretTest(unittest.TestCase):
    def test_terms_are_checked_by_code(self):
        llm = StubLLM({"terms": ["locked account", "Locked Account", "unlock account", "error 0x80070005",
                                 "call 06 12 34 56 78", "one two three four five six seven", "compte verrouillé"],
                       "application": "Active Directory"})
        result = interpret(llm, "Mon compte est bloqué depuis ce matin")
        self.assertEqual(result.terms, ["Active Directory", "locked account", "unlock account", "compte verrouillé"])
        self.assertEqual(result.application, "Active Directory")
        reasons = {d["term"]: d["reason"] for d in result.dropped}
        self.assertEqual(reasons["error 0x80070005"], "adds err:0x80070005")
        self.assertEqual(reasons["call 06 12 34 56 78"], "contact detail")
        self.assertEqual(reasons["one two three four five six seven"], "too long")
        self.assertNotIn("Locked Account", reasons)  # a repeat is skipped, not refused

    def test_an_entity_the_ticket_names_may_be_repeated(self):
        llm = StubLLM({"terms": ["Outlook error 0x80070005"], "application": None})
        result = interpret(llm, "Outlook affiche l'erreur 0x80070005")
        self.assertEqual(result.terms, ["Outlook error 0x80070005"])

    def test_at_most_twelve_terms(self):
        llm = StubLLM({"terms": [f"term {i}" for i in range(15)], "application": None})
        result = interpret(llm, "x")
        self.assertEqual(len(result.terms), 12)
        self.assertEqual(len(result.dropped), 3)

    def test_without_llm_or_when_it_fails_there_is_no_term(self):
        self.assertEqual(interpret(None, "x").terms, [])
        failed = interpret(StubLLM(LLMError("quota")), "x")
        self.assertEqual((failed.terms, failed.error), ([], "quota"))


if __name__ == "__main__":
    unittest.main()
