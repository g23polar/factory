#!/usr/bin/env python3
"""Install the software factory into a git repository.

  install_factory.py detect  <repo>
      Read-only. Prints JSON: repo state, detected stacks, proposed checks.

  install_factory.py install <repo> --answers <file.json> [--force] [--no-labels]
      Writes the factory. The answers file is produced by the install-factory skill
      after the user confirms the checks. Shape:
        {
          "checks":        [{"label": str, "cmd": str, "limit": int,          # required, non-empty
                             "lock": str}, ...],   # lock optional: a shared resource (bin/locked)
          "project_line":  str,                                               # required
          "base_branch":   str,                                               # optional, default from detect
          "concurrency":   int,                                               # optional, default 1
          "max_rounds":    int,                                               # optional, default 3
          "max_risk":      "low" | "medium" | "high",                         # optional, default "low"
          "escalate_paths": [str, ...]                                        # optional, default []
        }

Refuses a repo that already has factory/ unless --force, which rewrites the factory
files and keeps factory/jobs/ (job state).
"""

import argparse
import datetime as dt
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
TEMPLATE = SKILL / "template"
SOURCE = "reference factory snapshot @ f79ea9d (2026-11-24)"
INSTALLER_VERSION = "2.0"

# Factory agents run as their own sessions in bypassPermissions (intake excepted), so the
# project's deny rules are their guard. Merged into .claude/settings.local.json; never removed.
DENY_RULES = ["Bash(rm *)", "Bash(rm:*)", "Bash(*&& rm *)", "Bash(*; rm *)", "Bash(*| xargs rm*)",
              "Bash(rmdir:*)", "Bash(unlink:*)", "Bash(find * -delete*)"]
LOCK_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")

# Prices copied from the reference factory's config.json, dated 2026-11-24. Update when they change.
PRICES = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_write_5m": 10.0, "cache_write_1h": 16.0, "cache_read": 0.2},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.2},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01},
}

# Agents copied into .claude/agents/. Project agents (ios-developer, architecture, ...) are not factory agents.
FACTORY_AGENTS = ["approver", "builder", "intake", "reviewer-code", "reviewer-security",
                  "reviewer-ui", "reviewer-ux", "spec-writer"]

LABELS = {
    "factory": ("0E8A16", "Queued for the software factory"),
    "factory:needs-info": ("FBCA04", "Factory is waiting for the author"),
    "factory:needs-human": ("D93F0B", "Factory escalated this job to a human"),
}

GITIGNORE_LINES = [".claude", "CLAUDE.md", "/factory/jobs/", "/.worktrees/"]

SKIP_DIRS = {"node_modules", ".venv", "venv", "build", "dist", "target", "DerivedData",
             "vendor", "factory", ".worktrees", "__pycache__", "Pods", "Carthage"}
PLACEHOLDER = re.compile(r"<[A-Z][A-Z_]*>")


def die(msg, code=1):
    print(f"install-factory: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd, cwd, check=False):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def is_git(repo):
    return run(["git", "rev-parse", "--is-inside-work-tree"], repo).returncode == 0 and \
        run(["git", "rev-parse", "--show-toplevel"], repo).stdout.strip() == str(repo)


def default_branch(repo):
    res = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().split("/", 1)[-1]
    res = run(["git", "branch", "--show-current"], repo)
    return res.stdout.strip() or "main"


# ---------- stack detection ----------

def stack_checks(repo):
    """Propose checks by looking at the repo root and its immediate subdirectories."""
    dirs = [repo] + sorted(p for p in repo.iterdir()
                           if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS)
    checks, stacks, needs_input = [], [], []

    def add(label, cmd, limit=600):
        checks.append({"label": label, "cmd": cmd, "limit": limit})

    for d in dirs:
        rel = d.relative_to(repo)
        prefix = "" if str(rel) == "." else f"cd {shlex.quote(str(rel))} && "
        where = "" if str(rel) == "." else f" ({rel})"

        pyproject = d / "pyproject.toml"
        if pyproject.exists() or (d / "requirements.txt").exists() or (d / "setup.py").exists():
            text = pyproject.read_text() if pyproject.exists() else ""
            runner = "uv run " if (d / "uv.lock").exists() else ""
            stacks.append("python" + where)
            if "ruff" in text or (d / "ruff.toml").exists() or (d / ".ruff.toml").exists():
                add(f"python lint{where}", f"{prefix}{runner}ruff check .")
                add(f"python format{where}", f"{prefix}{runner}ruff format --check .")
            if "pytest" in text or (d / "requirements.txt").exists() and "pytest" in (d / "requirements.txt").read_text():
                add(f"python tests{where}", f"{prefix}{runner}pytest -q", 900)

        pkg = d / "package.json"
        if pkg.exists():
            try:
                scripts = json.loads(pkg.read_text()).get("scripts", {})
            except json.JSONDecodeError:
                scripts = {}
            stacks.append("node" + where)
            installer = ("npm ci" if (d / "package-lock.json").exists() else
                         "pnpm install --frozen-lockfile" if (d / "pnpm-lock.yaml").exists() else
                         "yarn install --frozen-lockfile" if (d / "yarn.lock").exists() else "npm install")
            add(f"node install{where}", f"{prefix}{installer}", 900)
            for name in ("lint", "typecheck", "test"):
                if name in scripts:
                    add(f"node {name}{where}", f"{prefix}npm run --silent {name}", 900)

        xcodeprojs = sorted(d.glob("*.xcodeproj"))
        if xcodeprojs or (d / "Package.swift").exists():
            stacks.append("swift/xcode" + where)
            if xcodeprojs:
                add(f"xcode tests{where}",
                    f"{prefix}xcodebuild test -project {xcodeprojs[0].name} -scheme <SCHEME> "
                    f"-destination '<DESTINATION>'", 900)
                needs_input.append("xcode scheme and destination")
            else:
                add(f"swift tests{where}", f"{prefix}swift test", 900)

        if (d / "Cargo.toml").exists():
            stacks.append("rust" + where)
            add(f"cargo fmt{where}", f"{prefix}cargo fmt --check")
            add(f"cargo test{where}", f"{prefix}cargo test", 900)

        if (d / "go.mod").exists():
            stacks.append("go" + where)
            add(f"go vet{where}", f"{prefix}go vet ./...")
            add(f"go test{where}", f"{prefix}go test ./...", 900)

    return stacks, checks, needs_input


# ---------- detect ----------

def cmd_detect(args):
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        die(f"{repo} is not a directory")
    git = is_git(repo)
    origin = run(["git", "remote", "get-url", "origin"], repo).stdout.strip() if git else ""
    gh_ok = run(["gh", "auth", "status"], repo).returncode == 0
    stacks, checks, needs_input = stack_checks(repo) if git else ([], [], [])
    warnings = []
    if not git:
        warnings.append("not a git repository: run `git init` first (the factory uses git worktrees)")
    if not origin:
        warnings.append("no `origin` remote: PRs and labels need a GitHub remote")
    if not gh_ok:
        warnings.append("`gh` is not authenticated: run `gh auth login`")
    if not stacks:
        warnings.append("no known stack detected: the checks must be written by hand")
    if (repo / "factory").exists():
        warnings.append("factory/ already exists: install needs --force")
    orca_running = False
    if shutil.which("orca"):
        status = run(["orca", "status", "--json"], repo)
        try:
            orca_running = json.loads(status.stdout)["result"]["app"]["running"] is True
        except (json.JSONDecodeError, KeyError, TypeError):
            orca_running = False
        if not orca_running:
            warnings.append("Orca is installed but not running: factory agents run as Orca workers; open Orca")
    else:
        warnings.append("`orca` CLI not found: factory agents run as Orca workers, so install Orca first")
    print(json.dumps({
        "repo": str(repo),
        "is_git": git,
        "origin": origin or None,
        "gh_authenticated": gh_ok,
        "orca": {"installed": bool(shutil.which("orca")), "running": orca_running},
        "base_branch": default_branch(repo) if git else "main",
        "existing_factory": (repo / "factory").exists(),
        "agents_md": (repo / "AGENTS.md").exists(),
        "claude_md": (repo / "CLAUDE.md").exists(),
        "stacks": stacks,
        "proposed_checks": checks,
        "needs_input": needs_input,
        "warnings": warnings,
    }, indent=2))


# ---------- install ----------

def validate_answers(answers):
    if not isinstance(answers.get("checks"), list) or not answers["checks"]:
        die("answers.checks must be a non-empty list")
    for c in answers["checks"]:
        for key in ("label", "cmd", "limit"):
            if key not in c:
                die(f"check {c!r} is missing {key!r}")
        if PLACEHOLDER.search(c["cmd"]):
            die(f"check {c['label']!r} still has a placeholder: {c['cmd']}")
        if "lock" in c and not LOCK_NAME.match(str(c["lock"])):
            die(f"check {c['label']!r} has a bad lock name {c['lock']!r} (lowercase letters, digits, dashes)")
    if not str(answers.get("project_line", "")).strip():
        die("answers.project_line is required (one sentence on what the project is)")
    if answers.get("max_risk", "low") not in ("low", "medium", "high"):
        die("answers.max_risk must be low, medium or high")


def checks_sh(checks, base_branch):
    steps = []
    for c in checks:
        label = shlex.quote(c["label"])
        cmd = shlex.quote(c["cmd"])
        if c.get("lock"):
            # Shared resource: bin/locked waits for it, then guards the run (exit 75 = busy → gate BUSY).
            steps.append(f'step {label}\n"$LOCKED" --max {int(c["limit"])} {c["lock"]} bash -c {cmd}')
        else:
            steps.append(f'step {label}\nrun_guarded {int(c["limit"])} {label} bash -c {cmd}')
    body = "\n\n".join(steps)
    return f"""#!/usr/bin/env bash
# The repo's checks. Exit 0 only when the branch is fit to review.
#
# Generated by install-factory from the repo's stack and answers. Edit freely: this is
# the repo's own check suite. The gate runs it from the main checkout with the job's
# worktree as the working directory. Keep it fast, deterministic and non-interactive.
# Every step runs under a hard kill guard (macOS has no `timeout`).
set -euo pipefail

LOGDIR="$(mktemp -d "${{TMPDIR:-/tmp}}/factory-checks.XXXXXX")"
# Steps that use a shared test resource run through bin/locked (see AGENTS.md); a lock that
# stays busy exits 75, which this script passes on and the gate reports as BUSY, not FAIL.
LOCKED="$(cd "$(dirname "$0")" && pwd)/bin/locked"

step() {{ echo; echo "== $*"; }}

# run_guarded <seconds> <label> <cmd...>: run cmd, kill its whole tree after <seconds>.
kill_tree() {{
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null || true
}}
run_guarded() {{
  local limit=$1 label=$2; shift 2
  "$@" &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= limit )); then
      echo "!! $label exceeded ${{limit}}s; killing it" >&2
      kill_tree "$pid"
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 5; waited=$((waited + 5))
  done
  wait "$pid"
}}

step "factory: policy tests"
python3 -B -m unittest discover -q -s "$(dirname "$0")/tests"

{body}

echo; echo "factory/checks.sh: all checks passed"
"""


def write_config(answers, base_branch):
    config = {
        "concurrency": int(answers.get("concurrency", 1)),
        "max_rounds": int(answers.get("max_rounds", 3)),
        "base_branch": answers.get("base_branch") or base_branch,
        "merge_method": "merge",
        "labels": {"queue": "factory", "needs_info": "factory:needs-info", "needs_human": "factory:needs-human"},
        "routing": {"low": {"code": "opus"}, "medium": {"code": "opus"}, "high": {"code": "opus"}},
        "surface_reviewers": {"ui": {"ux": "sonnet", "ui": "opus"}, "security": {"security": "opus"}},
        "protected_paths": ["factory/checks.sh", "factory/config.json", "factory/bin/*", "factory/tests/*"],
        "ports": {"base": 5100, "step": 10},
        "prices_usd_per_mtok": PRICES,
        "auto_merge": {
            "max_risk": answers.get("max_risk", "low"),
            "deny_surfaces": ["security"],
            "escalate_paths": list(answers.get("escalate_paths", [])),
        },
    }
    return json.dumps(config, indent=2) + "\n"


def copy_factory_files(repo, force):
    fdir = repo / "factory"
    for name in ("job", "gate", "route", "locked", "job-watch"):
        dst = fdir / "bin" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE / "factory" / "bin" / name, dst)
        dst.chmod(0o755)
    (fdir / "tests").mkdir(parents=True, exist_ok=True)
    for src in (TEMPLATE / "factory" / "tests").glob("*.py"):
        shutil.copy2(src, fdir / "tests" / src.name)
    shutil.copy2(TEMPLATE / "factory" / "dashboard.html", fdir / "dashboard.html")


def copy_agents(repo, project_line):
    adir = repo / ".claude" / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    for name in FACTORY_AGENTS:
        text = (TEMPLATE / "claude-agents" / f"{name}.md").read_text()
        if name == "intake":
            text = text.replace("{{PROJECT_LINE}}", project_line.strip())
        assert "{{" not in text, f"unfilled placeholder in {name}.md"
        (adir / f"{name}.md").write_text(text)
    for skill in ("factory", "catchup"):
        skill_dir = repo / ".claude" / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE / "factory-skill" / skill / "SKILL.md", skill_dir / "SKILL.md")


def ensure_agents_section(repo):
    section = (TEMPLATE / "AGENTS-factory-section.md").read_text()
    path = repo / "AGENTS.md"
    if path.exists():
        text = path.read_text()
        if "## Software factory" in text:
            return "kept (section already present)"
        path.write_text(text.rstrip() + "\n\n" + section)
        return "appended section"
    path.write_text(f"# {repo.name} — rules for every agent\n\n{section}")
    return "created"


def ensure_deny_rules(repo):
    """Merge the baseline deny rules into .claude/settings.local.json; returns those added."""
    path = repo / ".claude" / "settings.local.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    deny = settings.setdefault("permissions", {}).setdefault("deny", [])
    added = [r for r in DENY_RULES if r not in deny]
    deny.extend(added)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return added


def ensure_gitignore(repo):
    path = repo / ".gitignore"
    text = path.read_text() if path.exists() else ""
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in present]
    if missing:
        sep = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + sep + "\n".join(missing) + "\n")
    return missing


def ensure_agent_log(repo, stamp, summary):
    path = repo / ".claude" / "agent-usage.md"
    entry = (f"\n## {stamp} — Software factory installed\n\n"
             f"**Task:** install the software factory ({SOURCE}) via install-factory {INSTALLER_VERSION}.\n\n"
             f"**Tools/skills:** install-factory skill, `install_factory.py`.\n\n"
             f"**Outcome:** {summary}\n")
    if path.exists():
        path.write_text(path.read_text().rstrip() + "\n" + entry)
    else:
        path.write_text("# Agent Usage Log\n\nComprehensive log of main-agent, subagent, and tool/skill use.\n" + entry)


def create_labels(repo):
    results = {}
    for name, (color, desc) in LABELS.items():
        res = run(["gh", "label", "create", name, "--color", color, "--description", desc, "--force"], repo)
        results[name] = "ok" if res.returncode == 0 else res.stderr.strip() or "failed"
    return results


def verify(repo):
    checks = [
        ("policy tests", ["python3", "-B", "-m", "unittest", "discover", "-q", "-s", "factory/tests"]),
        ("checks.sh syntax", ["bash", "-n", "factory/checks.sh"]),
        ("job summary", ["factory/bin/job", "summary"]),
    ]
    out = {}
    for label, cmd in checks:
        # Files, not pipes: a process a check leaves behind can't keep verify() waiting for EOF.
        with tempfile.TemporaryFile("w+") as stdout, tempfile.TemporaryFile("w+") as stderr:
            code = subprocess.run(cmd, cwd=repo, stdout=stdout, stderr=stderr).returncode
            stdout.seek(0)
            stderr.seek(0)
            err, text = stderr.read(), stdout.read()
        out[label] = "ok" if code == 0 else f"exit {code}: {(err or text).strip()[-300:]}"
    return out


def cmd_install(args):
    repo = Path(args.repo).resolve()
    if not is_git(repo):
        die(f"{repo} is not a git repository root (run `git init` there first)")
    answers = json.loads(Path(args.answers).read_text())
    validate_answers(answers)
    if (repo / "factory").exists() and not args.force:
        die("factory/ already exists; re-run with --force to rewrite the factory files (factory/jobs/ is kept)")

    base = answers.get("base_branch") or default_branch(repo)
    keep_jobs = (repo / "factory" / "jobs").exists()
    copy_factory_files(repo, args.force)
    (repo / "factory" / "config.json").write_text(write_config(answers, base))
    checks_path = repo / "factory" / "checks.sh"
    checks_path.write_text(checks_sh(answers["checks"], base))
    checks_path.chmod(0o755)
    copy_agents(repo, answers["project_line"])
    deny_added = ensure_deny_rules(repo)
    agents_md = ensure_agents_section(repo)
    added_ignores = ensure_gitignore(repo)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    install_meta = {"source": SOURCE, "installer": INSTALLER_VERSION, "installed_at": stamp}
    (repo / "factory" / "install.json").write_text(json.dumps(install_meta, indent=2) + "\n")

    labels = "skipped (--no-labels)" if args.no_labels else create_labels(repo)
    checks_result = verify(repo)
    summary = f"checks: {len(answers['checks'])} steps; AGENTS.md {agents_md}; " \
              f"jobs kept: {keep_jobs}; verify: {checks_result}"
    ensure_agent_log(repo, stamp, summary)

    print(json.dumps({
        "repo": str(repo),
        "written": ["factory/bin/{job,gate,route,locked,job-watch}", "factory/dashboard.html", "factory/tests/",
                    "factory/config.json", "factory/checks.sh", "factory/install.json",
                    ".claude/agents/ (8 factory agents)", ".claude/skills/factory/SKILL.md",
                    ".claude/skills/catchup/SKILL.md", ".claude/agent-usage.md", ".claude/settings.local.json (deny rules)"],
        "deny_rules_added": deny_added,
        "AGENTS.md": agents_md,
        "gitignore_added": added_ignores,
        "jobs_kept": keep_jobs,
        "labels": labels,
        "verify": checks_result,
        "base_branch": base,
        "uncommitted_next_step": "git add factory AGENTS.md .gitignore && git commit -m 'Add software factory'",
    }, indent=2))
    if any(v != "ok" for v in checks_result.values()):
        sys.exit(1)


def main(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("repo")
    d.set_defaults(fn=cmd_detect)
    i = sub.add_parser("install")
    i.add_argument("repo")
    i.add_argument("--answers", required=True)
    i.add_argument("--force", action="store_true")
    i.add_argument("--no-labels", action="store_true")
    i.set_defaults(fn=cmd_install)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
