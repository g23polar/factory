"""Tests for factory/bin/rtk-hook: rtk rewrites only what it may, and never allows or asks.

No test runs the commands under test: the wrapper only rewrites JSON, and rtk's
`hook claude` only prints a rewrite. The real-rtk class is skipped without rtk.

Run from the repo root: python3 -m unittest discover -s factory/tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BIN_RTK_HOOK = REPO / "factory" / "bin" / "rtk-hook"

# Like `rtk hook claude`, but rewrites every command and says allow, which the wrapper must drop.
FAKE_RTK = """#!/usr/bin/env python3
import json, sys
if sys.argv[1:] != ["hook", "claude"]:
    sys.exit(2)
cmd = json.load(sys.stdin)["tool_input"]["command"]
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                  "permissionDecisionReason": "RTK auto-rewrite", "updatedInput": {"command": "rtk " + cmd}}}))
"""
FAKE_RTK_DENY = """#!/usr/bin/env python3
import json
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                  "permissionDecisionReason": "denied by fake"}}))
"""
# Leaves <its path>.ran behind, so a test can tell whether the wrapper ran it.
FAKE_RTK_MARKS = """#!/bin/sh
touch "$0.ran"
echo '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"command": "rtk x"}}}'
"""
FAKE_RTK_FAILS = "#!/bin/sh\necho '{}'\nexit 1\n"
FAKE_RTK_JUNK = "#!/bin/sh\necho 'not json'\n"

DELETING = ["rm -rf x", "rmdir x", "unlink x", "/bin/rm x", "find . -delete", "FOO=1 find . -delete",
            "LC_ALL=C find . -type f -delete", "ls && rm -rf x", "ls; rm x", "ls\nrm x", "ls | xargs rm",
            "echo $(rm x)", "git rm x"]
FULL_DIFF = ["git diff main...HEAD", "LC_ALL=C git diff main...HEAD", "git --no-pager diff",
             "git -C sub show HEAD", "git -c core.pager=cat diff", "git log -p", "git log -u -3", "git log --patch",
             "env FOO=1 git diff", "command git show", "gh pr diff 1"]
FACTORY = [f'{REPO}/factory/bin/job notify 014-x "m"', "factory/bin/job-diff 014-x",
           "orca orchestration send --from x --type worker_done --payload '{}'"]


def event(command, mode="bypassPermissions", cwd=REPO, tool="Bash"):
    data = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool, "cwd": str(cwd),
            "tool_input": {"command": command}}
    if mode is not None:
        data["permission_mode"] = mode
    return data


class Layout:
    """<tmp>/factory/bin/rtk-hook with <tmp>/factory/install.json pointing at an rtk."""

    def __init__(self, test, rtk_path):
        self.root = Path(tempfile.mkdtemp(prefix="rtk-hook-"))
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "factory" / "bin").mkdir(parents=True)
        self.hook = self.root / "factory" / "bin" / "rtk-hook"
        shutil.copy2(BIN_RTK_HOOK, self.hook)
        meta = {"source": "test"} if rtk_path is None else {"rtk": "wired", "rtk_path": str(rtk_path)}
        (self.root / "factory" / "install.json").write_text(json.dumps(meta))

    def run(self, data, cwd=REPO):
        stdin = data if isinstance(data, str) else json.dumps(data)
        res = subprocess.run([sys.executable, str(self.hook)], input=stdin, capture_output=True, text=True,
                             cwd=cwd, timeout=30)
        assert res.returncode == 0, res.stderr
        return res.stdout


class FakeRtkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Written once: macOS scans a new executable on its first run, which is slow.
        fakes = Path(tempfile.mkdtemp(prefix="fake-rtk-"))
        cls.addClassCleanup(shutil.rmtree, fakes, ignore_errors=True)
        cls.fakes = {}
        for name, script in (("rewrite", FAKE_RTK), ("deny", FAKE_RTK_DENY), ("fails", FAKE_RTK_FAILS),
                             ("junk", FAKE_RTK_JUNK)):
            path = fakes / name / "rtk"
            path.parent.mkdir()
            path.write_text(script)
            path.chmod(0o755)
            cls.fakes[name] = path

    def setUp(self):
        self.layout = Layout(self, self.fakes["rewrite"])

    def test_passthrough_commands_emit_nothing(self):
        for cmd in DELETING + FULL_DIFF + FACTORY:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.layout.run(event(cmd)), "")

    def test_unparseable_command_and_bad_input_emit_nothing(self):
        self.assertEqual(self.layout.run(event("echo 'unterminated")), "")
        self.assertEqual(self.layout.run("not json"), "")
        self.assertEqual(self.layout.run(event("git status", tool="Read")), "")

    def test_plain_command_is_rewritten_without_a_decision(self):
        out = json.loads(self.layout.run(event("git status")))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                      "updatedInput": {"command": "rtk git status"}}})

    def test_git_log_without_patch_is_rewritten(self):
        out = json.loads(self.layout.run(event("git -C sub log --oneline")))
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["command"], "rtk git -C sub log --oneline")

    def test_only_bypass_sessions_are_touched(self):
        self.assertEqual(self.layout.run(event("git status", mode="dontAsk")), "")
        self.assertEqual(self.layout.run(event("git status", mode="default")), "")
        self.assertEqual(self.layout.run(event("git status", mode=None)), "")

    def test_no_rtk_path_emits_nothing(self):
        self.assertEqual(Layout(self, None).run(event("git status")), "")

    def test_failing_or_junk_rtk_emits_nothing(self):
        for name in ("fails", "junk"):
            with self.subTest(fake=name):
                self.assertEqual(Layout(self, self.fakes[name]).run(event("git status")), "")

    def test_untrusted_rtk_path_is_never_run(self):
        outside = Path(tempfile.mkdtemp(prefix="fake-rtk-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        for name in ("rtk", "sh"):
            (outside / name).write_text(FAKE_RTK_MARKS)
            (outside / name).chmod(0o755)
        cases = {"relative": ("rtk", outside), "not named rtk": (outside / "sh", REPO)}
        for label, (rtk, cwd) in cases.items():
            with self.subTest(label):
                self.assertEqual(Layout(self, rtk).run(event("git status"), cwd=cwd), "")
        with self.subTest("inside the repo"):
            layout = Layout(self, None)
            inside = layout.root / "hook" / "rtk"
            inside.parent.mkdir()
            inside.write_text(FAKE_RTK_MARKS)
            inside.chmod(0o755)
            (layout.root / "factory" / "install.json").write_text(json.dumps({"rtk_path": str(inside)}))
            self.assertEqual(layout.run(event("git status")), "")
            self.assertFalse(Path(f"{inside}.ran").exists())
        self.assertEqual(sorted(p.name for p in outside.glob("*.ran")), [])

    def test_deny_is_forwarded(self):
        out = json.loads(Layout(self, self.fakes["deny"]).run(event("git status")))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": "denied by fake"}})


def real_rtk():
    path = shutil.which("rtk")
    if path is None:
        return None
    try:
        ok = subprocess.run([path, "gain"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return str(Path(path).resolve()) if ok else None


REAL_RTK = real_rtk()


@unittest.skipIf(REAL_RTK is None, "rtk is not installed or `rtk gain` fails")
class RealRtkTest(unittest.TestCase):
    def setUp(self):
        self.layout = Layout(self, REAL_RTK)

    def assert_no_allow_or_ask(self, out):
        if out:
            decision = json.loads(out)["hookSpecificOutput"].get("permissionDecision")
            self.assertNotIn(decision, ("allow", "ask"))

    def test_deleting_commands_are_not_rewritten(self):
        for cmd in ["rm -rf x", "rmdir x", "unlink x", "find . -delete", "FOO=1 find . -delete",
                    "LC_ALL=C find . -type f -delete", "ls && rm -rf x"]:
            with self.subTest(cmd=cmd):
                out = self.layout.run(event(cmd))
                self.assert_no_allow_or_ask(out)
                if out:
                    rewritten = json.loads(out)["hookSpecificOutput"].get("updatedInput", {}).get("command", "")
                    self.assertNotIn("rtk ", rewritten)

    def test_full_diffs_and_factory_commands_emit_nothing(self):
        for cmd in ["git diff main...HEAD", "LC_ALL=C git diff main...HEAD", "git --no-pager diff", "gh pr diff 1",
                    f'{REPO}/factory/bin/job notify 014-x "m"',
                    "orca orchestration send --from x --type worker_done --payload '{}'"]:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.layout.run(event(cmd)), "")

    def test_rewrite_never_allows_or_asks(self):
        self.assert_no_allow_or_ask(self.layout.run(event("git status")))

    def test_project_allow_rule_does_not_leak_through(self):
        project = Path(tempfile.mkdtemp(prefix="rtk-allow-"))
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(git *)"]}}))
        self.assert_no_allow_or_ask(self.layout.run(event("git status", cwd=project), cwd=project))


if __name__ == "__main__":
    unittest.main()
