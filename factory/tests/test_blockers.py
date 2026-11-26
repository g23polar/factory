"""Tests for issue dependencies: an issue GitHub lists as blocked by an open
issue in this repo is held by `job queue`, and refused by `job create` and the
needs-info → intake move. `gh` is faked, so nothing reaches the network.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

BIN_JOB = Path(__file__).resolve().parents[1] / "bin" / "job"
REPO_URL = "https://api.github.com/repos/Octo/Factory"


def load_job():
    loader = importlib.machinery.SourceFileLoader("factory_job_blockers", str(BIN_JOB))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def dep(number, state="open", reason=None, repo=REPO_URL):
    return {"number": number, "state": state, "state_reason": reason, "repository_url": repo}


class BlockersTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_job()
        self.mod.JOBS = Path(tempfile.mkdtemp()) / "jobs"
        self.mod.JOBS.mkdir()
        self.mod.CONCURRENCY = 2
        self.issues = []
        self.deps = {}  # issue → list of blocker objects, or a string: fail with that stderr
        self.calls = []
        self.labels = []
        self.fetched = []

        def fake_run(cmd, cwd=None, check=True):
            self.calls.append(cmd)
            if cmd[:3] == ["gh", "issue", "list"]:
                out = json.dumps(self.issues)
            elif cmd[:3] == ["gh", "repo", "view"]:
                out = "octo/factory\n"
            elif cmd[:2] == ["gh", "api"] and "/dependencies/blocked_by" in cmd[2]:
                n = int(cmd[2].split("/issues/")[1].split("/")[0])
                got = self.deps.get(n, [])
                if isinstance(got, str):
                    raise self.mod.Refused(f"`{' '.join(cmd)}` failed: {got}")
                out = json.dumps(got)
            else:
                raise AssertionError(f"unexpected command: {cmd}")
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        self.mod.run = fake_run

        def fake_fetch(n):
            self.fetched.append(n)
            return {"number": int(n), "title": f"issue {n}", "state": "OPEN", "url": f"u/{n}",
                    "labels": [{"name": "factory"}]}
        self.mod.fetch_issue = fake_fetch
        self.mod.replied_since_question = lambda issue: True
        self.mod.label = lambda *a, **k: self.labels.append((a, k))
        for name in ("comment", "assign", "orca_sync", "orca_pane"):
            setattr(self.mod, name, lambda *a, **k: None)

    def add_issue(self, n):
        self.issues.append({"number": n, "title": f"issue {n}"})

    def add_job(self, n, stage, round=1):
        job_id = f"{n:03d}-x"
        d = self.mod.JOBS / job_id
        d.mkdir()
        job = {"id": job_id, "issue": n, "stage": stage, "round": round, "round_limit": 3,
               "pr": None, "branch": f"factory/{job_id}", "worktree": "/nonexistent"}
        (d / "job.json").write_text(json.dumps(job))
        return job

    def queue(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.mod.cmd_queue()
        return {int(line.split("\t")[1]): line for line in out.getvalue().splitlines()}

    def dep_calls(self):
        return [c for c in self.calls if c[:2] == ["gh", "api"]]

    # 1
    def test_no_blockers_is_new(self):
        self.add_issue(14)
        self.assertEqual(self.queue()[14], "new\t14\t-\t-\tissue 14")

    # 2
    def test_open_blocker_is_blocked(self):
        self.add_issue(14)
        self.deps[14] = [dep(17), dep(15)]
        self.assertEqual(self.queue()[14], "blocked\t14\t-\t-\tblocked by #15, #17")

    # 3
    def test_closed_blockers_resolve(self):
        self.add_issue(14)
        self.deps[14] = [dep(15, "closed", "completed"), dep(16, "closed", "completed")]
        self.assertEqual(self.queue()[14], "new\t14\t-\t-\tissue 14")

    # 4
    def test_blocker_closed_as_not_planned_resolves(self):
        self.add_issue(14)
        self.deps[14] = [dep(15, "closed", "not_planned")]
        self.assertEqual(self.queue()[14], "new\t14\t-\t-\tissue 14")

    # 5
    def test_cross_repo_blocker_is_ignored(self):
        self.add_issue(14)
        self.deps[14] = [dep(15, repo="https://api.github.com/repos/octo/other"),
                         dep(16, repo="https://api.github.com/repos/someone/factory")]
        self.assertEqual(self.queue()[14], "new\t14\t-\t-\tissue 14")

    # 6
    def test_two_issue_cycle_blocks_both(self):
        self.add_issue(14)
        self.add_issue(15)
        self.deps[14] = [dep(15)]
        self.deps[15] = [dep(14)]
        rows = self.queue()
        self.assertEqual(rows[14], "blocked\t14\t-\t-\tblocked by #15 (cycle: #14 → #15 → #14)")
        self.assertEqual(rows[15], "blocked\t15\t-\t-\tblocked by #14 (cycle: #15 → #14 → #15)")

    def test_longer_cycle_names_every_issue(self):
        for n in (14, 15, 16):
            self.add_issue(n)
        self.deps = {14: [dep(15)], 15: [dep(16)], 16: [dep(14)]}
        self.assertEqual(self.queue()[15], "blocked\t15\t-\t-\tblocked by #16 (cycle: #15 → #16 → #14 → #15)")

    # 7
    def test_blocked_issue_takes_no_slot(self):
        self.mod.CONCURRENCY = 1
        self.add_issue(14)
        self.add_issue(15)
        self.deps[14] = [dep(15)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.mod.cmd_queue()
        self.assertEqual(out.getvalue().splitlines(), [
            "new\t15\t-\t-\tissue 15",
            "blocked\t14\t-\t-\tblocked by #15",
        ])

    # 8
    def test_failed_lookup_fails_closed(self):
        self.mod.CONCURRENCY = 1
        self.add_issue(14)
        self.add_issue(15)
        self.deps[14] = "HTTP 502"
        rows = self.queue()
        self.assertTrue(rows[14].startswith("blocked\t14\t-\t-\tcouldn't read blockers: "), rows[14])
        self.assertIn("HTTP 502", rows[14])
        self.assertEqual(rows[15], "new\t15\t-\t-\tissue 15")

    def test_malformed_lookup_fails_closed(self):
        self.add_issue(14)
        self.deps[14] = {"message": "Not Found"}
        self.assertTrue(self.queue()[14].startswith("blocked\t14\t-\t-\tcouldn't read blockers: "))

    # 9
    def test_replied_needs_info_with_blocker_is_blocked(self):
        self.add_issue(14)
        self.add_job(14, "needs-info")
        self.deps[14] = [dep(15)]
        self.assertEqual(self.queue()[14], "blocked\t14\t014-x\tneeds-info\tauthor replied; blocked by #15")

    def test_wait_rows_make_no_lookup(self):
        self.add_issue(14)
        self.add_job(14, "needs-info")
        self.mod.replied_since_question = lambda issue: False
        self.assertTrue(self.queue()[14].startswith("wait\t14"))
        self.assertEqual(self.dep_calls(), [])

    # 10
    def test_running_job_with_late_blocker_keeps_going(self):
        self.mod.CONCURRENCY = 1
        self.add_issue(14)
        self.add_issue(16)
        job = self.add_job(14, "build", round=2)
        self.deps[14] = [dep(15)]
        rows = self.queue()
        self.assertEqual(rows[14], "resume\t14\t014-x\tbuild\tround 2; blocked by #15 (added after start)")
        self.assertTrue(rows[16].startswith("hold\t16"))
        saved = json.loads((self.mod.JOBS / "014-x" / "job.json").read_text())
        self.assertEqual(saved, job)
        self.assertEqual(self.labels, [])

    def test_running_job_lookup_failure_changes_nothing(self):
        self.add_issue(14)
        self.add_job(14, "review", round=2)
        self.deps[14] = "HTTP 502"
        self.assertEqual(self.queue()[14], "resume\t14\t014-x\treview\tround 2")

    # 11
    def test_create_refuses_blocked_issue(self):
        self.deps[14] = [dep(15), dep(16, "closed", "completed")]
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            self.mod.cmd_create("14")
        self.assertNotEqual(e.exception.code, 0)
        self.assertIn("issue #14 is blocked by #15;", err.getvalue())
        self.assertEqual(list(self.mod.JOBS.iterdir()), [])

    def test_create_refuses_when_lookup_fails(self):
        self.deps[14] = "HTTP 502"
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self.mod.cmd_create("14")
        self.assertIn("couldn't read blockers", err.getvalue())
        self.assertEqual(list(self.mod.JOBS.iterdir()), [])

    # 12
    def test_needs_info_to_intake_refuses_blocked_issue(self):
        job = self.add_job(14, "needs-info")
        d = self.mod.JOBS / job["id"]
        (d / "intake.md").write_text("DECISION: NEEDS-INFO\n\nWhich screens?\n")
        self.deps[14] = [dep(15)]
        with self.assertRaises(self.mod.Refused) as e:
            self.mod.advance(dict(job), "intake")
        self.assertIn("blocked by #15", str(e.exception))
        self.assertTrue((d / "intake.md").exists())
        self.assertEqual(list(d.glob("intake-*.md")), [])
        self.assertFalse((d / "issue.md").exists())
        self.assertEqual(self.labels, [])
        self.assertEqual(json.loads((d / "job.json").read_text()), job)

    # 13
    def test_lookup_reads_only_the_dependency_api_once(self):
        self.deps[14] = [dep(15), dep(13)]
        self.deps[20] = []
        self.assertEqual(self.mod.open_blockers(14), [13, 15])
        self.assertEqual(self.mod.open_blockers(20), [])
        self.assertEqual(self.fetched, [])
        self.assertEqual([c[2] for c in self.dep_calls()], [
            "repos/{owner}/{repo}/issues/14/dependencies/blocked_by?per_page=100",
            "repos/{owner}/{repo}/issues/20/dependencies/blocked_by?per_page=100",
        ])
        self.assertEqual(len([c for c in self.calls if c[:3] == ["gh", "repo", "view"]]), 1)

    def test_queue_never_fetches_new_issues(self):
        self.add_issue(14)
        self.add_issue(15)
        self.deps[14] = [dep(15)]
        self.queue()
        self.assertEqual(self.fetched, [])
        self.assertEqual(len(self.dep_calls()), 2)


if __name__ == "__main__":
    unittest.main()
