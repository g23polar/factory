# factory

> Install an agent-driven software factory in any GitHub repository: label an issue, get a reviewed pull request.

`factory` installs a pipeline of Claude Code agents into your repository. The pipeline turns
GitHub issues into merged pull requests. Each issue labelled `factory` becomes a job, and the
job moves through fixed stages:

```
issue → intake → spec → build → gate → review → approve → merge (or escalate to a human)
```

Each stage is done by a dedicated agent with a narrow job. Deterministic scripts move a job
between stages and refuse illegal moves. Your own test, lint and security commands decide
whether code is allowed through. The agents never decide that. Anything risky, or anything
your policy says needs a person, stops and waits for a human.

The factory is for individuals and teams who want to hand routine, well-specified work to
agents without giving up control over what reaches their main branch.

## Use cases

**Solo developers: clearing the backlog.**
Write issues the way you normally would, label the ones you're happy to delegate, and run
`/factory run`. Each issue gets its own branch, a spec with testable acceptance criteria, an
implementation, and a draft PR. You review PRs instead of writing them. Vague issues are not
guessed at: intake labels them `factory:needs-info` and asks its question on the issue.

**Small teams: a consistent path from issue to PR.**
Every change goes through the same steps: spec, build, your checks, code review, then an
approval decision with written reasoning. The artifacts (`intake.md`, `spec.md`, `build.md`,
the review files, `decision.md`) give reviewers the "why" behind each PR. You don't have to
reconstruct it from a diff.

**Companies: delegating under a policy you control.**
Auto-merge is opt-in and narrow. By default only `low`-risk jobs without a security surface
can merge without a person, and only when they touch no path you've marked for escalation
(for example `src/payments/*` or `.github/*`). Everything else is labelled
`factory:needs-human`, with the approver's reasoning attached to the PR. To make every PR wait
for a person, add `*` to `escalate_paths`.

**Open-source maintainers: triaging contributions safely.**
Issue text is untrusted input, so it is handled that way. The intake agent, which reads the
issue, runs with an allowlist: it can read, write its own `intake.md` and report back,
nothing else. The factory only picks up issues a maintainer has labelled `factory`. Intake
can reject out-of-scope requests, using a one-line project description you provide during
install.

**Repos with shared test resources.**
If your tests use a local database, a simulator or a fixed port, name that resource during
install. Every check that uses it then runs through a machine-wide lock. Parallel jobs wait
their turn, and a busy resource is reported as BUSY rather than as a false test failure.

## Installing / Getting started

Requirements:

- [Claude Code](https://claude.com/claude-code)
- [Orca](https://github.com/stablyai/orca). The factory dispatches each agent as an Orca worker in its own tab.
- A GitHub repository with an `origin` remote, and [`gh`](https://cli.github.com/) authenticated (`gh auth login`)
- `git`, `bash`, `python3`

The installer is a Claude Code skill. Install it once per machine:

```shell
git clone https://github.com/g23polar/factory.git
mkdir -p ~/.claude/skills
cp -R factory/installer/install-factory ~/.claude/skills/
```

Then open Claude Code at the root of the repository you want to add the factory to, and run:

```
/install-factory
```

The installer first runs a read-only preflight. It checks for a git repo, an `origin`
remote, `gh` authentication, Orca, and an existing factory, and it proposes checks based on
your stack. It then asks a few questions, one at a time, and writes the factory into the
repo. Finally it runs the factory's own policy tests to verify the install. Nothing is
committed or pushed for you.

Then commit the parts that are meant to be shared:

```shell
git add factory AGENTS.md .gitignore
git commit -m "Add software factory"
git push
```

Restart Claude Code in an Orca terminal on the repo's main checkout, label an issue
`factory`, and run:

```
/factory run          # work through every open `factory` issue
/factory run 42       # run one issue now
/factory hack 42      # make issue 42's small change straight on the base branch, no PR
/factory hack <text>  # file an issue from the text, then make it directly
/factory <anything>   # ask about jobs, give feedback, or file a feature
```

### Initial configuration

The installer asks for:

1. **Checks.** These are the commands every job must pass, such as tests, lint, format,
   build and secret scanning. They are proposed automatically for Python, Node, Swift/Xcode,
   Rust and Go, and you can edit or add to them. Mark any check that uses a shared resource
   with a lock name, for example `pg`.
2. **Project line.** One sentence on what the project is for. Intake uses it to judge scope.
3. **Escalate paths.** Glob patterns for files that must always wait for a human.
4. **Max risk for auto-merge.** The default is `low`.

The base branch, concurrency (1) and maximum review rounds (3) use defaults. You can change
them later in `factory/config.json`.

## How it works

One Claude Code session in your repo's main checkout is the **orchestrator**. It never
writes code, specs or reviews itself, except for a quick change you start with
`/factory hack`, which it makes and pushes to the base branch directly. Each job gets a
temporary git worktree (`.worktrees/<job-id>`), and every agent runs there as its own Claude
Code session in its own Orca tab. You can open any agent's tab and talk to it.

| Stage | Who | Output |
|---|---|---|
| Intake | `intake` agent | ACCEPT, NEEDS-INFO or REJECT; risk (`low`/`medium`/`high`) and surfaces (`ui`, `security`) |
| Route | `factory/bin/route` (script) | Which reviewers and models this job gets |
| Spec | `spec-writer` agent | `spec.md` with testable acceptance criteria |
| Build | `builder` agent | Commits on `factory/<job-id>` and a draft PR that closes the issue |
| Gate | `factory/bin/gate` (script) | Runs `factory/checks.sh`. On FAIL the builder gets the log and no reviewers run |
| Review | `reviewer-code` always; `reviewer-security` and `reviewer-ux`/`reviewer-ui` by surface | One review file per reviewer. Any CHANGES verdict sends the job back to the builder |
| Approve | `approver` agent, then the merge policy | APPROVE merges; ESCALATE labels the PR `factory:needs-human` |

A job gets at most `max_rounds` build → gate → review rounds before it goes to a human.

## Features

- **Issue-driven.** GitHub issues are the queue and labels are the state.
- **Per-job isolation.** Each job gets its own worktree, branch, PR and agent sessions.
- **Your checks are the gate.** Code that fails `checks.sh` never reaches a reviewer.
- **Risk-routed review.** Reviewers and models are chosen from intake's risk and surface rating.
- **Policy-enforced merging.** A script, not an agent, decides whether a PR may auto-merge.
- **Quick changes.** `/factory hack` skips intake, spec, review and the PR, so the change
  lands on the base branch unreviewed. Only you can start one, it must pass `checks.sh`, and
  branch protection refuses it like any direct push.
- **Protected factory.** No job can change `factory/bin/`, `factory/tests/`, `checks.sh` or `config.json`.
- **Audit trail.** Every decision is written to a file in the job folder and to the issue or PR.
- **Shared-resource locks.** Checks that share a resource are serialized across all worktrees.
- **Baseline guardrails.** Deny rules for `rm`, `rmdir`, `unlink` and `find -delete` are merged into the agents' settings.
- **Dashboard.** Run `cd factory && python3 -m http.server`, then open `http://localhost:8000/dashboard.html`.

## Configuration

All options live in `factory/config.json`. The installer generates it, and you can edit it
afterwards. Re-running the installer with `--force` regenerates `config.json` and
`checks.sh` from your answers and discards hand edits. Job state in `factory/jobs/` is always
kept.

#### `concurrency`
Type: `integer`. Default: `1`

How many jobs run at once.

#### `max_rounds`
Type: `integer`. Default: `3`

How many build → gate → review rounds a job gets before it is escalated to a human.

#### `base_branch`
Type: `string`. Default: the repository's default branch

The branch jobs start from and merge into.

#### `merge_method`
Type: `string`. Default: `"merge"`

How approved PRs are merged with `gh pr merge`.

#### `labels`
Type: `object`. Default: `{"queue": "factory", "needs_info": "factory:needs-info", "needs_human": "factory:needs-human"}`

The GitHub labels used as job state. The installer creates them.

#### `routing`
Type: `object`. Default: `opus` for the code reviewer at every risk level

Which model reviews code for each intake risk level.

#### `surface_reviewers`
Type: `object`. Default: `{"ui": {"ux": "sonnet", "ui": "opus"}, "security": {"security": "opus"}}`

Extra reviewers, and their models, added when intake flags a surface.

#### `protected_paths`
Type: `string[]`. Default: `["factory/checks.sh", "factory/config.json", "factory/bin/*", "factory/tests/*"]`

Files no job may change.

#### `ports`
Type: `object`. Default: `{"base": 5100, "step": 10}`

A port range given to each job, so parallel dev servers don't collide.

#### `auto_merge`
Type: `object`. Default: `{"max_risk": "low", "deny_surfaces": ["security"], "escalate_paths": []}`

When the factory may merge without a human:

- `max_risk`: the highest intake risk that may auto-merge (`low`, `medium` or `high`).
- `deny_surfaces`: surfaces that always need a human.
- `escalate_paths`: glob patterns. A diff that touches any of them needs a human. `"*"` makes every PR wait for a person.

```json
"auto_merge": {
  "max_risk": "low",
  "deny_surfaces": ["security"],
  "escalate_paths": ["src/payments/*", ".github/*"]
}
```

#### `factory/checks.sh`
The gate's commands, generated from your answers. Each step has a time limit. Steps that
use a shared resource run through `factory/bin/locked`.

## Contributing

Contributions are welcome. This repository runs its own factory, so the easiest way in is
to open an issue that describes the change. A maintainer may label it `factory` and let the
pipeline build it, or you can submit a pull request directly:

1. Fork the repository and create a feature branch.
2. Make your change, and run `bash factory/checks.sh` until it passes.
3. Open a pull request against `main` that explains what changed and why.

## Links

- Repository: https://github.com/g23polar/factory
- Issue tracker: https://github.com/g23polar/factory/issues
- Security issues: please don't open a public issue. Report them privately through the
  repository's **Security → Report a vulnerability** page.
- Based on the software factory from [zazencodes-season-3](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2)

## Licensing

Licensed under the [GNU General Public License v3.0](LICENSE).

This project is a modified derivative of the software factory in
[zazencodes-season-3](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2),
which is also GPL-3.0. See [NOTICE](NOTICE) for attribution and a summary of the changes.

The license covers this project's own code: the installer and the files it copies into
your repository. Using the factory to build your project does not by itself place your
project's code under the GPL. If your organization has a copyleft policy, check with them
before adopting it.
