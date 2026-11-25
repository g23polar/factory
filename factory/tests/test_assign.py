"""Tests that factory/bin/job assigns someone when a job waits on a human:
the issue author on needs-info, the operator (issue and PR) on needs-human.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import io
import subprocess
import unittest
from contextlib import redirect_stderr
from pathlib import Path

BIN_JOB = Path(__file__).resolve().parents[1] / "bin" / "job"


def load_job_module():
    loader = importlib.machinery.SourceFileLoader("factory_job_assign", str(BIN_JOB))
    spec = importlib.util.spec_from_loader("factory_job_assign", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class AssignTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job_module()
        self.calls = []
        self.returncode = 0

        def fake_run(cmd, cwd=None, check=True):
            self.calls.append((cmd, check))
            return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="no access")
        self.mod.run = fake_run

    def test_assign_adds_assignee_without_failing_the_move(self):
        self.mod.assign("issue", 7, "octo")
        self.assertEqual(self.calls, [(["gh", "issue", "edit", "7", "--add-assignee", "octo"], False)])

    def test_assign_failure_warns_and_does_not_raise(self):
        self.returncode = 1
        err = io.StringIO()
        with redirect_stderr(err):
            self.mod.assign("pr", 9, "outsider")
        self.assertIn("couldn't assign @outsider to pr #9", err.getvalue())

    def test_label_no_longer_assigns(self):
        self.mod.label("issue", 7, add="factory:needs-human")
        self.assertEqual(self.calls, [(["gh", "issue", "edit", "7", "--add-label", "factory:needs-human"], True)])

    def test_waiting_stages_assign_the_right_person(self):
        src = BIN_JOB.read_text()
        needs_info = src.index('add=LABELS["needs_info"])')
        self.assertIn('assign("issue", job["issue"], fetch_issue(job["issue"])["author"]["login"])',
                      src[needs_info:needs_info + 200])
        needs_human = src.index('add=LABELS["needs_human"])')
        self.assertIn('assign("issue", job["issue"], operator())', src[needs_human:needs_human + 200])
        self.assertIn('assign("pr", job["pr"], operator())', src)


if __name__ == "__main__":
    unittest.main()
