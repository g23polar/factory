"""Tests for factory/bin/job-diff: the full diff survives an output-rewriting hook (rtk).

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

BIN_JOB_DIFF = Path(__file__).resolve().parents[1] / "bin" / "job-diff"
JOB_ID = "001-big-change"
MARKER = "MARKER-past-line-101"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

# Like `rtk git …`: run the real git, then keep only the first 100 lines and exit 0.
FAKE_RTK = """#!/bin/bash
"$@" | head -n 100
exit 0
"""


def hook(cmd):
    """Stub PreToolUse hook: rewrite a command starting with `git ` the way rtk does."""
    return f"rtk {cmd}" if cmd.startswith("git ") else cmd


class JobDiffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="job-diff-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        cls.git("init", "-q")
        (cls.repo / "big.txt").write_text("base\n")
        cls.git("add", "big.txt")
        cls.git("commit", "-q", "-m", "base")
        cls.git("update-ref", "refs/remotes/origin/main", "HEAD")
        lines = [f"line {i}" for i in range(150)]
        lines[140] = MARKER
        (cls.repo / "big.txt").write_text("base\n" + "\n".join(lines) + "\n")
        cls.git("commit", "-q", "-am", "add 150 lines")

        fake = cls.tmp / "fakebin"
        fake.mkdir()
        (fake / "rtk").write_text(FAKE_RTK)
        (fake / "rtk").chmod(0o755)
        cls.env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}

    def setUp(self):
        factory = Path(tempfile.mkdtemp(prefix="main-", dir=self.tmp)) / "factory"
        (factory / "bin").mkdir(parents=True)
        (factory / "config.json").write_text(json.dumps({"base_branch": "main"}))
        self.helper = factory / "bin" / "job-diff"
        shutil.copy2(BIN_JOB_DIFF, self.helper)
        self.jobs = factory / "jobs"
        self.folder = self.jobs / JOB_ID
        self.folder.mkdir(parents=True)
        self.write_job(self.repo)
        self.patch = self.folder / "diff.patch"
        loader = importlib.machinery.SourceFileLoader("factory_job_diff", str(self.helper))
        self.mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_job_diff", loader))
        loader.exec_module(self.mod)

    def write_job(self, worktree):
        (self.folder / "job.json").write_text(json.dumps(
            {"id": JOB_ID, "worktree": str(worktree), "branch": "factory/x"}))

    @classmethod
    def git(cls, *args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cls.repo,
                              env={**os.environ, **GIT_ENV}, capture_output=True, check=True).stdout

    def bash(self, cmd):
        """Run a command as the agent's Bash tool would, after the stub hook."""
        return subprocess.run(["bash", "-c", hook(cmd)], cwd=self.repo, env=self.env, capture_output=True)

    def run_helper(self, *args):
        return self.bash(" ".join([str(self.helper), *args]))

    def run_in_process(self, *args):
        """Run the helper in-process (cheaper than a new python): (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                self.mod.main(list(args))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def raw_diff(self):
        return self.git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "origin/main...HEAD")

    def diff_part(self):
        head, sep, rest = self.patch.read_bytes().partition(b"# Diff (origin/main...HEAD)\n")
        self.assertTrue(sep, "no `# Diff` line")
        self.assertTrue(head.startswith(b"# Commits (origin/main..HEAD)\n"), head[:80])
        return rest

    def test_a_control_fake_rtk_cuts_the_diff(self):
        res = self.bash("git diff origin/main...HEAD")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 100)
        self.assertNotIn(MARKER.encode(), res.stdout)
        self.assertIn(MARKER.encode(), self.raw_diff())

    def test_b_helper_writes_the_full_diff_through_the_hook(self):
        res = self.run_helper(JOB_ID)
        self.assertEqual(res.returncode, 0, res.stderr)
        part = self.diff_part()
        self.assertEqual(part, self.raw_diff())
        self.assertIn(MARKER.encode(), part)
        self.assertIn(b"add 150 lines", self.patch.read_bytes())

    def test_c_hostile_repo_config_is_ignored(self):
        junk = self.tmp / "junk.sh"
        junk.write_text("#!/bin/sh\necho JUNK-FROM-EXT-DIFF\n")
        junk.chmod(0o755)
        for key, value in (("color.ui", "always"), ("diff.external", str(junk)),
                           ("core.pager", "echo JUNK-FROM-PAGER")):
            self.git("config", key, value)
            self.addCleanup(self.git, "config", "--unset", key)
        code, _, err = self.run_in_process(JOB_ID)
        self.assertEqual(code, 0, err)
        data = self.patch.read_bytes()
        self.assertNotIn(b"\x1b", data)
        self.assertNotIn(b"JUNK", data)
        self.assertIn(MARKER.encode(), self.diff_part())

    def test_d_printed_line_count_matches_the_file(self):
        code, out, err = self.run_in_process(JOB_ID)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("\n"), 1, out)
        path, _, count = out.strip().rpartition(": ")
        self.assertEqual(path, str(self.patch.resolve()))
        self.assertTrue(count.endswith(" lines"), out)
        self.assertEqual(int(count.split()[0]), len(self.patch.read_bytes().splitlines()))
        self.assertGreater(int(count.split()[0]), 150)

    def test_e_reruns_and_concurrent_runs_leave_one_identical_file(self):
        self.assertEqual(self.run_in_process(JOB_ID)[0], 0)
        first = self.patch.read_bytes()
        out = io.StringIO()  # four reruns at once, each replacing the file the first run wrote
        threads = [threading.Thread(target=self.mod.job_diff, args=(JOB_ID,)) for _ in range(4)]
        with redirect_stdout(out):
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(out.getvalue().splitlines()), 4)
        self.assertEqual(self.patch.read_bytes(), first)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["diff.patch", "job.json"])

    def test_f_errors_write_nothing(self):
        before = sorted(p.relative_to(self.jobs) for p in self.jobs.rglob("*"))
        for args in (("no-such-job",), (), (JOB_ID, "extra"), ("../jobs",)):
            code, out, err = self.run_in_process(*args)
            self.assertNotEqual(code, 0, args)
            self.assertTrue(err.strip(), args)
            self.assertEqual(out, "", args)
        self.assertEqual(sorted(p.relative_to(self.jobs) for p in self.jobs.rglob("*")), before)

    def test_f_git_failure_leaves_the_old_file_untouched(self):
        self.patch.write_text("old\n")
        not_a_repo = self.tmp / "not-a-repo"
        not_a_repo.mkdir(exist_ok=True)
        self.write_job(not_a_repo)
        code, _, err = self.run_in_process(JOB_ID)
        self.assertNotEqual(code, 0)
        self.assertTrue(err.strip())
        self.assertEqual(self.patch.read_text(), "old\n")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["diff.patch", "job.json"])


if __name__ == "__main__":
    unittest.main()
