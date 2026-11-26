"""Tests for scripts/install_factory.py. Each test builds a real git repo in a temp dir.

Run: python3 -m unittest discover -q -s ~/.claude/skills/install-factory/tests
"""

import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
INSTALLER = SKILL / "scripts" / "install_factory.py"

GOOD_ANSWERS = {
    "checks": [{"label": "smoke", "cmd": "test -f README.md", "limit": 60}],
    "project_line": "A tiny test project.",
    "escalate_paths": ["src/payments/*"],
    "max_risk": "low",
}


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def make_repo(files=None):
    repo = Path(tempfile.mkdtemp(prefix="ifactory-"))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    for rel, text in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def run_installer(*args, expect_ok=True, env=None):
    res = subprocess.run([sys.executable, str(INSTALLER), *args], capture_output=True, text=True, env=env)
    if expect_ok is True:
        assert res.returncode == 0, res.stdout + res.stderr
    elif expect_ok is False:
        assert res.returncode != 0, res.stdout + res.stderr
    return res


def write_answers(repo, answers):
    path = Path(tempfile.mkdtemp(prefix="ifactory-answers-")) / "answers.json"
    path.write_text(json.dumps(answers))
    return str(path)


def install(repo, answers=None, *extra, expect_ok=True, env=None):
    return run_installer("install", str(repo), "--answers", write_answers(repo, answers or GOOD_ANSWERS),
                         "--no-labels", *extra, expect_ok=expect_ok, env=env)


class DetectTest(unittest.TestCase):
    def detect(self, repo):
        res = run_installer("detect", str(repo))
        return json.loads(res.stdout)

    def test_empty_repo_has_no_stack_and_warns(self):
        info = self.detect(make_repo())
        self.assertEqual(info["stacks"], [])
        self.assertEqual(info["proposed_checks"], [])
        self.assertTrue(any("no known stack" in w for w in info["warnings"]))
        self.assertTrue(info["is_git"])
        self.assertFalse(info["existing_factory"])

    def test_python_project_with_ruff_and_pytest(self):
        repo = make_repo({"pyproject.toml": '[project]\nname="x"\n[tool.ruff]\n[project.optional-dependencies]\ntest=["pytest"]\n'})
        cmds = [c["cmd"] for c in self.detect(repo)["proposed_checks"]]
        self.assertIn("ruff check .", cmds)
        self.assertIn("ruff format --check .", cmds)
        self.assertIn("pytest -q", cmds)

    def test_uv_project_uses_uv_run(self):
        repo = make_repo({"pyproject.toml": '[tool.ruff]\n', "uv.lock": "x"})
        cmds = [c["cmd"] for c in self.detect(repo)["proposed_checks"]]
        self.assertIn("uv run ruff check .", cmds)

    def test_nested_backend_gets_cd_prefix(self):
        repo = make_repo({"server/pyproject.toml": '[tool.ruff]\n[tool.pytest]\npytest = "x"\n'})
        info = self.detect(repo)
        self.assertIn("python (server)", info["stacks"])
        cmds = [c["cmd"] for c in info["proposed_checks"]]
        self.assertIn("cd server && ruff check .", cmds)

    def test_node_only_proposes_scripts_that_exist(self):
        pkg = json.dumps({"scripts": {"test": "jest", "build": "tsc"}})
        repo = make_repo({"package.json": pkg, "package-lock.json": "{}"})
        cmds = [c["cmd"] for c in self.detect(repo)["proposed_checks"]]
        self.assertIn("npm ci", cmds)
        self.assertIn("npm run --silent test", cmds)
        self.assertNotIn("npm run --silent build", cmds)

    def test_xcode_project_needs_scheme_and_destination(self):
        repo = make_repo({"App.xcodeproj/project.pbxproj": "x"})
        info = self.detect(repo)
        self.assertIn("xcode scheme and destination", info["needs_input"])
        self.assertTrue(any("<SCHEME>" in c["cmd"] for c in info["proposed_checks"]))

    def test_factory_dir_is_reported(self):
        repo = make_repo({"factory/config.json": "{}"})
        info = self.detect(repo)
        self.assertTrue(info["existing_factory"])
        self.assertTrue(any("--force" in w for w in info["warnings"]))


class InstallValidationTest(unittest.TestCase):
    def test_rejects_empty_checks(self):
        repo = make_repo()
        run_installer("install", str(repo), "--answers", write_answers(repo, {**GOOD_ANSWERS, "checks": []}),
                      "--no-labels", expect_ok=False)
        self.assertFalse((repo / "factory").exists())

    def test_rejects_unresolved_placeholder(self):
        repo = make_repo()
        bad = {**GOOD_ANSWERS, "checks": [{"label": "x", "cmd": "xcodebuild -scheme <SCHEME>", "limit": 60}]}
        install(repo, bad, expect_ok=False)
        self.assertFalse((repo / "factory").exists())

    def test_rejects_missing_project_line(self):
        repo = make_repo()
        install(repo, {**GOOD_ANSWERS, "project_line": "  "}, expect_ok=False)

    def test_rejects_bad_max_risk(self):
        repo = make_repo()
        install(repo, {**GOOD_ANSWERS, "max_risk": "critical"}, expect_ok=False)

    def test_rejects_non_git_directory(self):
        plain = Path(tempfile.mkdtemp(prefix="ifactory-plain-"))
        run_installer("install", str(plain), "--answers", write_answers(plain, GOOD_ANSWERS),
                      "--no-labels", expect_ok=False)


class FactoryV2Test(unittest.TestCase):
    """Install-factory 2.0: Orca workers, shared locks, deny rules."""

    def test_new_scripts_are_installed_executable(self):
        repo = make_repo()
        install(repo)
        for name in ("locked", "job-watch", "job-diff"):
            self.assertTrue(os.access(repo / "factory/bin" / name, os.X_OK), name)
        for name in ("test_orca_view.py", "test_orca_workers.py", "test_blockers.py", "test_job_diff.py", "test_pr_label.py"):
            self.assertTrue((repo / "factory/tests" / name).exists(), name)

    def test_reviewers_and_approver_read_the_diff_from_job_diff(self):
        repo = make_repo()
        install(repo)
        for name in ("reviewer-code", "reviewer-security", "reviewer-ux", "reviewer-ui", "approver"):
            text = (repo / ".claude/agents" / f"{name}.md").read_text()
            self.assertIn("factory/bin/job-diff", text, name)
            self.assertNotIn("git diff <base>...HEAD", text, name)

    def test_locked_checks_run_through_bin_locked(self):
        repo = make_repo()
        answers = {**GOOD_ANSWERS, "checks": [
            {"label": "db tests", "cmd": "test -f README.md", "limit": 900, "lock": "pg"},
            {"label": "lint", "cmd": "true", "limit": 60}]}
        install(repo, answers)
        checks = (repo / "factory/checks.sh").read_text()
        self.assertIn('"$LOCKED" --max 900 pg bash -c', checks)
        self.assertIn("run_guarded 60 lint", checks)
        env = {**os.environ, "FACTORY_LOCK_DIR": tempfile.mkdtemp()}
        res = subprocess.run(["bash", "factory/checks.sh"], cwd=repo, env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout[-800:] + res.stderr[-800:])

    def test_bad_lock_name_is_refused(self):
        repo = make_repo()
        answers = {**GOOD_ANSWERS, "checks": [{"label": "x", "cmd": "true", "limit": 60, "lock": "Bad Name"}]}
        install(repo, answers, expect_ok=False)

    def test_deny_rules_are_merged_not_replaced(self):
        repo = make_repo()
        settings = repo / ".claude/settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(curl:*)"], "allow": ["Read"]}}))
        out = json.loads(install(repo).stdout)
        perms = json.loads(settings.read_text())["permissions"]
        self.assertIn("Bash(curl:*)", perms["deny"])
        self.assertIn("Bash(rm *)", perms["deny"])
        self.assertEqual(perms["allow"], ["Read"])
        self.assertIn("Bash(rm *)", out["deny_rules_added"])
        out2 = json.loads(install(repo, None, "--force").stdout)
        self.assertEqual(out2["deny_rules_added"], [])  # idempotent

    def test_agents_md_describes_orca_workers_and_locks(self):
        repo = make_repo()
        install(repo)
        text = (repo / "AGENTS.md").read_text()
        self.assertIn("factory/bin/job spawn", text)
        self.assertIn("factory/bin/locked", text)

    def test_skill_and_agents_md_document_hack(self):
        repo = make_repo()
        install(repo)
        self.assertIn("/factory hack", (repo / ".claude/skills/factory/SKILL.md").read_text())
        self.assertIn("/factory hack", (repo / "AGENTS.md").read_text())

    def test_skill_documents_background_waiter(self):
        repo = make_repo()
        install(repo)
        skill = (repo / ".claude/skills/factory/SKILL.md").read_text()
        for text in ("orca orchestration check --run <run> --wait --timeout-ms 3600000 --json",
                     "run_in_background", "orca orchestration run-current --json", "timedOut"):
            self.assertIn(text, skill)

    def test_skill_documents_blocked_issues(self):
        repo = make_repo()
        install(repo)
        skill = (repo / ".claude/skills/factory/SKILL.md").read_text()
        self.assertIn("`blocked`", skill)
        self.assertIn("blocked", (repo / ".claude/skills/catchup/SKILL.md").read_text())

    def test_detect_reports_orca(self):
        repo = make_repo()
        out = json.loads(run_installer("detect", str(repo)).stdout)
        self.assertEqual(set(out["orca"]), {"installed", "running"})


class InstallTest(unittest.TestCase):
    def test_writes_factory_and_verifies(self):
        repo = make_repo()
        res = install(repo)
        out = json.loads(res.stdout)
        self.assertEqual(out["verify"], {"policy tests": "ok", "checks.sh syntax": "ok", "job summary": "ok"})
        for rel in ("factory/bin/job", "factory/bin/gate", "factory/bin/route", "factory/dashboard.html",
                    "factory/checks.sh", "factory/config.json", "factory/install.json",
                    ".claude/skills/factory/SKILL.md", ".claude/skills/catchup/SKILL.md",
                    ".claude/agent-usage.md"):
            self.assertTrue((repo / rel).exists(), rel)
        self.assertTrue(os.access(repo / "factory/bin/job", os.X_OK))
        self.assertTrue(os.access(repo / "factory/checks.sh", os.X_OK))

    def test_installs_catchup_skill_as_read_only_digest(self):
        repo = make_repo()
        install(repo)
        text = (repo / ".claude/skills/catchup/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: catchup\n"))
        self.assertIn("You\nchange nothing", text)

    def test_installs_all_eight_factory_agents_and_no_project_agents(self):
        repo = make_repo()
        install(repo)
        names = sorted(p.name for p in (repo / ".claude/agents").glob("*.md"))
        self.assertEqual(names, sorted(f"{n}.md" for n in [
            "approver", "builder", "intake", "reviewer-code", "reviewer-security",
            "reviewer-ui", "reviewer-ux", "spec-writer"]))

    def test_intake_gets_project_line_and_no_placeholder_remains(self):
        repo = make_repo()
        install(repo)
        intake = (repo / ".claude/agents/intake.md").read_text()
        self.assertIn("A tiny test project.", intake)
        for path in (repo / ".claude/agents").glob("*.md"):
            self.assertNotIn("{{", path.read_text(), path.name)

    def test_config_reflects_answers(self):
        repo = make_repo()
        install(repo)
        config = json.loads((repo / "factory/config.json").read_text())
        self.assertEqual(config["auto_merge"], {"max_risk": "low", "deny_surfaces": ["security"],
                                                "escalate_paths": ["src/payments/*"]})
        self.assertEqual(config["base_branch"], "main")
        self.assertEqual(config["concurrency"], 1)

    def test_generated_checks_pass_and_fail_correctly(self):
        repo = make_repo()
        install(repo)
        ok = subprocess.run(["bash", "factory/checks.sh"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("all checks passed", ok.stdout)

        bad = {**GOOD_ANSWERS, "checks": [{"label": "fails", "cmd": "false", "limit": 60}]}
        repo2 = make_repo()
        install(repo2, bad)
        res = subprocess.run(["bash", "factory/checks.sh"], cwd=repo2, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)

    def test_quoting_survives_single_quotes_in_commands(self):
        repo = make_repo()
        quoted = {**GOOD_ANSWERS, "checks": [{"label": "it's quoted", "cmd": "test \"$(echo 'ok')\" = ok", "limit": 60}]}
        install(repo, quoted)
        res = subprocess.run(["bash", "factory/checks.sh"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_refuses_existing_factory_without_force(self):
        repo = make_repo()
        install(repo)
        install(repo, expect_ok=False)

    def test_force_rewrites_but_keeps_jobs(self):
        repo = make_repo()
        install(repo)
        # A sentinel job folder: --force must not touch anything under factory/jobs/.
        job_file = repo / "factory/jobs/001-x/intake.md"
        job_file.parent.mkdir(parents=True)
        job_file.write_text("keep me\n")
        install(repo, GOOD_ANSWERS, "--force")
        self.assertEqual(job_file.read_text(), "keep me\n")
        self.assertTrue((repo / "factory/bin/job").exists())

    def test_gitignore_entries_added_once(self):
        repo = make_repo()
        (repo / ".gitignore").write_text("node_modules/\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "ignore")
        install(repo)
        install(repo, GOOD_ANSWERS, "--force")
        lines = (repo / ".gitignore").read_text().splitlines()
        for entry in (".claude", "CLAUDE.md", "/factory/jobs/", "/.worktrees/"):
            self.assertEqual(lines.count(entry), 1, entry)
        self.assertIn("node_modules/", lines)
        # job create links each worktree's .claude to the main checkout's; git must ignore the link too
        (repo / "wt").mkdir()
        (repo / "wt" / ".claude").symlink_to(repo / ".claude")
        res = subprocess.run(["git", "check-ignore", "-q", "wt/.claude"], cwd=repo)
        self.assertEqual(res.returncode, 0)

    def test_agents_md_created_when_missing(self):
        repo = make_repo()
        install(repo)
        text = (repo / "AGENTS.md").read_text()
        self.assertIn("## Software factory", text)

    def test_agents_md_section_appended_once_and_existing_text_kept(self):
        repo = make_repo({"AGENTS.md": "# Rules\n\nBe kind.\n"})
        install(repo)
        install(repo, GOOD_ANSWERS, "--force")
        text = (repo / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("# Rules\n\nBe kind."))
        self.assertEqual(text.count("## Software factory"), 1)

    def test_agent_log_is_appended_not_replaced(self):
        repo = make_repo({".claude/agent-usage.md": "# Existing log\n\nentry one\n"})
        install(repo)
        text = (repo / ".claude/agent-usage.md").read_text()
        self.assertIn("entry one", text)
        self.assertIn("Software factory installed", text)


FAKE_RTK = """#!/bin/sh
case "$1" in
  --version) echo "rtk 9.9.9" ;;
  gain) [ -n "$FAKE_RTK_GAIN_FAILS" ] && exit 1; echo "no savings yet" ;;
  *) exit 2 ;;
esac
"""
RTK_DENY = ["Bash(rtk find * -delete*)", "Bash(*rtk find * -delete*)", "Bash(*find * -delete*)",
            "Bash(rtk run *)", "Bash(rtk proxy *)"]
BASELINE_DENY = ["Bash(rm *)", "Bash(rm:*)", "Bash(*&& rm *)", "Bash(*; rm *)", "Bash(*| xargs rm*)",
                 "Bash(rmdir:*)", "Bash(unlink:*)", "Bash(find * -delete*)"]


class RtkTest(unittest.TestCase):
    """Optional rtk: detection, the `rtk` answer, and the hook and deny rules it wires."""

    def env(self, rtk=True, user_hook=False, gain_fails=False):
        """PATH with system tools plus a fake rtk (or none), and a temp HOME (optionally with a user rtk hook)."""
        home = Path(tempfile.mkdtemp(prefix="ifactory-home-"))
        bindir = Path(tempfile.mkdtemp(prefix="ifactory-bin-"))
        for tool in ("git", "gh", "bash", "sh", "python3"):
            found = shutil.which(tool)
            if found:
                (bindir / tool).symlink_to(found)
        if rtk:
            (bindir / "rtk").write_text(FAKE_RTK)
            (bindir / "rtk").chmod(0o755)
        if user_hook:
            (home / ".claude").mkdir()
            (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/rtk hook claude"}]}]}}))
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home)}
        if gain_fails:
            env["FAKE_RTK_GAIN_FAILS"] = "1"
        return env

    def detect(self, env):
        return json.loads(run_installer("detect", str(make_repo()), env=env).stdout)

    def install(self, answers, *extra, env, repo=None, expect_ok=True):
        repo = repo or make_repo()
        res = install(repo, answers, *extra, env=env, expect_ok=expect_ok)
        return repo, res

    def read(self, repo):
        meta = json.loads((repo / "factory/install.json").read_text())
        settings = json.loads((repo / ".claude/settings.local.json").read_text())
        return meta, settings

    def hook_commands(self, settings):
        return [h["command"] for e in settings.get("hooks", {}).get("PreToolUse", []) for h in e["hooks"]]

    def test_detect_reports_working_rtk_with_path_and_version(self):
        env = self.env()
        rtk = self.detect(env)["rtk"]
        self.assertTrue(rtk["installed"])
        self.assertEqual(rtk["path"], str(Path(shutil.which("rtk", path=env["PATH"])).resolve()))
        self.assertEqual(rtk["version"], "9.9.9")
        self.assertFalse(rtk["global_hook"])

    def test_detect_treats_failing_gain_as_not_installed(self):
        rtk = self.detect(self.env(gain_fails=True))["rtk"]
        self.assertEqual(rtk, {"installed": False, "path": None, "version": None, "global_hook": False})

    def test_detect_without_rtk_adds_no_warning(self):
        info = self.detect(self.env(rtk=False))
        self.assertFalse(info["rtk"]["installed"])
        self.assertFalse(any("rtk" in w for w in info["warnings"]))

    def test_detect_reports_user_level_hook(self):
        self.assertTrue(self.detect(self.env(user_hook=True))["rtk"]["global_hook"])

    def test_absent_rtk_wires_nothing(self):
        repo, res = self.install({**GOOD_ANSWERS, "rtk": True}, env=self.env(rtk=False))
        meta, settings = self.read(repo)
        self.assertEqual(meta["rtk"], "absent")
        self.assertNotIn("rtk_path", meta)
        self.assertEqual(settings, {"permissions": {"deny": BASELINE_DENY}})
        self.assertNotIn("rtk", json.loads(res.stdout))

    def test_declined_wires_nothing(self):
        for answers in (GOOD_ANSWERS, {**GOOD_ANSWERS, "rtk": False}):
            with self.subTest(rtk=answers.get("rtk")):
                repo, res = self.install(answers, env=self.env())
                meta, settings = self.read(repo)
                self.assertEqual(meta["rtk"], "declined")
                self.assertEqual(settings, {"permissions": {"deny": BASELINE_DENY}})
                self.assertNotIn("rtk", json.loads(res.stdout))

    def assert_wired_once(self, repo):
        meta, settings = self.read(repo)
        self.assertEqual(meta["rtk"], "wired")
        self.assertEqual(meta["rtk_version"], "9.9.9")
        self.assertTrue(os.path.isabs(meta["rtk_path"]))
        commands = self.hook_commands(settings)
        self.assertEqual(len(commands), 1)
        argv = shlex.split(commands[0])
        self.assertEqual(argv, [str(repo.resolve() / "factory/bin/rtk-hook")])
        self.assertTrue(os.access(argv[0], os.X_OK))
        deny = settings["permissions"]["deny"]
        self.assertEqual(sorted(deny), sorted(BASELINE_DENY + RTK_DENY))
        return settings

    def test_accept_wires_hook_and_deny_rules(self):
        repo, res = self.install({**GOOD_ANSWERS, "rtk": True}, env=self.env())
        out = json.loads(res.stdout)
        self.assertEqual(out["rtk"], "wired")
        self.assertTrue(any("rtk-hook" in w and "PreToolUse" in w for w in out["written"]))
        self.assertTrue(set(RTK_DENY) <= set(out["deny_rules_added"]))
        self.assert_wired_once(repo)

    def test_rerun_is_idempotent(self):
        env = self.env()
        answers = {**GOOD_ANSWERS, "rtk": True}
        repo, _ = self.install(answers, env=env)
        before = self.assert_wired_once(repo)
        self.install(answers, env=env, repo=repo, expect_ok=False)
        self.assertEqual(self.read(repo)[1], before)
        for _ in range(2):
            self.install(answers, "--force", env=env, repo=repo)
            self.assert_wired_once(repo)

    def test_existing_hooks_and_settings_are_kept(self):
        repo = make_repo()
        existing = {
            "permissions": {"allow": ["Read"], "deny": ["Bash(curl:*)"]},
            "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "echo pre"}]}],
                      "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo post"}]}]},
            "model": "opus",
        }
        path = repo / ".claude/settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(existing))
        self.install({**GOOD_ANSWERS, "rtk": True}, env=self.env(), repo=repo)
        settings = json.loads(path.read_text())
        self.assertEqual(settings["permissions"]["allow"], ["Read"])
        self.assertIn("Bash(curl:*)", settings["permissions"]["deny"])
        self.assertEqual(settings["hooks"]["PostToolUse"], existing["hooks"]["PostToolUse"])
        self.assertEqual(settings["hooks"]["PreToolUse"][0], existing["hooks"]["PreToolUse"][0])
        self.assertEqual(self.hook_commands(settings), ["echo pre", shlex.quote(str(repo.resolve() / "factory/bin/rtk-hook"))])
        self.assertEqual(settings["model"], "opus")

    def test_user_level_hook_adds_only_deny_rules(self):
        repo, res = self.install({**GOOD_ANSWERS, "rtk": True}, env=self.env(user_hook=True))
        meta, settings = self.read(repo)
        self.assertEqual(meta["rtk"], "already-global")
        self.assertNotIn("rtk_path", meta)
        self.assertNotIn("hooks", settings)
        self.assertTrue(set(RTK_DENY) <= set(settings["permissions"]["deny"]))
        self.assertEqual(json.loads(res.stdout)["rtk"], "already-global")

    def test_config_protects_rtk_dir_and_install_json(self):
        repo, _ = self.install(GOOD_ANSWERS, env=self.env(rtk=False))
        config = json.loads((repo / "factory/config.json").read_text())
        self.assertIn(".rtk/**", config["protected_paths"])
        self.assertIn("factory/install.json", config["protected_paths"])

    def test_wrapper_installed_even_when_absent(self):
        repo, _ = self.install(GOOD_ANSWERS, env=self.env(rtk=False))
        self.assertTrue(os.access(repo / "factory/bin/rtk-hook", os.X_OK))
        self.assertTrue((repo / "factory/tests/test_rtk_hook.py").exists())

    def test_non_boolean_answer_is_refused(self):
        repo, res = self.install({**GOOD_ANSWERS, "rtk": "yes"}, env=self.env(), expect_ok=False)
        self.assertIn("answers.rtk", res.stderr)
        self.assertFalse((repo / "factory").exists())


class VerifyTest(unittest.TestCase):
    def test_a_stray_process_does_not_stall_verify(self):
        repo = make_repo()
        install(repo)
        # A policy test that leaves a sleep behind holding the inherited stdout/stderr.
        pidfile = repo / "stray.pid"
        self.addCleanup(self.kill_pidfile, pidfile)
        (repo / "factory/tests/test_stray.py").write_text(
            "import subprocess\nimport unittest\n\n\n"
            "class StrayTest(unittest.TestCase):\n"
            "    def test_leaves_a_sleep_behind(self):\n"
            f"        subprocess.run(['bash', '-c', 'sleep 30 & echo $! > {pidfile}'], check=True)\n")
        spec = importlib.util.spec_from_file_location("install_factory", INSTALLER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        start = time.monotonic()
        out = mod.verify(repo)
        self.assertLess(time.monotonic() - start, 25)  # the stray sleeps 30s
        self.assertEqual(out["policy tests"], "ok")
        self.assertTrue(pidfile.exists())  # the stray really ran

    def kill_pidfile(self, pidfile):
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
