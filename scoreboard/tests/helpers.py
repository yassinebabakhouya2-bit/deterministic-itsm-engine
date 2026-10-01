import shutil
import tempfile
import unittest
from pathlib import Path


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="scoreboard-test-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def path(self, name: str) -> Path:
        return self._tmp / name

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def record(ticket_id, expected, kind="fiche", fiches=(), score=None, run=0, client="a", engine="e",
           latency=0.1, usage=None, error=None):
    return {
        "engine": engine,
        "run": run,
        "ticket_id": ticket_id,
        "client": client,
        "expected": list(expected),
        "kind": kind,
        "fiches": list(fiches),
        "score": score,
        "latency_s": latency,
        "usage": usage or {},
        "error": error,
    }
