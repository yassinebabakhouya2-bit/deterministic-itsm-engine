"""The two seams that let kecore run in Azure without changing its results.

- documents are read from bytes, in an OS-independent order (fiches_from_documents);
- the LLM record is a store (a folder locally, a blob container in Azure) with one layout.
"""

import unittest

from kecore.fiches import document_sort_key, fiches_from_documents, load_folder, read_document, read_document_bytes
from kecore.llm import FileRecordStore, LLMError, RecordingLLM

from .helpers import DEMO_KB, FakeLLM, TempDirTestCase, step


class MemoryStore:
    def __init__(self):
        self.items: dict[str, str] = {}

    def read(self, relative):
        return self.items.get(relative)

    def write(self, relative, text):
        self.items[relative] = text


class DocumentsTest(TempDirTestCase):
    def test_bytes_and_path_give_the_same_text(self):
        for path in sorted(DEMO_KB.rglob("*")):
            if path.is_file():
                self.assertEqual(read_document_bytes(path.name, path.read_bytes()), read_document(path), path.name)

    def test_order_is_case_insensitive_and_separator_agnostic(self):
        names = ["b.md", "A.md", "a2.md", "sub\\C.md", "sub/b.md"]
        self.assertEqual(sorted(names, key=document_sort_key), ["A.md", "a2.md", "b.md", "sub/b.md", "sub\\C.md"])

    def test_documents_from_memory_match_the_folder(self):
        from_folder, warnings_folder = load_folder(DEMO_KB, "clienta")
        documents = []
        for path in DEMO_KB.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                documents.append((str(path.relative_to(DEMO_KB)), lambda data=data: data))
        documents.reverse()  # the input order must not matter
        from_memory, warnings_memory = fiches_from_documents(documents, "clienta")
        self.assertEqual([f.to_dict() for f in from_memory], [f.to_dict() for f in from_folder])
        self.assertEqual(warnings_memory, warnings_folder)

    def test_first_file_wins_a_duplicate_id_in_case_insensitive_order(self):
        # A case-sensitive sort would put "B" before "a"; Windows, and now every OS, reads "a" first.
        documents = [
            ("KB0012345 B.md", lambda: b"# Titre B\n\nTexte B."),
            ("KB0012345 a.md", lambda: b"# Titre A\n\nTexte A."),
        ]
        fiches, warnings = fiches_from_documents(documents, "c")
        self.assertEqual([f.source for f in fiches], ["KB0012345 a.md"])
        self.assertIn("same fiche id", warnings[0])


class RecordStoreTest(TempDirTestCase):
    def test_any_store_replays_what_another_recorded(self):
        inner = FakeLLM({"T": {"sections": [], "steps": [step("Fermez Outlook.")]}})
        on_disk = RecordingLLM(inner, self.path("cache"))
        first = on_disk.complete_json("s", "Article title: T\n", {}, "fiche_steps")

        memory = MemoryStore()
        disk = FileRecordStore(self.path("cache"))
        for path in self.path("cache").rglob("*.json"):
            relative = path.relative_to(self.path("cache")).as_posix()
            memory.write(relative, disk.read(relative))

        replay = RecordingLLM(None, mode="replay", model_id="fake-model@test", store=memory)
        self.assertEqual(replay.complete_json("s", "Article title: T\n", {}, "fiche_steps").data, first.data)
        with self.assertRaises(LLMError):
            replay.complete_json("s", "another request", {}, "fiche_steps")

    def test_record_mode_writes_through_the_store(self):
        memory = MemoryStore()
        recorder = RecordingLLM(FakeLLM({}), store=memory)
        recorder.complete_json("s", "u", {}, "x")
        self.assertEqual(len(memory.items), 1)
        relative = next(iter(memory.items))
        self.assertRegex(relative, r"^[0-9a-f]{2}/[0-9a-f]{64}\.json$")

    def test_a_store_or_a_folder_is_required(self):
        with self.assertRaises(ValueError):
            RecordingLLM(None, mode="replay")


if __name__ == "__main__":
    unittest.main()
