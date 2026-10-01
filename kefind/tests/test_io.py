import shutil
import tempfile
import unittest
from pathlib import Path

from kecore.errors import InputError

from kefind.io import load_decomposed_jsonl, save_decomposed_jsonl

from .helpers import decompose_fiche


class IoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="kefind-test-"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_round_trip(self):
        fiche = decompose_fiche("KB001", "clienta", "Outlook ne démarre plus",
                                 "## Résolution\n1. Fermez Outlook.\n2. Relancez Outlook.\n")
        path = self._tmp / "fiches.jsonl"
        save_decomposed_jsonl(path, [fiche])
        loaded = load_decomposed_jsonl(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].to_dict(), fiche.to_dict())

    def test_missing_file_raises_input_error(self):
        with self.assertRaises(InputError):
            load_decomposed_jsonl(self._tmp / "missing.jsonl")

    def test_invalid_line_raises_input_error(self):
        path = self._tmp / "bad.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        with self.assertRaises(InputError):
            load_decomposed_jsonl(path)

    def test_blank_lines_are_skipped(self):
        fiche = decompose_fiche("KB001", "clienta", "Titre", "## Résolution\n1. Faites ceci.\n")
        path = self._tmp / "fiches.jsonl"
        save_decomposed_jsonl(path, [fiche])
        path.write_text("\n" + path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        loaded = load_decomposed_jsonl(path)
        self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
