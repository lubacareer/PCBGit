import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pcbgit.review import ReviewError, changed_files, review, snapshot, write_report


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "board.kicad_pcb").write_text("before")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "board.kicad_pcb").write_text("after")
        self.git("commit", "-am", "head")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout

    def test_git_snapshots_and_comparison(self):
        self.assertEqual(changed_files(self.repo, self.base, "HEAD"), ["board.kicad_pcb"])
        snapshot(self.repo, self.base, self.root / "snapshot")
        self.assertEqual((self.root / "snapshot" / "board.kicad_pcb").read_text(), "before")

    def test_missing_kicad_is_not_a_pass(self):
        with patch("pcbgit.review.run_tool", return_value=(None, "not installed")):
            result = review(self.repo, self.base, "HEAD", self.root / "report")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(json.loads((self.root / "report" / "report.json").read_text())["head"], result["head"])

    def test_report_escapes_untrusted_text(self):
        result = {"status": "unknown", "base": "a", "head": "b", "files": [{"path": "<script>", "sides": []}]}
        write_report(result, self.root / "report")
        page = (self.root / "report" / "index.html").read_text()
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_invalid_ref_fails(self):
        with self.assertRaises(ReviewError):
            snapshot(self.repo, "does-not-exist", self.root / "snapshot")


if __name__ == "__main__":
    unittest.main()
