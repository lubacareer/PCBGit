import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pcbgit.review import ReviewError, changed_files, kicad_file, review, run_tool, schematic_roots, snapshot, write_report


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

    def test_git_snapshot_ignores_inherited_repository_overrides(self):
        overrides = {key: str(self.root / "does-not-exist") for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")}
        with patch.dict("os.environ", overrides):
            snapshot(self.repo, "HEAD", self.root / "snapshot")
        self.assertEqual((self.root / "snapshot" / "board.kicad_pcb").read_text(), "after")

    def test_kicad_artifacts_are_never_reused_between_runs(self):
        output = self.root / "report"
        def first_run(args):
            target = Path(args[args.index("--output") + 1])
            target.write_text("<svg/>" if args[3] == "svg" else "{}", encoding="utf-8")
            return 0, ""
        with patch("pcbgit.review.run_tool", side_effect=first_run):
            first = kicad_file(self.repo / "board.kicad_pcb", "board.kicad_pcb", output, "head", True)
        self.assertEqual(len(first["artifacts"]), 2)
        with patch("pcbgit.review.run_tool", return_value=(0, "")):
            second = kicad_file(self.repo / "board.kicad_pcb", "board.kicad_pcb", output, "head", True)
        self.assertFalse(second["artifacts"])
        self.assertEqual([item["status"] for item in second["findings"]], ["unknown", "unknown"])

    def test_hierarchical_schematic_checks_only_root_but_renders_children(self):
        (self.repo / "root.kicad_sch").write_text(
            '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))', encoding="utf-8")
        (self.repo / "child.kicad_sch").write_text('(kicad_sch)', encoding="utf-8")
        self.git("add", "root.kicad_sch", "child.kicad_sch")
        self.git("commit", "-m", "add hierarchy")
        def fake_kicad(path, name, output, side, check):
            return {"side": side, "artifacts": [], "findings": []}
        with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), \
             patch("pcbgit.review.kicad_file", side_effect=fake_kicad) as called, \
             patch("pcbgit.review.check_board", return_value={"status": "partial", "profile": None, "supported": [], "unsupported": [], "findings": []}):
            result = review(self.repo, self.base, "HEAD", self.root / "report", skip_inventory=True)
        calls = {c.args[1]: c.kwargs["check"] for c in called.call_args_list if c.args[3] == "head"}
        self.assertTrue(calls["root.kicad_sch"])
        self.assertFalse(calls["child.kicad_sch"])
        self.assertEqual(result["status"], "pass")

    def test_hierarchy_missing_or_cyclic_files_are_unknown(self):
        root = self.root / "schematics"
        root.mkdir()
        (root / "main.kicad_sch").write_text('(kicad_sch (sheet (property "Sheetfile" "missing.kicad_sch")))')
        roots, findings = schematic_roots(root)
        self.assertEqual(roots, set())
        self.assertEqual(findings[0]["status"], "unknown")
        (root / "missing.kicad_sch").write_text('(kicad_sch (sheet (property "Sheet file" "main.kicad_sch")))')
        roots, findings = schematic_roots(root)
        self.assertEqual(roots, set())
        self.assertIn("cycle", findings[0]["message"])
        (root / "missing.kicad_sch").write_text('(kicad_sch (sheet')
        roots, findings = schematic_roots(root)
        self.assertEqual(roots, set())
        self.assertEqual(findings[0]["status"], "unknown")

    def test_missing_kicad_is_not_a_pass(self):
        with patch("pcbgit.review.run_tool", return_value=(None, "not installed")):
            result = review(self.repo, self.base, "HEAD", self.root / "report", skip_inventory=True)
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

    def test_inventory_uses_committed_bom_and_fails_closed(self):
        bom = self.repo / "bom.csv"
        bom.write_text("Reference,Qty,DigiKey,Mouser\nR1,2,DK1,\n")
        self.git("add", "bom.csv")
        self.git("commit", "-m", "add bom")
        bom.write_text("Reference,Qty,DigiKey,Mouser\nR1,2,,\n")
        with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), patch("pcbgit.review.kicad_file", return_value={"side": "head", "artifacts": [], "findings": []}), patch("pcbgit.review.check_board", return_value={"status": "partial", "profile": None, "supported": [], "unsupported": [], "findings": []}), patch("pcbgit.inventory._digikey", return_value=(4, "exact")):
            result = review(self.repo, self.base, "HEAD", self.root / "report", bom="bom.csv", build_quantity=2)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["inventory"]["findings"][0]["quantity"], 4)
            missing = review(self.repo, self.base, "HEAD", self.root / "report2", bom="missing.csv")
            self.assertEqual(missing["status"], "fail")
            skipped = review(self.repo, self.base, "HEAD", self.root / "report3", skip_inventory=True)
            self.assertEqual(skipped["inventory"]["status"], "skipped")

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
