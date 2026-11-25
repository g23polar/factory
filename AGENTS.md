# factory — rules for every agent

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard: `cd factory && python3 -m http.server`, then
open http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.

One Claude Code session in the main checkout, run inside Orca, is the factory's only
orchestrator. Each job's worktree is an Orca tab nested under it (stage and verdicts in
its status and comment, a `factory` pane running `factory/bin/job-watch`). Every factory
agent runs there as its own Claude Code session (`claude --agent <name>`), dispatched by
`factory/bin/job spawn` as an Orca orchestration worker: the user may open any agent's
tab and talk to it. An agent that needs the user asks in its own tab and waits; it
reports to the orchestrator only with `worker_done`. Nothing outside the factory builds
code or PRs here, except a quick change the user starts with `/factory hack`, which the
orchestrator commits straight to the base branch after `checks.sh` passes.

Every checkout of this repo on the machine shares its test resources. Run anything that
uses a shared one through `factory/bin/locked --max <seconds> <resource> <cmd>` (e.g.
`pg` for a test database, `sim` for a simulator; `checks.sh` already does for its
steps). It waits up to 10 minutes, then exits 75 ("busy", naming the holder): try again
later, never bypass it. `--max` is the kill guard: past it the whole run is killed (exit
124). Keep it near twice a normal run.

Local policy (`factory/config.json` `auto_merge`, enforced by `factory/bin/job merge`):
the factory merges a PR by itself only when intake rated it `low` risk, it has no
`security` surface, and its diff touches no `escalate_paths` pattern. Everything
else waits for a human, labeled `factory:needs-human`. `factory/bin/`,
`factory/tests/`, `factory/checks.sh` and `factory/config.json` are protected: no
job may change them.
