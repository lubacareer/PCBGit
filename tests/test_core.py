import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pcbgit.review import ReviewError, changed_files, review, run_tool, snapshot, write_report


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

    def test_snapshot_does_not_honor_export_ignore(self):
        (self.repo / ".gitattributes").write_text("board.kicad_pcb export-ignore\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "attempt to hide board")
        snapshot(self.repo, "HEAD", self.root / "snapshot")
        self.assertEqual((self.root / "snapshot" / "board.kicad_pcb").read_text(), "after")

    def test_snapshot_rejects_symlink_objects(self):
        process = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                                 input="../outside", stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, check=True)
        self.git("update-index", "--add", "--cacheinfo", f"120000,{process.stdout.strip()},escape")
        self.git("commit", "-m", "add symlink")
        with self.assertRaises(ReviewError):
            snapshot(self.repo, "HEAD", self.root / "snapshot")

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

    def test_cad_subprocess_does_not_inherit_distributor_credentials(self):
        secrets = {key: "private" for key in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
                   "DIGIKEY_ACCESS_TOKEN", "DIGIKEY_ACCOUNT_ID", "MOUSER_API_KEY")}
        with patch.dict("os.environ", secrets), patch("pcbgit.review.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["kicad-cli"], 0, "ok")
            self.assertEqual(run_tool(["kicad-cli", "version"]), (0, "ok"))
            environment = run.call_args.kwargs["env"]
        self.assertTrue(all(key not in environment for key in secrets))


if __name__ == "__main__":
    unittest.main()
