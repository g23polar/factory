"""Tests that a job waiting on the user carries the needs-human label on its PR
as well as its issue, and that the label leaves both when the job moves on
(feedback, merge, reject).

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

BIN_JOB = Path(__file__).resolve().parents[1] / "bin" / "job"


def load_job():
    loader = importlib.machinery.SourceFileLoader("factory_job_pr_label", str(BIN_JOB))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PrLabelTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.mod.JOBS = Path(tempfile.mkdtemp()) / "jobs"
        self.d = self.mod.JOBS / "004-x"
        self.d.mkdir(parents=True)
        self.labels = []
        self.mod.label = lambda kind, number, add=None, remove=None: self.labels.append((kind, number, add, remove))
        for name in ("comment", "assign", "notify", "gh"):
            setattr(self.mod, name, lambda *a, **k: None)
        self.mod.gh_json = lambda *a: {"isDraft": False}
        self.mod.operator = lambda: "me"
        self.mod.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        self.need = self.mod.LABELS["needs_human"]

    def job(self, stage, pr=30):
        return {"id": "004-x", "issue": 12, "stage": stage, "round": 1, "round_limit": 3,
                "pr": pr, "branch": "factory/004-x", "worktree": "/nonexistent"}

    def escalated(self):
        (self.d / "decision.md").write_text("ESCALATE\n\n## Why\nRisky.\n")

    def test_escalation_labels_issue_and_pr(self):
        self.escalated()
        self.mod.advance(self.job("approve"), "needs-human")
        self.assertIn(("issue", 12, self.need, None), self.labels)
        self.assertIn(("pr", 30, self.need, None), self.labels)

    def test_escalation_without_pr_labels_only_the_issue(self):
        self.escalated()
        (self.d / "build.md").write_text("STATUS: BLOCKED\n")
        self.mod.advance(self.job("build", pr=None), "needs-human")
        self.assertEqual(self.labels, [("issue", 12, self.need, None)])

    def test_feedback_removes_label_from_issue_and_pr(self):
        self.escalated()
        old = time.time() - 60
        os.utime(self.d / "decision.md", (old, old))
        (self.d / "feedback-1.md").write_text("Please rename it.\n")
        self.mod.advance(self.job("needs-human"), "build")
        self.assertIn(("issue", 12, None, self.need), self.labels)
        self.assertIn(("pr", 30, None, self.need), self.labels)

    def test_merge_removes_label_from_issue_and_pr(self):
        self.mod.pr_state = lambda job: "MERGED"
        self.mod.advance(self.job("needs-human"), "merged")
        self.assertIn(("issue", 12, None, self.need), self.labels)
        self.assertIn(("pr", 30, None, self.need), self.labels)

    def test_reject_removes_label_from_pr(self):
        (self.d / "rejection.md").write_text("# Rejected\nNot now.\n")
        self.mod.advance(self.job("needs-human"), "rejected")
        self.assertIn(("pr", 30, None, self.need), self.labels)

    def test_other_moves_leave_pr_labels_alone(self):
        (self.d / "decision.md").write_text("APPROVE\n")
        self.mod.advance(self.job("approve"), "merging")
        self.assertEqual([l for l in self.labels if l[0] == "pr"], [])


if __name__ == "__main__":
    unittest.main()
