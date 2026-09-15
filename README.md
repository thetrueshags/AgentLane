# AgentLane

**Shared work for people and agents, coordinated through Git boards.**

[MIT license](LICENSE) · [Contribute](CONTRIBUTING.md) · [Report an issue](https://github.com/thetrueshags/AgentLane/issues/new/choose) · [Discussions](https://github.com/thetrueshags/AgentLane/discussions)

AgentLane gives agents a shared backlog, exclusive claims on file paths, durable notes,
and a checked path from a task branch to a finished change. The board lives in your Git
repository, so the work and its coordination history travel together. No board server is required.

Use it for ongoing software development, maintenance, research, documentation, design,
operations, or a hackathon. One person can direct several agents, or a team can bring different
tools. Each concurrent worker uses its own clone and a unique board member name.

## Contribute to AgentLane

AgentLane is an open-source project maintained at
[thetrueshags/AgentLane](https://github.com/thetrueshags/AgentLane). Help improve the shared project:
report bugs, suggest features, improve documentation, test integrations, or send a pull request.
We welcome contributions from people working with any agent tool, including first-time contributors.

Start with the [contribution guide](CONTRIBUTING.md), or tell your agent:

> Help me contribute to AgentLane. Read CONTRIBUTING.md, make a focused improvement, and
> prepare a pull request back to thetrueshags/AgentLane.

Source contributions use GitHub issues and pull requests; no board registration is required.
MIT allows forks and reuse, and we encourage sharing useful improvements back here so everyone
benefits. A GitHub fork is also the usual workspace for submitting an upstream pull request.

## What AgentLane does

- **Plan work:** add tasks and acceptance criteria without reserving files or changing branches.
- **Divide work:** claim paths before editing; overlapping claims are refused.
- **Share context:** record findings, decisions, blockers and test results on the board.
- **Recover interrupted work:** time-limited claims expire while branches preserve the changes.
- **Check changes:** run project quality gates before landing or submitting a PR.
- **Choose how work lands:** land directly through Git, or submit a draft GitHub PR for review.
- **Track review outcomes:** sync merged PRs to done and closed PRs back to the backlog.

Task kinds include code, docs, test, research, design, ops and other. Deliverables must be files
in Git: a research report, design brief or runbook gets the same ownership rules as source code.
AgentLane coordinates repository work; running deployments or other external actions needs the
project's own authorization and process.

## Start through your agent

Tell your agent:

> Set up AgentLane in this repository and follow its AGENTS.md. Help me choose a task.

For a new project, ask it to clone [AgentLane](https://github.com/thetrueshags/AgentLane),
create your project's repository, and initialize the board there. For an existing project,
ask it to integrate the files below and preserve your existing agent instructions and hooks.
The agent handles setup and explains the board in plain language. GitHub authentication may
require you to complete `gh auth login` in a browser.

## Project setup reference

Prerequisites: Git, Python 3.9+, Bash for the supplied hooks and gates, and a writable Git remote.
On Windows use Git for Windows' Bash on PATH. GitHub CLI (`gh`) is needed for PR submission
and review syncing. Direct landing works with a writable Git remote on other hosts too;
the supplied automation workflows and PR integration are specific to GitHub.

1. Start with a committed default branch on the remote.
2. Integrate `.harness/`, `bin/board`, `tools/board-mcp` and the relevant agent configuration.
   Retain AgentLane's MIT license notice with the copied files. Merge `AGENTS.md` into existing
   instructions. Inspect existing hooks before installing.
3. Set `remote`, `main_branch`, `board_branch`, `hot_paths` and the timing settings in
   `.harness/config.json` for the project. Keep the default short claims, or adjust them to
   suit your work cadence. Long-running projects use successive tasks and sessions.
4. Select `landing_mode`: `direct` (default) or `pr` (review before merging).
5. Have the agent initialize and register each worker:

   ```sh
   python3 bin/board init
   python3 bin/board install --name alice --agent codex
   python3 bin/board join --name alice --agent codex
   ```

6. Customize `.harness/gate/code.sh` and `.harness/gate/docs.sh` to define acceptable work.
   Add `.harness/gate/smoke.sh` for an extra code check. Gates run locally before both direct
   landings and PR submissions.
7. On GitHub, enable the supplied workflows if desired. They target `main` and `board` by
   default; update their branch filters, checkout refs and push targets if you change these.
   With a shared board initialized, the main gate automatically reverts failed landings; adapt it to your team's branch rules
   and review policy before enabling it. Add `SLACK_WEBHOOK_URL` only for optional notifications.

The `.harness/` directory and `board_*` tool names are retained for compatibility with existing
installations. The product and rendered board are named AgentLane.

## A typical workflow

```sh
# Plan now, work later. No claim is created.
python3 bin/board add "Document recovery procedures" --kind ops \
  --description "Include restore steps and a verified recovery exercise." --globs 'docs/runbooks/**'

python3 bin/board status
python3 bin/board take TASK_ID
# The agent edits, checks, commits and pushes the claimed branch.
python3 bin/board note "Verified recovery against the staging snapshot" --kind test
python3 bin/board done
```

With `landing_mode: "pr"`, `done` runs the gate, opens a draft PR and releases the claim into
review. After a reviewer merges or closes it, `python3 bin/board sync-reviews` updates the task.
You can also request a PR for a single task with `done --pr`. A PR submission is not a landing.
PR creation failures keep the claim, and merged outcomes are checked against the remote's
configured main branch before the task is marked done.

Agents can use the equivalent MCP tools, including `board_add`, `board_take`, `board_note`,
`board_done` and `board_sync_reviews`. See [the architecture and operations reference](docs/agentlane.md)
for ownership, time limits, gates, review behavior and limitations.

## Agent integrations

Project configuration is included for Claude Code, Cursor, Codex, VS Code/Copilot, Gemini CLI
and OpenCode. Other agents can invoke `python3 bin/board` directly. See the configuration files
and [integration notes](docs/agentlane.md#hooks-in-the-agents).

## Developing AgentLane itself

Anyone contributing to AgentLane's source can work as a normal software project. Team registration
and claims apply when using an initialized shared board, not automatically to every source
maintenance session. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

Run the full test suite:

```sh
python3 -m unittest discover -s tests -v
```

Tests create temporary Git repositories. They cover claims, collisions, expiry, landing,
backlog planning, review outcomes, MCP calls and CI board operations. `tools/simulate` exercises
concurrent workers; historical evaluation reports live in `sim/`.

## License and community

AgentLane is available under the [MIT license](LICENSE). Participation follows our
[community guidelines](CODE_OF_CONDUCT.md). See [SECURITY.md](SECURITY.md) for private vulnerability reporting.
