"""Tests for the local auto-merge policy in factory/bin/job (config.json `auto_merge`).

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import subprocess
import unittest
from pathlib import Path

BIN_JOB = Path(__file__).resolve().parents[1] / "bin" / "job"


def load_job_module():
    loader = importlib.machinery.SourceFileLoader("factory_job", str(BIN_JOB))
    spec = importlib.util.spec_from_loader("factory_job", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


POLICY = {"max_risk": "low", "deny_surfaces": ["security"], "escalate_paths": ["src/agent/*"]}


class AutoMergeBlockersTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job_module()
        self.mod.CONFIG = {**self.mod.CONFIG, "auto_merge": POLICY}
        self.changed = ["src/scheduler/pack.py"]
        self.mod.git = lambda job, *args, check=True: subprocess.CompletedProcess(
            args, 0, stdout="\n".join(self.changed) + "\n", stderr="")

    def job(self, risk="low", surfaces=()):
        return {"id": "001-x", "worktree": "/nonexistent",
                "intake": {"risk": risk, "surfaces": list(surfaces)}}

    def test_low_risk_no_surface_outside_agent_layer_may_merge(self):
        self.assertEqual(self.mod.auto_merge_blockers(self.job()), [])

    def test_ui_surface_alone_does_not_block(self):
        self.assertEqual(self.mod.auto_merge_blockers(self.job(surfaces=["ui"])), [])

    def test_medium_and_high_risk_block(self):
        for risk in ("medium", "high"):
            reasons = self.mod.auto_merge_blockers(self.job(risk=risk))
            self.assertEqual(len(reasons), 1, risk)
            self.assertIn(f"`{risk}`", reasons[0])

    def test_unknown_or_missing_risk_blocks(self):
        for risk in (None, "", "LOW", "critical"):
            self.assertTrue(self.mod.auto_merge_blockers(self.job(risk=risk)), risk)
        self.assertTrue(self.mod.auto_merge_blockers({"id": "x", "worktree": "/n", "intake": None}))

    def test_security_surface_blocks(self):
        reasons = self.mod.auto_merge_blockers(self.job(surfaces=["ui", "security"]))
        self.assertEqual(reasons, ["intake flagged the `security` surface"])

    def test_agent_layer_change_blocks_including_nested_paths(self):
        self.changed = ["src/agent/prompt.py", "src/agent/tools/x.py", "README.md"]
        reasons = self.mod.auto_merge_blockers(self.job())
        self.assertEqual(len(reasons), 1)
        self.assertIn("src/agent/prompt.py", reasons[0])
        self.assertIn("src/agent/tools/x.py", reasons[0])
        self.assertNotIn("README.md", reasons[0])

    def test_agent_tests_do_not_block(self):
        self.changed = ["tests/agent/test_loop.py"]
        self.assertEqual(self.mod.auto_merge_blockers(self.job()), [])

    def test_reasons_accumulate(self):
        self.changed = ["src/agent/loop.py"]
        reasons = self.mod.auto_merge_blockers(self.job(risk="high", surfaces=["security"]))
        self.assertEqual(len(reasons), 3)

    def test_no_policy_means_no_blockers(self):
        self.mod.CONFIG = {k: v for k, v in self.mod.CONFIG.items() if k != "auto_merge"}
        self.assertEqual(self.mod.auto_merge_blockers(self.job(risk="high")), [])


if __name__ == "__main__":
    unittest.main()
