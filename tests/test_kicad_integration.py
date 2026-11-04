"""Integration smoke test using real KiCad 9; CI must assert KiCad is present."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pcbgit.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "kicad"


@unittest.skipUnless(shutil.which("kicad-cli"), "KiCad CLI is not installed")
class KiCadIntegrationTest(unittest.TestCase):
    def test_real_render_and_checks(self):
        version = subprocess.run(["kicad-cli", "version"], capture_output=True, text=True, check=True)
        self.assertTrue(version.stdout.strip().startswith("9."), version.stdout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            output = root / "report"
            repo.mkdir()

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()

            git("init")
            git("config", "user.email", "fixtures@example.invalid")
            git("config", "user.name", "PCBGit fixture")
            for file in (FIXTURES / "base").iterdir():
                shutil.copy(file, repo / file.name)
            git("add", ".")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            for file in (FIXTURES / "head").iterdir():
                shutil.copy(file, repo / file.name)
            git("add", ".")
            git("commit", "-m", "change")
            code = main(["review", "--repo", str(repo), "--base", base,
                         "--head", "HEAD", "--output", str(output), "--skip-inventory"])
            result = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "pass", result)
            self.assertEqual(result["inventory"]["status"], "skipped")
            self.assertTrue((output / "index.html").is_file())
            self.assertEqual(len(result["files"]), 2)
            for item in result["files"]:
                sides = {side["side"]: side for side in item["sides"]}
                self.assertIn("base", sides)
                self.assertIn("head", sides)
                for name in ("base", "head"):
                    svg = [output / path for path in sides[name]["artifacts"] if path.endswith(".svg")]
                    self.assertTrue(svg, item)
                    self.assertTrue(all(file.is_file() and file.stat().st_size for file in svg))
                expected = "DRC" if item["path"].endswith(".kicad_pcb") else "ERC"
                check = [finding for finding in sides["head"]["findings"] if expected in finding["message"]]
                self.assertEqual(len(check), 1, item)
                self.assertEqual(check[0]["status"], "pass", check)
                self.assertIsInstance(check[0]["details"], dict)
                self.assertTrue(any((output / path).is_file() and path.endswith(".json") for path in sides["head"]["artifacts"]))
            # This schematic has no populated components. The default strict
            # gate must run and return unknown rather than silently passing.
            default_output = root / "default-report"
            code = main(["review", "--repo", str(repo), "--base", base,
                         "--head", "HEAD", "--output", str(default_output)])
            default = json.loads((default_output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 2, default)
            self.assertEqual(default["inventory"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
