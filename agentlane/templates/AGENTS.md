# AgentLane shared workflow

Read this file before working. Explain actions in plain language; people may interact only
through their agent. Their explicit instructions take precedence.

## Choose the operating mode

For **AgentLane source maintenance**, read CONTRIBUTING.md and use normal Git branches and
pull requests. Do not initialize a board or require membership just to contribute to AgentLane.
The source owner can direct ordinary maintenance commits. Publish only when authorized.

For **shared project work using AgentLane**, follow the rules below. Each concurrent worker
needs a unique member name and a separate clone. One person may direct several workers.
Use `agentlane --help`; `python3 bin/board` remains a compatibility entry point in source checkouts.
All supported agents use these same rules, through the CLI or `board_*` MCP tools.

## Start a session

1. Run `agentlane doctor` if setup is unclear. It is read-only and explains missing prerequisites.
2. Join with `agentlane join --name <worker> --agent <tool>` if local identity is missing.
   Ask the person for their name when needed. Never silently reuse another worker's identity.
3. Run `agentlane status` and read recent notes. Summarize current work and available tasks.
   Use `agentlane list --available` and `agentlane show TASK_ID` to inspect a candidate.

## Work in a lane

1. Inspect the board before work. Plan future work with `agentlane add`; planning reserves no files.
2. **Claim before editing:** `agentlane take TASK_ID`. Declare paths with `--paths` when needed.
   If another claim overlaps, report its owner and pick different work or request a handoff.
3. Work only on your own claim branch and inside its declared paths. If scope grows, release
   and re-take the task with the expanded paths before editing them, or take a disjoint stacked
   claim with `--base`. Do not edit while ownership is unconfirmed.
4. Make small commits and push your claim branch when authorized. Never force push.
5. Leave useful findings, decisions, blockers and test results with `agentlane note TASK_ID "text"`.
   Use `agentlane stuck` when blocked. Non-code deliverables belong in Git too.
6. Send `agentlane heartbeat` during active work, at least every few minutes. Read the project's
   timing settings in `.harness/config.json`; do not assume board reads refresh your claim.
7. Sync before landing: `agentlane sync`. Commit all work, then run `agentlane done TASK_ID`.
   It updates the branch, checks ownership and paths, runs the gate, and serializes landing.
   Fix failures and retry. **Never bypass the gate or push directly to main.**
8. Report success only after the command confirms a landing. In PR mode, report submission
   for review; use `agentlane sync --reviews` after merging before reporting completion.

## Recovery and shared files

- Stale claims can be re-taken after configured TTL or inactivity expiry. Their branches retain
  work. `agentlane release --force TASK_ID` only recovers a stale claim; it cannot steal a live one.
- Release work you cannot continue. Handoff requires pushing the branch successfully first.
- Files in `hot_paths` require a short `--hot` claim covering only those paths; no extensions.
- Never work on someone else's branch, fabricate tests, or report success before it is confirmed.
- Test a review branch in a fresh worktree; test the main branch for already-landed work.
  Record results and propose bugs with `agentlane add`.
