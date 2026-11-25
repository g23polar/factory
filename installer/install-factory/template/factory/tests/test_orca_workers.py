"""Tests for factory agents as Orca workers: `job spawn`, `job send`, the job's
worktree at create, agent permissions, tab cleanup and cost from session
transcripts.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


def load_job():
    loader = importlib.machinery.SourceFileLoader("factory_job_w", str(BIN / "job"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.root = Path(tempfile.mkdtemp())
        self.mod.JOBS = self.root / "jobs"
        (self.mod.JOBS / "004-dark-mode").mkdir(parents=True)
        self.wt = self.root / "wt"
        self.wt.mkdir()
        self.mod.orca = lambda *a: True  # the view, not under test here
        self.calls = []
        self.replies = {}

        def strict(*args):
            self.calls.append(args)
            return self.replies.get(args[:2], {})
        self.mod.orca_strict = strict

    def job(self, **kw):
        return {"id": "004-dark-mode", "issue": 12, "title": "Dark mode", "stage": "build", "round": 2,
                "round_limit": 3, "branch": "factory/004-dark-mode", "worktree": str(self.wt), "pr": 30,
                "gate": None, "reviews": {}, "cost_usd": 0, **kw}

    def saved(self):
        return json.loads((self.mod.JOBS / "004-dark-mode" / "job.json").read_text())

    def test_spawn_opens_an_agent_tab_and_dispatches_a_worker(self):
        self.replies = {("terminal", "create"): {"terminal": {"handle": "term_1"}},
                        ("orchestration", "worker-start"): {"dispatchId": "ctx_1"}}
        self.mod.cmd_spawn(self.job(), "reviewer-ui", "claude-opus-5-5", "Review round 2. Port: 5142.")
        create, start = self.calls
        self.assertEqual(create[create.index("--title") + 1], "reviewer-ui r2")
        command = shlex.split(create[create.index("--command") + 1])
        self.assertEqual(command[:3], ["claude", "--agent", "reviewer-ui"])
        self.assertIn("bypassPermissions", command)
        self.assertEqual(command[-2:], ["--model", "claude-opus-5-5"])
        self.assertIn("term_1", start)
        self.assertIn("--keep-open", start)
        self.assertIn(f"path:{self.wt}", start)
        task = Path(start[start.index("--spec-file") + 1]).read_text()
        self.assertTrue(task.startswith("Job: 004-dark-mode. Job folder: "))
        self.assertIn("Review round 2. Port: 5142.", task)
        self.assertIn("worker_done", task)
        self.assertIn("never use `orca orchestration ask`", task)
        self.assertEqual(self.saved()["dispatches"]["reviewer-ui"],
                         {"dispatch": "ctx_1", "terminal": "term_1", "title": "reviewer-ui r2"})

    def test_intake_gets_only_read_its_file_and_orca(self):
        flags = self.mod.agent_permissions(self.job(stage="intake"), "intake")
        self.assertEqual(flags[:2], ["--permission-mode", "dontAsk"])
        allowed = flags[flags.index("--allowedTools") + 1:]
        self.assertEqual([a for a in allowed if a.startswith("Bash")],
                         ["Bash(orca orchestration send --from * --type worker_done *)"])
        self.assertIn(f"Edit(/{self.mod.JOBS}/004-dark-mode/intake.md)", allowed)
        self.assertEqual(self.mod.agent_permissions(self.job(), "builder"),
                         ["--permission-mode", "bypassPermissions"])

    def test_spawn_refuses_unknown_agent(self):
        with self.assertRaises(self.mod.Refused):
            self.mod.cmd_spawn(self.job(), "no-such-agent", None, "")
        self.assertEqual(self.calls, [])

    def test_spawn_makes_a_missing_worktree(self):
        made = []
        self.mod.make_worktree = lambda j: made.append(j["id"])
        self.replies = {("terminal", "create"): {"terminal": {"handle": "term_1"}},
                        ("orchestration", "worker-start"): {"dispatchId": "ctx_1"}}
        self.mod.cmd_spawn(self.job(worktree=str(self.root / "missing")), "intake", None, "")
        self.assertEqual(made, ["004-dark-mode"])

    def test_failed_dispatch_closes_the_new_tab(self):
        closed = []
        self.mod.orca = lambda *a: closed.append(a) or True
        self.replies = {("terminal", "create"): {"terminal": {"handle": "term_9"}}}  # worker-start: no id
        with self.assertRaises(self.mod.Refused):
            self.mod.cmd_spawn(self.job(), "builder", None, "Build round 1.")
        self.assertIn(("terminal", "close", "--terminal", "term_9"), closed)

    def test_spawn_stops_when_orca_gives_no_handle(self):
        with self.assertRaises(self.mod.Refused):
            self.mod.cmd_spawn(self.job(), "builder", None, "Build round 1.")
        self.assertEqual(len(self.calls), 1)  # never dispatched

    def test_send_goes_to_the_builder_and_tracks_the_follow_up_dispatch(self):
        j = self.job(dispatches={"builder": {"dispatch": "ctx_1", "terminal": "term_1", "title": "builder r1"}})
        self.replies = {("orchestration", "send"): {"dispatchId": "ctx_2"}}
        self.mod.cmd_send(j, "builder", "Build round 2. Port: 5140.")
        (send,) = self.calls
        self.assertEqual(send[send.index("--to") + 1], "dispatch:ctx_1")
        self.assertIn("Build round 2. Port: 5140.", send[send.index("--body") + 1])
        self.assertEqual(self.saved()["dispatches"]["builder"]["dispatch"], "ctx_2")

    def test_send_without_a_worker_refuses(self):
        with self.assertRaises(self.mod.Refused):
            self.mod.cmd_send(self.job(), "builder", "x")


class OrcaStrictTest(unittest.TestCase):
    def test_failures_raise_refused(self):
        mod = load_job()
        for out in [b'{"ok":false,"error":{"message":"terminal_worktree_mismatch"}}\n', b"", b"garbage"]:
            mod.subprocess = type("S", (), {
                "run": staticmethod(lambda *a, out=out, **k: subprocess.CompletedProcess(a, 1, out, b"")),
                "TimeoutExpired": subprocess.TimeoutExpired})
            with self.assertRaises(mod.Refused):
                mod.orca_strict("terminal", "create")

    def test_pretty_printed_and_trailing_text_output_parse(self):
        mod = load_job()
        for out in [b'{\n  "ok": true,\n  "result": {\n    "dispatchId": "ctx_1"\n  }\n}\n',
                    b'{"ok":true,"result":{"dispatchId":"ctx_1"}}\nerror: trailing text\n']:
            mod.subprocess = type("S", (), {
                "run": staticmethod(lambda *a, out=out, **k: subprocess.CompletedProcess(a, 0, out, b"")),
                "TimeoutExpired": subprocess.TimeoutExpired})
            self.assertEqual(mod.orca_strict("orchestration", "worker-start"), {"dispatchId": "ctx_1"})



class WorktreeLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.calls = []
        self.mod.run = lambda cmd, cwd=None, check=True: (self.calls.append(tuple(cmd)),
                                                           subprocess.CompletedProcess(cmd, 0, "", ""))[1]

    def test_create_makes_the_worktree_with_a_claude_link(self):
        wt = Path(tempfile.mkdtemp()) / "004-x"
        wt.mkdir()  # git worktree add is mocked; the link is real
        self.mod.make_worktree({"worktree": str(wt), "branch": "factory/004-x"})
        self.assertIn(("git", "worktree", "add", "-b", "factory/004-x", str(wt), f"origin/{self.mod.BASE}"),
                      self.calls)
        self.assertEqual((wt / ".claude").resolve(), (self.mod.REPO / ".claude").resolve())

    def test_terminal_stage_closes_tabs_before_removing_the_worktree(self):
        order = []
        self.mod.orca = lambda *a: order.append(("orca",) + a) or True
        self.mod.run = lambda cmd, cwd=None, check=True: order.append(tuple(cmd)) or \
            subprocess.CompletedProcess(cmd, 0, "", "")
        self.mod.pr_state = lambda j: "MERGED"
        self.mod.label = lambda *a, **k: None
        wt = tempfile.mkdtemp()
        self.mod.advance({"id": "004-x", "issue": 1, "stage": "review", "round": 1, "pr": 3,
                          "branch": "factory/004-x", "worktree": wt}, "merged")
        close = next(i for i, c in enumerate(order) if c[:3] == ("orca", "terminal", "close"))
        remove = next(i for i, c in enumerate(order) if c[:3] == ("git", "worktree", "remove"))
        self.assertLess(close, remove)


class CostTest(unittest.TestCase):
    def test_session_transcripts_in_the_job_worktree_count(self):
        mod = load_job()
        config = Path(tempfile.mkdtemp())
        mod.os.environ["CLAUDE_CONFIG_DIR"] = str(config)
        try:
            job = {"id": "004-x", "worktree": "/Users/me/repo/.worktrees/004-x"}
            folder = mod.transcripts_dir(job["worktree"])
            folder.mkdir(parents=True)
            usage = {"input_tokens": 1_000_000, "output_tokens": 0}
            lines = [{"type": "user", "message": {"content": "Job: 004-x."}},
                     {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage}},
                     {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": usage}}]
            (folder / "s1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
            price = mod.CONFIG["prices_usd_per_mtok"]["claude-opus-5-5"]["input"]
            self.assertEqual(mod.cost_usd(job), round(price, 2))  # m1 counted once
        finally:
            del mod.os.environ["CLAUDE_CONFIG_DIR"]


if __name__ == "__main__":
    unittest.main()
