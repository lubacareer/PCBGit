"""Exercise GitHub composite action routing without a Docker daemon."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "Composite action uses bash")
class GitHubActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n')
        docker.chmod(0o755)
        self.capture = self.root / "docker.log"
        text = (Path(__file__).resolve().parents[1] / "action.yml").read_text()
        body = text.split("      run: |\n", 1)[1]
        self.script = "\n".join(line[8:] if line.startswith("        ") else line for line in body.splitlines())
        self.env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "CAPTURE": str(self.capture),
                    "GITHUB_WORKSPACE": str(self.root), "GITHUB_ACTION_PATH": str(self.root),
                    "PCB_BASE": "base", "PCB_HEAD": "head", "PCB_OUTPUT": "report", "PCB_BOM": "",
                    "PCB_BUILD_QUANTITY": "1", "PCB_PROFILE": "", "PCB_IMAGE": "", "PCB_EVENT": "pull_request",
                    "PCB_HEAD_REPO": "fork/project", "PCB_REPO": "owner/project", "MOUSER_API_KEY": "private"}

    def run_action(self, **env):
        return subprocess.run(["bash", "-c", self.script], env={**self.env, **env}, capture_output=True, text=True, timeout=30)

    def test_fork_uses_pinned_image_and_skips_secrets(self):
        image = "registry.example/pcbgit@sha256:" + "a" * 64
        result = self.run_action(PCB_IMAGE=image)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.capture.read_text().splitlines()
        self.assertTrue(calls[0].startswith("pull " + image))
        self.assertIn("--skip-inventory", calls[1])
        self.assertNotIn("-e MOUSER_API_KEY", calls[1])
        self.assertNotIn("private", "\n".join(calls))

    def test_rejects_mutable_image_and_pull_request_target(self):
        result = self.run_action(PCB_IMAGE="registry.example/pcbgit:latest")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.capture.exists())
        result = self.run_action(PCB_EVENT="pull_request_target")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.capture.exists())

    def test_default_build_and_trusted_secret_forwarding(self):
        result = self.run_action(PCB_EVENT="push")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.capture.read_text().splitlines()
        self.assertTrue(calls[0].startswith("build "))
        self.assertIn("-e MOUSER_API_KEY", calls[1])
        self.assertNotIn("--skip-inventory", calls[1])
