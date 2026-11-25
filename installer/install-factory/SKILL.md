---
name: install-factory
description: Install the software factory (GitHub issue → intake → spec → build → gate → review → approve, with /factory run) into a git repository. Use when the user says /install-factory, asks to set up, add or install the software factory in a repo, or wants a new project to have the factory.
---

# Install the software factory

This installs the factory snapshot bundled in `template/` (the reference factory:
agents as Orca workers, shared test locks, Orca tab view) into a target repo.

**How the installed factory runs.** One Claude Code session in the repo's main
checkout, started inside Orca, is the only orchestrator: it runs `/factory`, makes
decisions with the user, and keeps its context for that. Each job gets a temporary
worktree (`.worktrees/<job>`, an Orca tab nested under the main checkout) for as
long as it lives. Every factory agent (intake, spec-writer, builder, reviewers,
approver) runs in that worktree as its own Claude Code session in its own tab,
dispatched as an Orca orchestration worker: the user can open any of them and talk
to it, and the orchestrator only hears `worker_done` through Orca's mailbox.
Orca is therefore required. The installer is `scripts/install_factory.py`. It
is deterministic. You gather the answers and show the user what will change.

Paths below are relative to this skill directory, `~/.claude/skills/install-factory/`.

## 1. Preflight (read-only)

```bash
python3 ~/.claude/skills/install-factory/scripts/install_factory.py detect <repo>
```

It prints JSON: `is_git`, `origin`, `gh_authenticated`, `base_branch`,
`existing_factory`, `agents_md`, `stacks`, `proposed_checks`, `needs_input`, `warnings`.

Act on the warnings before asking anything else:

- **Not a git repo:** ask whether to `git init`. Don't run it without a yes.
- **No `origin` remote:** the factory needs GitHub. Ask whether to continue with
  `--no-labels`. PRs and labels won't work until a remote exists.
- **`gh` not authenticated:** tell the user to run `gh auth login` themselves.
- **`existing_factory: true`:** ask whether to re-apply with `--force`. The job
  state in `factory/jobs/` is kept either way.
- **Orca not installed or not running** (`orca` in the JSON): the factory can't
  dispatch agents without it. Tell the user; installing is fine, running isn't.

## 2. Questions, one at a time

Your global CLAUDE.md requires one question per message. Ask them in this order,
waiting for each answer:

1. **Checks.** Show `proposed_checks` as a list. Ask: confirm, or edit? For each
   item in `needs_input` (for example the Xcode scheme and destination), ask for
   its value and substitute it. The installer refuses any `<PLACEHOLDER>` left in a command.
   Then ask which checks use a resource that two runs on this machine must not share
   (a local test database, a simulator, a fixed port) and give those a `lock` name
   (e.g. `pg`, `sim`). They run through `factory/bin/locked`, which serializes them
   across every worktree and reports BUSY instead of a false failure.
2. **Project line.** Ask for one sentence on what the project is for. The intake
   agent uses it to decide what belongs in scope.
3. **Escalate paths.** Ask which paths must always wait for a human, for example
   `src/payments/*`. Default: none. These go into `auto_merge.escalate_paths`.
4. **Max risk for auto-merge.** Default `low`. Ask only if the user wants to change it.

Don't ask about base branch, concurrency or rounds. Use the defaults and say so.

## 3. Install

Write the answers to the scratchpad, then install:

```json
{
  "checks": [{"label": "python tests", "cmd": "pytest -q", "limit": 900, "lock": "pg"},
             {"label": "lint", "cmd": "ruff check .", "limit": 120}],
  "project_line": "One sentence from step 2.",
  "escalate_paths": [],
  "max_risk": "low"
}
```

```bash
python3 ~/.claude/skills/install-factory/scripts/install_factory.py install <repo> \
  --answers <scratchpad>/answers.json [--force] [--no-labels]
```

It writes:

- `factory/`: `bin/{job,gate,route,locked,job-watch}`, `dashboard.html`, `tests/`, a generated `config.json`
  and `checks.sh`, and `install.json` (the source snapshot and installer version)
- `.claude/agents/`: the 8 factory agents (`intake`, `spec-writer`, `builder`,
  `approver`, `reviewer-code`, `reviewer-security`, `reviewer-ux`, `reviewer-ui`)
- `.claude/skills/factory/SKILL.md`, the `/factory` orchestrator
- `.claude/skills/catchup/SKILL.md`, `/catchup`: a read-only digest of overnight factory activity
- `.claude/agent-usage.md`: the log, created or appended to
- `.claude/settings.local.json`: baseline deny rules for `rm`, `rmdir`, `unlink` and
  `find -delete`, merged in (never removed). Agents run as their own sessions in
  bypass mode, except intake (`dontAsk`, allowed only to read, write `intake.md` and
  report `worker_done`), so these rules are their guard.
- `AGENTS.md`: a "Software factory" section appended, or created if missing
- `.gitignore`: `.claude`, `CLAUDE.md`, `/factory/jobs/`, `/.worktrees/`, each added once
- GitHub labels `factory`, `factory:needs-info` and `factory:needs-human`, unless `--no-labels`

It then runs verification: the factory's policy tests, `bash -n` on `checks.sh`
and `factory/bin/job summary`. Exit 0 means all three passed. Read the JSON output.

## 4. Report

- Say what was written and the verify results.
- Say what was not committed. Give the user the `git add` line from the output.
  Don't commit or push unless they ask.
- Explain the git policy. `factory/`, `AGENTS.md` and `.gitignore` are meant to
  be committed. `.claude/` and `CLAUDE.md` are gitignored, so on a fresh clone the
  user must re-run this skill to get the `/factory` skill and agents. `AGENTS.md`
  is committed on purpose: job worktrees are git checkouts, so they never see a
  gitignored `CLAUDE.md`.
- Tell the user to restart Claude Code in the repo, in an Orca terminal on the main
  checkout. Newly added agents and skills register at session start, so `/factory`
  won't appear in a running session, and Orca can only wake an orchestrator that
  runs inside it.
- If the repo has shared test resources, tell the user to name them and their
  commands in AGENTS.md (the factory section explains `factory/bin/locked`).

## Known limits

- **Snapshot, not upstream.** The template is a copy of the reference factory, not a
  live pull from zazencodes-season-3. Upstream updates are a manual merge.
- **`checks.sh` is generated from detected commands.** Unknown stacks get no checks,
  and the installer exits with an error. Write the checks by hand, then re-run.
- **Detection misses repo-specific gates.** It proposes lint, format, test and build
  commands from manifests. It doesn't know about import linters (`lint-imports`), contract
  validators or fixture checks. Add those as extra checks in step 1.
- **Verification runs the policy tests and `job summary` only.** It does not run
  the project's checks. Run `bash factory/checks.sh` yourself to confirm them.
- **`--force` rewrites `config.json` and `checks.sh` from your answers.** Hand edits
  to either are lost. Keep a copy first, or edit after the install.
- **Prices in `config.json` are a dated copy** (2026-11-24). They drift from current pricing.
- **Deny rules are a guard, not a sandbox.** They match command text, so
  `bash -c 'rm …'` or `/bin/rm` can slip past them. Intake, the agent that reads
  untrusted issue text, is the one held to an allowlist.
- **Orca-specific.** Spawning, the agent tabs and the orchestrator's wake-ups all go
  through the `orca` CLI. Without Orca the factory can't dispatch agents.
- **Upstream vs local.** The template carries local changes to the
  zazencodes reference (auto-merge policy, Orca workers, locks, BUSY gate,
  `--match-head-commit`). Re-snapshot the template when the reference factory changes.

## Tests

```bash
python3 -m unittest discover -q -s ~/.claude/skills/install-factory/tests
```
