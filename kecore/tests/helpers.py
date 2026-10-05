import shutil
import tempfile
import unittest
from pathlib import Path

from kecore.llm import LLMResult, LLMUsage

DEMO_KB = Path(__file__).resolve().parent.parent / "examples" / "kb-demo"


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="kecore-test-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def path(self, name: str) -> Path:
        return self._tmp / name

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path


class FakeLLM:
    """Answers by article title; '__headings__' answers the heading-role request.

    An answer can be a single dict (returned every time, e.g. a model that agrees with
    itself), or a list of dicts consumed in order across successive calls for the same
    title (to script a self-check pass that disagrees with the first) — the last one
    repeats once the list is exhausted.
    """

    model_id = "fake-model@test"

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []
        self._title_calls: dict[str, int] = {}

    def complete_json(self, system, user, schema, schema_name, *, temperature=None, seed=None):
        self.calls.append(schema_name)
        if schema_name == "heading_roles":
            data = self.answers.get("__headings__", {"mappings": []})
        elif schema_name == "software_dictionary":
            data = self.answers.get("__dictionary__", {"products": []})
        else:
            title = user.split("\n", 1)[0][len("Article title: "):]
            entry = self.answers.get(title, {"sections": [], "steps": []})
            if isinstance(entry, list):
                index = self._title_calls.get(title, 0)
                data = entry[min(index, len(entry) - 1)]
                self._title_calls[title] = index + 1
            else:
                data = entry
        if isinstance(data, Exception):
            raise data
        return LLMResult(data, LLMUsage(1000, 200), "fake-model")


def step(quote, kind="action", role="resolution", condition=None, on_failure=None, instruction=None):
    return {
        "quote": quote,
        "kind": kind,
        "section_role": role,
        "condition": condition,
        "on_failure": on_failure,
        "instruction": instruction,
    }


OUTLOOK_ANSWER = {
    "sections": [
        {"heading": "Symptôme", "role": "symptom"},
        {"heading": "Cause", "role": "cause"},
        {"heading": "Résolution", "role": "resolution"},
    ],
    "steps": [
        step("Fermez Outlook."),
        # backticks dropped by the model, and a rewording that adds a shortcut
        step(
            "Lancez Outlook en mode sans échec avec la commande outlook.exe /safe.",
            instruction="Appuyez sur Win+R et tapez outlook.exe /safe",
        ),
        step(
            "Si Outlook démarre en mode sans échec, allez dans Fichier > Options > Compléments et désactivez les compléments COM.",
            condition="Si Outlook démarre en mode sans échec",
            on_failure="Si le problème persiste, supprimez le fichier .ost",
        ),
        step(
            "Si le problème persiste, supprimez le fichier .ost dans %LOCALAPPDATA%\\Microsoft\\Outlook puis relancez Outlook.",
            condition="Si le problème persiste",
            instruction="Supprimez le fichier .ost puis relancez Outlook",
        ),
    ],
}
