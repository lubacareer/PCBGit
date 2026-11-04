"""Exercise the published GitLab template against real Git histories."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "GitLab template requires a Unix shell")
class GitLabTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "f").write_text("1")
        self.git("add", "f")
        self.git("commit", "-m", "one")
        self.first = self.git("rev-parse", "HEAD")
        for value in ("2", "3"):
            (self.repo / "f").write_text(value)
            self.git("commit", "-am", value)
        self.head = self.git("rev-parse", "HEAD")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "pcbgit"
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\nprintf "%s" "${MOUSER_API_KEY-unset}" > "$CAPTURE.secret"\n')
        tool.chmod(0o755)
        self.capture = self.root / "args"
        template = (Path(__file__).resolve().parents[1] / ".gitlab" / "pcbgit.yml").read_text()
        body = template.split("    - |\n", 1)[1].split("\n  artifacts:", 1)[0]
        self.script = "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())
        self.env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                    "CAPTURE": str(self.capture), "CI_COMMIT_SHA": self.head,
                    "CI_COMMIT_BRANCH": "main", "CI_DEFAULT_BRANCH": "main", "CI_PIPELINE_SOURCE": "push"}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()

    def run_template(self, **env):
        return subprocess.run(["bash", "-c", self.script], cwd=self.repo, env={**self.env, **env}, capture_output=True, text=True, timeout=30)

    def test_multi_commit_push_uses_before_sha(self):
        result = self.run_template(CI_COMMIT_BEFORE_SHA=self.first)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.capture.read_text().splitlines()
        self.assertEqual(args[args.index("--base") + 1], self.first)

    def test_initial_push_uses_empty_baseline(self):
        result = self.run_template(CI_COMMIT_BEFORE_SHA="0" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(":empty:", self.capture.read_text().splitlines())

    def test_unknown_previous_commit_fails_instead_of_using_parent(self):
        result = self.run_template(CI_COMMIT_BEFORE_SHA="f" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot access", result.stderr)

    def test_non_fast_forward_push_fails_explicitly(self):
        result = self.run_template(CI_COMMIT_BEFORE_SHA=self.head, CI_COMMIT_SHA=self.first)
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-fast-forward", result.stderr)

    def test_new_branch_refreshes_default_branch(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, timeout=30)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "origin", "main")
        self.git("update-ref", "refs/remotes/origin/main", self.first)
        result = self.run_template(CI_COMMIT_BEFORE_SHA="0" * 40, CI_COMMIT_BRANCH="feature")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main"), self.head)
        args = self.capture.read_text().splitlines()
        self.assertEqual(args[args.index("--base") + 1], "refs/remotes/origin/main")

    def test_fork_does_not_pass_credentials(self):
        result = self.run_template(CI_PIPELINE_SOURCE="merge_request_event", CI_MERGE_REQUEST_DIFF_BASE_SHA=self.first,
                                   CI_MERGE_REQUEST_SOURCE_PROJECT_ID="2", CI_MERGE_REQUEST_TARGET_PROJECT_ID="1",
                                   MOUSER_API_KEY="private")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--skip-inventory", self.capture.read_text().splitlines())
        self.assertEqual(Path(str(self.capture) + ".secret").read_text(), "unset")
