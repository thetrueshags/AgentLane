# How AgentLane works and why

This is the reference for the pieces under `.harness/`, `bin/`, `tools/` and `.github/`. The
board coordinates any work whose deliverables live in Git, across tools and repeated sessions.

## Data on the `board` branch

- `tasks/<id>.json`: title, description, kind, paths, status (`open`, `claimed`, `review`, `blocked`, `done`),
  owner, landing history.
- `claims/<id>.json`: owner, agent, paths, branch, base, created, expiry, last heartbeat,
  extensions used, hot flag. One file per live claim; landing or releasing deletes it.
- `notes/<member>.jsonl` and `events/<member>.jsonl`: append-only, one file per member, so
  concurrent writers never touch the same file and the board branch never conflicts.
- `members/<name>.json`, `narrator.json` (the last event the Slack narrator posted), `BOARD.md`
  (rendered on every change so non-developers can read the board on github.com).

## Why claims are atomic without a server

`bin/board` runs every mutation as: fetch the board branch, reset the board worktree to it, apply
the change, commit, push. A git ref update is compare-and-swap. If another member pushed in
between, the push is rejected as non-fast-forward, the shim re-fetches and re-applies. Overlap is
re-checked on every retry against the fresh state, so two members can never both hold overlapping
paths. The board worktree lives inside `.git/board-wt` so it never appears in the project tree.

## Why landing is a rebase and a fast-forward push

Git provides serialization through updates to the shared branch: `board done` rebases the claim branch onto the
current `main`, runs the gate, and pushes with `HEAD:main`. A non-fast-forward rejection means
someone landed first; the shim rebases again, re-runs the gate, and retries, four times. No human
is in the path. The pre-push hook refuses any push to `main` that did not come through `board
done` (it looks for `BOARD_LAND=1` in the environment).

## Retries, heartbeats and contention

A lost board push is retried with exponential backoff and jitter, capped at eight seconds, up to
`push_retries` times. Heartbeats are coalesced: a claim refreshed within `heartbeat_min_seconds`
is not rewritten, and that window is clamped to a third of `stall_release_minutes` so coalescing
can never starve expiry. A transaction that changes no data does not render or push. These came
out of `sim/EVAL_001.md`, where immediate retries livelocked at eight agents.

## The gate

`.harness/gate/code.sh` auto-detects npm, pytest or unittest, mix, go and cargo and runs what it
finds. `.harness/gate/docs.sh` only checks for merge markers. Changed paths that all fall under
`docs_globs` in the config take the docs gate. Everything else takes the code gate, plus
`.harness/gate/smoke.sh` if the team adds one. `BOARD_PATHS` carries the changed paths, one per
line, for gates that want to scope themselves. The team edits these scripts to define acceptable
quality. Keep the code gate under three minutes: a slow gate makes agents queue and stack.

## The main-gate workflow

Every push to `main` runs the full code gate. Automatic recovery applies only when the shared
`board` branch exists; source-only repositories report a failing check without reverting.
On failure with a board present, the workflow first runs the gate at the
base of the pushed range; if main was already red there, it leaves the revert to the run that
broke it, so a clean landing on a red main is never reverted as collateral. Otherwise it reverts
the exact commit range that was pushed, pushes the revert, reopens the task with the failure
attached, and records an event the narrator turns into a Slack line naming the owner. This is the backstop for gaps in
the local gate and for flaky tests. It helps keep the shared branch usable throughout the project. Teams requiring reviewed reverts
should adapt this workflow before enabling it.

## The board workflow

Runs on every push to the board branch and every five minutes. It releases claims whose time is
up or whose heartbeat stopped for 15 minutes, posts one line per new event to Slack when
`SLACK_WEBHOOK_URL` is set, and re-renders `BOARD.md`. It never posts heartbeats, only state
changes: take, done, stuck, note, handoff, expired, reverted.

## Hooks in the agents

- Claude Code (`.claude/settings.json`): `SessionStart` prints the brief board status into the
  agent's context; `Stop` sends one heartbeat per turn. Both scripts never fail the agent.
- Other agents: the MCP server is enough for the tools. Heartbeats happen through any board call.
  A team can add a Stop-class hook per agent that runs `python3 bin/board heartbeat --quiet`.

## Backlog planning and different kinds of work

`board add "title" --description "expected outcome" --kind research` records an open task
without creating a claim, switching branches or reserving paths. Optional `--globs` can be
provided at planning time; paths are required when taking the task. Backlog items may overlap:
only active claims reserve files. `board_add` exposes the same workflow through MCP.

Kinds are code, docs, test, research, design, ops and other. Kind describes the work; changed
paths determine the gate. A research script outside `docs_globs` still takes the code gate.
Use notes for shared context and files for deliverables, including reports and design decisions.

## Review policy and outcomes

Set `landing_mode` to `direct` (default) or `pr` in `.harness/config.json`. `board done` honors
that policy; `--pr` can also request review on a direct-landing project. Both routes rebase,
check changed paths and run the quality gate. PR mode pushes the claim branch and opens a draft
GitHub PR. Only successful creation moves the task to `review` and releases its claim.
A failed gate or failed PR creation keeps the claim so the agent can recover.

After review, `board sync-reviews` (MCP: `board_sync_reviews`) reads PR state through GitHub CLI.
A merged PR becomes `done` only when its merge commit is reachable from the configured remote
main branch. This supports regular, squash and rebase merges as represented by GitHub's merge
commit result. A PR closed without merging reopens the task and preserves its last branch and
PR link. Open PRs stay in review. Sync is safe to repeat and checks task state again when writing.
If PR creation succeeded but recording it failed, inspect the existing PR and board before
retrying; duplicate-PR errors preserve the claim and need manual recovery.

The local hook still blocks direct pushes to main. A reviewer merges through the repository's
normal hosting workflow. Configure branch protection and CI to match that policy; the provided
main gate has an automatic-revert policy intended for projects that permit it.

## Time limits

Default TTL is 45 minutes, two extensions, hot claims 10 minutes with no extension. A claim is
flagged stalled after 10 minutes without a heartbeat and released after 15. These numbers are
judgment calls; change them in `.harness/config.json`.

## Stacking

A member may take a second claim based on their own unmerged claim branch. Landing the first
then the second works because `board done` rebases onto `main`. Stacking on another member's
branch is refused because their expiry or revert would strand you.

## Hot paths

`hot_paths` in the config lists files everyone touches: dependency manifests, the main router,
the app entry. Normal claims cannot cover them. A `hot` claim covers exactly those paths for ten
minutes so nobody holds them.

## What is enforced where

| Rule | Enforced by |
|---|---|
| No overlapping claims | `board take`, compare-and-swap on the board branch |
| Edit only claimed paths | pre-push hook, `board done` |
| No direct push to main | pre-push hook |
| Rebase before landing | `board done` |
| Gate before landing | `board done` |
| Gate after landing, revert on red | `.github/workflows/main-gate.yml` |
| Claim expiry | `board status`, `board expire`, `.github/workflows/board.yml` |
| One or two claims per member, own-branch stacking only | `board take` |
| Hot paths through micro-claims only | `board take`, `board done`, pre-push hook |
| Large pushes warned or refused | pre-push hook (`warn_lines`, `refuse_lines`) |

An agent that clones without running `board install` bypasses the pre-push hook. `board done`
still applies the same checks, and the main-gate workflow catches what slips past both.

## Simulation and evaluation

`tools/simulate` runs N synthetic agents with injected crashes and bad landings against a local
origin and audits the invariants afterwards. Results and defects found are in `sim/EVAL_001.md`.

## Known gaps

- Cowork and Claude Desktop read MCP config globally, not from the repo; the Copilot cloud agent
  is configured in the GitHub web UI; Windsurf has only a global MCP file. Inspect those tools' setup requirements when integrating them.
- Whether Cowork's sandbox can push to GitHub is unverified.
- The narrator is template-based, not an LLM summary. It is deliberately boring.
- PR review and merging remain manual. Run `board sync-reviews` after review outcomes;
  GitHub CLI and authentication are required. Automatic review syncing is not installed.
- `.harness/` and the `board_*` tools keep their existing names for compatibility.
- Supplied workflows target `main` and `board`; configuring different branch names in the CLI
  does not rewrite workflow filters or checkout refs.
- Hooks are local guardrails, not a security boundary against an agent that deliberately bypasses them.
