"""Tests for factory notifications: a job that starts waiting on the user
(needs-info, needs-human) notifies, and `job notify` lets agents and the
orchestrator say they're waiting. Notifications are best effort.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

BIN_JOB = Path(__file__).resolve().parents[1] / "bin" / "job"


def load_job():
    loader = importlib.machinery.SourceFileLoader("factory_job_notify", str(BIN_JOB))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.calls = []
        self.returncode = 0

        def fake_run(cmd, cwd=None, check=True):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="boom")
        self.mod.run = fake_run

    def test_title_and_message_are_argv_not_script(self):
        self.mod.notify('Factory: "x"', 'say "hi" \\ end run')
        (cmd,) = self.calls
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-2:], ['Factory: "x"', 'say "hi" \\ end run'])
        script = " ".join(cmd[1:-2])
        self.assertNotIn("hi", script)
        self.assertIn("item 2 of argv", script)

    def test_message_is_one_line_and_bounded(self):
        self.mod.notify("t", "a\n\nb  " + "x" * 500)
        msg = self.calls[0][-1]
        self.assertTrue(msg.startswith("a b x"))
        self.assertLessEqual(len(msg), 240)

    def test_failure_warns_and_does_not_raise(self):
        self.returncode = 1
        err = io.StringIO()
        with redirect_stderr(err):
            self.mod.notify("t", "m")
        self.assertIn("couldn't notify", err.getvalue())

    def test_missing_osascript_does_not_raise(self):
        def missing(cmd, cwd=None, check=True):
            raise FileNotFoundError("osascript")
        self.mod.run = missing
        with redirect_stderr(io.StringIO()):
            self.mod.notify("t", "m")


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.mod.JOBS = Path(tempfile.mkdtemp()) / "jobs"
        self.d = self.mod.JOBS / "004-x"
        self.d.mkdir(parents=True)
        self.sent = []
        self.mod.notify = lambda title, message: self.sent.append((title, message))
        for name in ("comment", "label", "assign"):
            setattr(self.mod, name, lambda *a, **k: None)
        self.mod.operator = lambda: "me"

    def job(self, stage, **kw):
        return {"id": "004-x", "issue": 12, "stage": stage, "round": 1, "round_limit": 3,
                "pr": None, "branch": "factory/004-x", "worktree": "/nonexistent", **kw}

    def test_escalation_notifies_with_the_reason(self):
        (self.d / "decision.md").write_text("ESCALATE\n\n## Why\nTouches auth; needs a human look.\n")
        self.mod.advance(self.job("approve"), "needs-human")
        self.assertEqual(self.sent, [("Factory: needs a human", "#12 004-x: Touches auth; needs a human look.")])

    def test_needs_info_notifies(self):
        (self.d / "intake.md").write_text("DECISION: NEEDS-INFO\n\nWhich screens?\n")
        self.mod.fetch_issue = lambda n: {"author": {"login": "octo"}}
        self.mod.advance(self.job("intake"), "needs-info")
        self.assertEqual(self.sent, [("Factory: needs info", "#12 004-x: intake needs more detail on the issue")])

    def test_other_moves_do_not_notify(self):
        (self.d / "decision.md").write_text("APPROVE\n")
        self.mod.advance(self.job("approve"), "merging")
        self.assertEqual(self.sent, [])

    def test_refused_move_does_not_notify(self):
        (self.d / "decision.md").write_text("APPROVE\n")
        with self.assertRaises(self.mod.Refused):
            self.mod.advance(self.job("approve"), "needs-human")
        self.assertEqual(self.sent, [])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.mod.JOBS = Path(tempfile.mkdtemp()) / "jobs"
        (self.mod.JOBS / "004-dark-mode").mkdir(parents=True)
        (self.mod.JOBS / "004-dark-mode" / "job.json").write_text(json.dumps({"id": "004-dark-mode", "issue": 12}))
        self.sent = []
        self.mod.notify = lambda title, message: self.sent.append((title, message))

    def test_job_id_issue_number_and_dash(self):
        self.mod.cmd_notify("004-dark-mode", "builder: which port?")
        self.mod.cmd_notify("12", "intake: which screens?")
        self.mod.cmd_notify("-", "run settled")
        self.assertEqual(self.sent, [("Factory: 004-dark-mode", "builder: which port?"),
                                     ("Factory: 004-dark-mode", "intake: which screens?"),
                                     ("Factory", "run settled")])

    def test_unknown_or_malformed_job_is_refused(self):
        for target in ("005-nope", "99", "../004-dark-mode", "004-dark-mode/.."):
            with self.assertRaises(self.mod.Refused, msg=target):
                self.mod.cmd_notify(target, "hi")
        self.assertEqual(self.sent, [])

    def test_empty_message_is_refused(self):
        with self.assertRaises(self.mod.Refused):
            self.mod.cmd_notify("-", "  \n")

    def test_main_routes_notify(self):
        self.mod.main(["notify", "-", "hello"])
        self.assertEqual(self.sent, [("Factory", "hello")])

    def test_worker_footer_names_the_exact_command(self):
        footer = self.mod.worker_footer({"id": "004-dark-mode"}, "builder")
        self.assertIn(f"`{self.mod.BIN}/job notify 004-dark-mode \"builder: ", footer)

    def test_intake_may_run_notify_for_its_job_only(self):
        job = {"id": "004-dark-mode", "stage": "intake"}
        allowed = self.mod.agent_permissions(job, "intake")
        self.assertIn(f"Bash({self.mod.BIN}/job notify 004-dark-mode *)", allowed)


if __name__ == "__main__":
    unittest.main()
