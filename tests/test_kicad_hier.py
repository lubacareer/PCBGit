"""Exercise automatic hierarchical BOM export with real KiCad 9."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pcbgit.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "kicad_hier"


@unittest.skipUnless(shutil.which("kicad-cli"), "KiCad CLI is not installed")
class KiCadHierarchicalBOMTest(unittest.TestCase):
    def test_real_hierarchical_bom_passes_strict_stock_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, output = root / "repo", root / "report"
            repo.mkdir()

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                      capture_output=True, text=True, timeout=30).stdout.strip()

            git("init")
            git("config", "user.name", "PCBGit fixture")
            git("config", "user.email", "fixtures@example.invalid")
            for file in FIXTURES.iterdir():
                shutil.copy(file, repo / file.name)
            git("add", ".")
            git("commit", "-m", "add hierarchical schematic")

            with patch("pcbgit.inventory._digikey", return_value=(3, "fixture")) as digikey, \
                 patch("pcbgit.inventory._mouser", return_value=(3, "fixture")) as mouser:
                code = main(["review", "--repo", str(repo), "--base", ":empty:",
                             "--head", "HEAD", "--output", str(output), "--build-quantity", "2"])
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0, report)
            self.assertEqual(report["inventory"]["status"], "pass", report)
            parts = report["inventory"]["findings"]
            self.assertEqual(len(parts), 1, parts)
            self.assertEqual(parts[0]["reference"], "U1")
            self.assertEqual(parts[0]["quantity"], 2)
            self.assertEqual(parts[0]["status"], "in_stock")
            digikey.assert_called_once_with("TEST-DK-1")
            mouser.assert_called_once_with("TEST-MS-1")


if __name__ == "__main__":
    unittest.main()
