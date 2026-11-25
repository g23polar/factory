"""Tests for the shared resource lock (factory/bin/locked), the Orca view of
factory jobs (bin/job orca_sync/orca_pane, bin/job-watch) and the merge's
--match-head-commit.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name):
    loader = importlib.machinery.SourceFileLoader(f"factory_{name.replace('-', '_')}", str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def job(worktree, stage="review", **kw):
    return {"id": "004-dark-mode", "issue": 12, "title": "Dark mode", "pr": 30, "stage": stage,
            "round": 2, "round_limit": 3, "gate": "PASS", "reviews": {"code": "PASS", "ui": "CHANGES"},
            "cost_usd": 1.2, "worktree": worktree, **kw}


class OrcaViewTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("job")
        self.calls = []
        self.answer = True
        self.mod.orca = lambda *args: (self.calls.append(args), self.answer)[1]
        self.tmp = tempfile.mkdtemp()

    def args_of(self, verb):
        return [c for c in self.calls if c[:2] == verb]

    def test_sync_mirrors_stage_onto_the_tab(self):
        self.mod.orca_sync(job(self.tmp))
        (args,) = self.args_of(("worktree", "set"))
        self.assertIn(f"path:{self.tmp}", args)
        self.assertEqual(args[args.index("--display-name") + 1], "#12 Dark mode (004-dark-mode)")
        self.assertEqual(args[args.index("--workspace-status") + 1], "in-review")
        self.assertEqual(args[args.index("--pr") + 1], "30")
        self.assertEqual(args[args.index("--comment") + 1],
                         "round 2/3 · review · gate PASS · code PASS, ui CHANGES · $1.20")
        self.assertNotIn("--unread", args)

    def test_status_mapping(self):
        for stage, status in [("spec", "in-progress"), ("build", "in-progress"), ("gate", "in-progress"),
                              ("approve", "in-review"), ("merging", "in-review"), ("needs-human", "in-review")]:
            self.calls.clear()
            self.mod.orca_sync(job(self.tmp, stage=stage))
            (args,) = self.args_of(("worktree", "set"))
            self.assertEqual(args[args.index("--workspace-status") + 1], status, stage)

    def test_waiting_on_a_human_marks_the_tab_unread(self):
        self.mod.orca_sync(job(self.tmp, stage="needs-human"))
        self.assertIn("--unread", self.args_of(("worktree", "set"))[0])

    def test_no_tab_without_a_worktree(self):
        self.mod.orca_sync(job("/nonexistent/worktree", stage="intake"))
        self.mod.orca_pane(job("/nonexistent/worktree", stage="intake"))
        self.assertEqual(self.calls, [])

    def test_pane_opens_once(self):
        j = job(self.tmp, stage="spec")
        self.mod.orca_pane(j)
        self.mod.orca_pane(j)
        (args,) = self.args_of(("terminal", "create"))
        self.assertTrue(j["orca_pane"])
        self.assertIn("factory", args)
        self.assertTrue(args[args.index("--command") + 1].endswith("job-watch 004-dark-mode"))

    def test_pane_retried_when_orca_is_down(self):
        self.answer = False
        j = job(self.tmp, stage="spec")
        self.mod.orca_pane(j)
        self.assertNotIn("orca_pane", j)

    def test_save_writes_state_before_orca_and_survives_it_breaking(self):
        jobs = Path(tempfile.mkdtemp())
        self.mod.JOBS = jobs
        (jobs / "004-dark-mode").mkdir()
        seen = []

        def broken(*args):
            seen.append((jobs / "004-dark-mode" / "job.json").exists())
            raise AttributeError("orca answered with a list")
        self.mod.orca = broken
        j = job(self.tmp, stage="spec")
        self.mod.save(j)  # must not raise
        self.assertEqual(seen, [True])  # job.json was already on disk when Orca was called
        self.assertEqual(json.loads((jobs / "004-dark-mode" / "job.json").read_text())["stage"], "spec")

    def test_pane_flag_is_persisted(self):
        jobs = Path(tempfile.mkdtemp())
        self.mod.JOBS = jobs
        (jobs / "004-dark-mode").mkdir()
        self.mod.save(job(self.tmp, stage="spec"))
        self.assertTrue(json.loads((jobs / "004-dark-mode" / "job.json").read_text())["orca_pane"])

    def test_pane_command_is_shell_quoted(self):
        self.mod.BIN = Path("/Users/me/my repo/factory/bin")
        self.mod.orca_pane(job(self.tmp, stage="spec"))
        (args,) = self.args_of(("terminal", "create"))
        self.assertEqual(args[args.index("--command") + 1],
                         "'/Users/me/my repo/factory/bin/job-watch' 004-dark-mode")

    def test_unexpected_orca_output_is_not_ok(self):
        mod = load("job")
        fake = Path(self.tmp) / "orca"
        for output in ["null", "[1]", '"ok"', '{"ok": false}', "not json", '{"ok": "yes"}']:
            fake.write_text(f"#!/bin/sh\necho '{output}'\n")
            fake.chmod(0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = f"{self.tmp}:{old}"
            try:
                self.assertIs(mod.orca("worktree", "ps"), False, output)
            finally:
                os.environ["PATH"] = old
        fake.write_text("#!/bin/sh\necho '{\"ok\": true}'\n")
        os.environ["PATH"] = f"{self.tmp}:{os.environ['PATH']}"
        self.assertIs(mod.orca("worktree", "ps"), True)

    def test_orca_missing_is_harmless(self):
        mod = load("job")  # the real orca(), with no orca binary on PATH
        old = os.environ["PATH"]
        os.environ["PATH"] = self.tmp
        try:
            self.assertIs(mod.orca("worktree", "ps"), False)
            mod.orca_sync(job(self.tmp))
        finally:
            os.environ["PATH"] = old


class MergeMatchesHeadTest(unittest.TestCase):
    def test_merge_pins_the_gated_commit(self):
        mod = load("job")
        calls = []
        mod.sync = lambda j: j
        mod.save = lambda j: None
        mod.advance = lambda j, stage: j
        mod.auto_merge_blockers = lambda j: []
        mod.git = lambda j, *a, check=True: subprocess.CompletedProcess(a, 0, stdout="abc123\n", stderr="")
        mod.gh = lambda *a: calls.append(a) or ""
        mod.gh_json = lambda *a: {"isDraft": False}
        mod.pr_state = lambda j: "OPEN"
        mod.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))})
        mod.cmd_merge({"id": "004-x", "stage": "merging", "pr": 30, "branch": "factory/004-x", "round": 1})
        merge = next(c for c in calls if c[:2] == ("pr", "merge"))
        self.assertEqual(merge[merge.index("--match-head-commit") + 1], "abc123")


class JobWatchTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("job-watch")
        self.jobs = Path(tempfile.mkdtemp())
        self.mod.J.JOBS = self.jobs

    def test_once_renders_stage_and_agent_first_lines(self):
        d = self.jobs / "004-dark-mode"
        (d / "round-2").mkdir(parents=True)
        (d / "job.json").write_text(json.dumps(job("/w")))
        (d / "build.md").write_text("STATUS: READY\n\nmore")
        (d / "round-2" / "checks.md").write_text("GATE: PASS\n")
        (d / "round-2" / "review-ui.md").write_text("VERDICT: CHANGES\n")
        stage, text = self.mod.render("004-dark-mode")
        self.assertEqual(stage, "review")
        self.assertIn("round 2/3 · review · gate PASS · code PASS, ui CHANGES · $1.20 · PR #30", text)
        self.assertIn("STATUS: READY", text)
        self.assertIn("round-2/review-ui.md", text)
        self.assertIn("VERDICT: CHANGES", text)

    def test_older_job_shape_renders(self):
        d = self.jobs / "001-old"
        d.mkdir()
        (d / "job.json").write_text(json.dumps({"id": "001-old", "issue": 1, "title": "x", "stage": "build"}))
        stage, text = self.mod.render("001-old")
        self.assertEqual(stage, "build")

    def test_exits_when_the_job_is_gone(self):
        start = time.monotonic()
        self.assertEqual(self.mod.main(["404-gone"]), 0)
        self.assertLess(time.monotonic() - start, 2)


class LockedTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {**os.environ, "FACTORY_LOCK_DIR": self.dir, "FACTORY_LOCK_WAIT": "1"}

    def locked(self, *args):
        return subprocess.run([str(BIN / "locked"), *args], env=self.env, capture_output=True, text=True)

    def hold(self, res, seconds):
        p = subprocess.Popen([str(BIN / "locked"), res, "sleep", str(seconds)], env=self.env)
        time.sleep(0.5)
        kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
        self.addCleanup(self.release, p, [int(pid) for pid in kids])
        return p

    def release(self, holder, kids):
        holder.kill()
        holder.wait()
        for pid in kids:  # locked runs its command in a new session, so killing locked misses it
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def kill_pidfile(self, pidfile):
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_runs_command_and_returns_its_exit_code(self):
        res = self.locked("pg", sys.executable, "-c", "print('hi'); raise SystemExit(3)")
        self.assertEqual((res.returncode, res.stdout.strip()), (3, "hi"))

    def test_busy_resource_exits_75_without_running(self):
        self.hold("sim", 5)
        res = self.locked("sim", "echo", "ran")
        self.assertEqual(res.returncode, 75)
        self.assertNotIn("ran", res.stdout)
        self.assertIn("sim busy", res.stderr)
        self.assertIn("sleep 5", res.stderr)  # says who holds it

    def test_resources_are_independent(self):
        self.hold("sim", 5)
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_lock_is_released_when_the_holder_dies(self):
        holder = self.hold("pg", 30)
        holder.kill()
        holder.wait()
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_max_kills_the_whole_process_group(self):
        marker = Path(self.dir) / "grandchild-survived"
        start = time.monotonic()
        res = self.locked("--max", "1", "pg", "bash", "-c", f"(sleep 3; touch {marker}) & sleep 30")
        self.assertEqual(res.returncode, 124)
        self.assertLess(time.monotonic() - start, 10)
        time.sleep(3)
        self.assertFalse(marker.exists())
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_lingering_grandchild_does_not_keep_the_lock(self):
        pidfile = Path(self.dir) / "grandchild.pid"
        self.addCleanup(self.kill_pidfile, pidfile)
        res = self.locked("pg", "bash", "-c", f"nohup sleep 20 >/dev/null 2>&1 & echo $! > {pidfile}; disown; exit 0")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.locked("pg", "true").returncode, 0)

    def test_default_lock_dir_is_the_same_from_a_worktree(self):
        mod = load("locked")
        main = mod.default_lock_dir()
        self.assertEqual(main.parent, Path.home() / ".local/share/factory-locks")
        repo = BIN.parents[1]
        if subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo,
                          capture_output=True).returncode != 0:
            self.skipTest("no commits yet: can't make a worktree")
        wt = Path(tempfile.mkdtemp()) / "wt"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt)], cwd=repo, check=True)
        try:
            (wt / "factory/bin").mkdir(parents=True, exist_ok=True)  # uncommitted in a fresh install
            (wt / "factory/bin/locked").write_text((BIN / "locked").read_text())  # this version
            code = ("import importlib.machinery as m, importlib.util as u;"
                    f"l=m.SourceFileLoader('l','{wt}/factory/bin/locked');"
                    "s=u.spec_from_loader('l',l); mod=u.module_from_spec(s); l.exec_module(mod);"
                    "print(mod.default_lock_dir())")
            copy = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertEqual(copy.stdout.strip(), str(main))
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo)

    def test_any_resource_name_works_and_bad_names_are_usage_errors(self):
        self.assertEqual(self.locked("db-main", "true").returncode, 0)
        for bad in ["DB", "../x", "a b", "-x"]:
            self.assertEqual(self.locked(bad, "true").returncode, 2, bad)


if __name__ == "__main__":
    unittest.main()
