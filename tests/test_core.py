import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pcbgit.review import ReviewError, changed_files, git_environment, inner_copper_layers, kicad_file, review, run_tool, schematic_roots, snapshot, write_report
from pcbgit.sexpr import _parse


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
        try:
            return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  timeout=30, env=git_environment()).stdout
        except subprocess.TimeoutExpired:
            self.fail("Test Git command timed out")

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
                                 stderr=subprocess.PIPE, text=True, check=True,
                                 timeout=30, env=git_environment())
        self.git("update-index", "--add", "--cacheinfo", f"120000,{process.stdout.strip()},escape")
        self.git("commit", "-m", "add symlink")
        with self.assertRaises(ReviewError):
            snapshot(self.repo, "HEAD", self.root / "snapshot")

    def test_snapshot_rejects_gitlink_as_unsupported(self):
        oid = self.git("rev-parse", "HEAD").strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},submodule")
        self.git("commit", "-m", "add gitlink")
        with self.assertRaisesRegex(ReviewError, "Unsafe or unsupported repository entry: submodule"):
            snapshot(self.repo, "HEAD", self.root / "snapshot")

    def test_git_snapshot_ignores_inherited_repository_overrides(self):
        overrides = {key: str(self.root / "does-not-exist") for key in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")}
        with patch.dict("os.environ", overrides):
            snapshot(self.repo, "HEAD", self.root / "snapshot")
        self.assertEqual((self.root / "snapshot" / "board.kicad_pcb").read_text(), "after")

    def test_test_helper_reports_git_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaisesRegex(AssertionError, "timed out"):
                self.git("status")

    def test_snapshot_rejects_preexisting_destination_symlink(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "board.txt").write_text("inside")
        self.git("add", "nested/board.txt")
        self.git("commit", "-m", "add nested file")
        destination = self.root / "snapshot"
        destination.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (destination / "nested").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"OS does not permit creating test symlinks: {exc}")
        with self.assertRaises(ReviewError):
            snapshot(self.repo, "HEAD", destination)
        self.assertFalse((outside / "board.txt").exists())

    def test_snapshot_checks_resolved_target_containment(self):
        destination = self.root / "snapshot"
        original = Path.resolve
        def redirect(path, *args, **kwargs):
            if path == destination / "board.kicad_pcb":
                return self.root / "outside.kicad_pcb"
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirect):
            with self.assertRaisesRegex(ReviewError, "escapes destination"):
                snapshot(self.repo, "HEAD", destination)
        self.assertFalse((self.root / "outside.kicad_pcb").exists())

    def test_kicad_artifacts_are_never_reused_between_runs(self):
        output = self.root / "report"
        (self.repo / "board.kicad_pcb").write_text('(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))')
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


    def test_sexpression_decodes_kicad_escaped_utf8(self):
        self.assertEqual(_parse(r'(root "a\"b" "c\\d" "\q" "\n\r\t" "\xc3\xa9" "\303\251")'),
                         ["root", 'a"b', 'c\\d', '\\q', '\n\r\t', chr(0xe9), chr(0xe9)])
        with self.assertRaisesRegex(ValueError, "invalid UTF-8"):
            _parse(r'(root "\xff")')

    def test_hierarchy_resolves_escaped_filenames_but_rejects_escaping_path(self):
        root = self.root / "escaped"
        root.mkdir()
        (root / "child.kicad_sch").write_text('(kicad_sch)', encoding="utf-8")
        (root / "main.kicad_sch").write_text(
            r'(kicad_sch (sheet (property "Sheetfile" "\x63hild.kicad_sch")))', encoding="utf-8")
        self.assertEqual(schematic_roots(root), ({"main.kicad_sch"}, []))
        (root / "main.kicad_sch").write_text(
            r'(kicad_sch (sheet (property "Sheetfile" "\x2e\x2e/outside.kicad_sch")))', encoding="utf-8")
        self.assertEqual(schematic_roots(root)[1][0]["status"], "unknown")

    def test_inner_copper_layers_are_exported_individually(self):
        board = self.root / "multi.kicad_pcb"
        board.write_text('(kicad_pcb (layers (0 "F.Cu" signal) (4 "In1.Cu" power) '
                         '(6 "In2.Cu" signal "Power Plane") (31 "B.Cu" signal)))', encoding="utf-8")
        self.assertEqual(inner_copper_layers(board), ["In1.Cu", "In2.Cu"])
        # The numeric ordinal is not necessarily the copper layer index.
        board.write_text('(kicad_pcb (layers (1 "B.SilkS" user) (4 "In1.Cu" power "GND") '
                         '(6 "In2.Cu" signal "Power Plane") (31 "B.Cu" signal)))', encoding="utf-8")
        self.assertEqual(inner_copper_layers(board), ["In1.Cu", "In2.Cu"])
        calls = []
        def fake_tool(args):
            calls.append(args)
            Path(args[args.index("--output") + 1]).write_text("<svg/>", encoding="utf-8")
            return 0, ""
        with patch("pcbgit.review.run_tool", side_effect=fake_tool):
            result = kicad_file(board, board.name, self.root / "report", "head", False)
        self.assertEqual(len(result["artifacts"]), 3)
        self.assertEqual([call[call.index("--layers") + 1] for call in calls],
                         ["F.Cu,B.Cu,Edge.Cuts,F.Silkscreen,B.Silkscreen",
                          "In1.Cu,Edge.Cuts", "In2.Cu,Edge.Cuts"])
        self.assertFalse(result["findings"])
        report = {"status": "pass", "base": "a", "head": "b", "files": [{"path": board.name, "sides": [result]}]}
        write_report(report, self.root / "report")
        self.assertIn("layer-In1.Cu.svg", (self.root / "report" / "index.html").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("kicad-cli"), "KiCad CLI is not installed")
    def test_real_kicad_exports_inner_copper_layers(self):
        board = Path(__file__).parent / "fixtures" / "four-layer.kicad_pcb"
        result = kicad_file(board, board.name, self.root / "report", "head", False)
        self.assertEqual(result["findings"], [])
        self.assertEqual({Path(path).name for path in result["artifacts"]},
                         {"board.svg", "layer-In1.Cu.svg", "layer-In2.Cu.svg"})
        self.assertTrue(all((self.root / "report" / path).stat().st_size > 0 for path in result["artifacts"]))

    def test_inner_layer_parser_and_export_failure_report_unknown(self):
        board = self.root / "multi.kicad_pcb"
        board.write_text('(kicad_pcb (layers (4 "In1.Cu" power)))', encoding="utf-8")
        def fake_tool(args):
            if "In1.Cu,Edge.Cuts" in args:
                return 1, "failure"
            Path(args[args.index("--output") + 1]).write_text("<svg/>", encoding="utf-8")
            return 0, ""
        with patch("pcbgit.review.run_tool", side_effect=fake_tool):
            result = kicad_file(board, board.name, self.root / "report", "head", False)
        self.assertEqual(result["findings"][0]["status"], "unknown")
        self.assertIn("In1.Cu", result["findings"][0]["message"])
        board.write_text('(kicad_pcb)')
        with patch("pcbgit.review.run_tool", side_effect=fake_tool):
            result = kicad_file(board, board.name, self.root / "report2", "head", False)
        self.assertEqual(result["findings"][0]["status"], "unknown")

    def test_changed_child_triggers_root_check_only(self):
        (self.repo / "root.kicad_sch").write_text(
            '(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))', encoding="utf-8")
        child = self.repo / "child.kicad_sch"
        child.write_text('(kicad_sch)', encoding="utf-8")
        self.git("add", "root.kicad_sch", "child.kicad_sch")
        self.git("commit", "-m", "add hierarchy")
        base = self.git("rev-parse", "HEAD").strip()
        child.write_text('(kicad_sch (text "changed"))', encoding="utf-8")
        self.git("commit", "-am", "change child")
        def fake_kicad(path, name, output, side, check):
            return {"side": side, "artifacts": [], "findings": []}
        with patch("pcbgit.review.run_tool", return_value=(0, "9.0.9")), \
             patch("pcbgit.review.kicad_file", side_effect=fake_kicad) as called:
            result = review(self.repo, base, "HEAD", self.root / "report")
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
            result = review(self.repo, self.base, "HEAD", self.root / "report")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(json.loads((self.root / "report" / "report.json").read_text())["head"], result["head"])

    def test_report_escapes_untrusted_text(self):
        result = {"status": "unknown", "base": "a", "head": "b", "findings": [{"status": "unknown", "message": "<bad hierarchy>"}], "files": [{"path": "<script>", "sides": []}]}
        write_report(result, self.root / "report")
        page = (self.root / "report" / "index.html").read_text()
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;bad hierarchy&gt;", page)
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
