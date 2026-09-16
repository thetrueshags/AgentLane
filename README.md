# AgentLane

**Parallel coding agents. One repository. No collisions.**

**Git-native coordination for humans and coding agents working concurrently on one repository.**

Each worker gets a lane: a task, an exclusive claim on the paths it needs, and an isolated branch.
Git coordinates ownership and serializes completed work into the shared codebase. The board lives
in the repository. No coordination server, database, hosted control plane or shared agent runtime
is required.

[MIT license](LICENSE) | [Contribute](CONTRIBUTING.md) | [Issues](https://github.com/thetrueshags/AgentLane/issues/new/choose) | [Discussions](https://github.com/thetrueshags/AgentLane/discussions)

Use AgentLane for product development, maintenance, documentation, research, operations, or a
hackathon. One person can direct several agents; a team can bring different tools. Each concurrent
worker uses a separate clone and unique name. This coordinates cooperative workers; repository
permissions remain your responsibility.

## Install

Requires Python 3.9+, Git and Bash (Git for Windows on Windows). There are no Python runtime
dependencies. From an AgentLane source checkout:

```sh
python3 -m pip install -e .
agentlane --help
agentlane --version
```

Or use `pipx install .` for an isolated installation. For an existing project, install AgentLane
from a local source checkout or a maintainer-provided wheel, then work in that project.
No package has been published as part of this release preparation. On Windows, use `python` if
that selects your intended interpreter.

## Your first lane

In a project whose shared board is initialized:

```sh
git clone <project-repository>
cd <project-directory>
agentlane join --name alice --agent codex
agentlane status
agentlane list --available
agentlane take AL-12
# Work inside the claim's paths, then commit your changes.
agentlane note AL-12 "API complete; frontend can now use /auth/login"
agentlane done AL-12
```

`take` without an ID lists available tasks. `show AL-12` shows description, paths, ownership,
branch, timestamps, stale state and notes. For incomplete setup, run `agentlane doctor`: it
diagnoses without making changes. Install the local pre-push guardrail with `agentlane install`;
`done` enforces its own checks regardless.

Your agent can handle these steps. Tell it: "Read AGENTS.md, inspect the board, and help me choose
a task." See the [three-worker walkthrough](docs/example.md).

## Set up a project once

Start with a Git repository having an initial commit on its remote main branch. Defaults are
`main` and `board`.

```sh
agentlane install --project --agent codex
agentlane init
agentlane join --name alice --agent codex
```

`install --project` adds missing configuration, instructions, gates and the selected agent's MCP
configuration. Existing files are preserved; merge your existing instructions/configuration when
necessary. It refuses to replace a different pre-push hook and retains the MIT notice with copied
files. It does not create a remote or publish project files.

Commit generated files through a first setup lane, choosing the paths actually added:

```sh
agentlane take --new "Configure project coordination" --paths ".harness/**" "AGENTS.md" ".codex/config.toml"
git add .harness AGENTS.md .codex/config.toml
git commit -m "Configure AgentLane"
agentlane done
```

Customize `.harness/gate/code.sh` before this landing. `agentlane doctor` explains remaining setup
steps. For source contributions to AgentLane itself, use [CONTRIBUTING.md](CONTRIBUTING.md) instead.

## Plan, work and recover

```sh
agentlane add "Implement auth endpoint" --description "Return a tested login response" \
  --paths "src/auth/**" "tests/auth/**"
agentlane list --mine
agentlane show AL-12
agentlane heartbeat
agentlane sync
agentlane release AL-12
```

Backlog tasks reserve no paths. Conflicting claims are refused, including simultaneous attempts.
Paths are repository-relative; quote wildcards. See [glob semantics](docs/architecture.md#path-ownership).

Claims default to 45 minutes with two extensions, and become recoverable after 15 minutes without
a heartbeat. Send heartbeats while working; reads do not refresh claims. Timing is configurable.
Stale claims stay visible until a sweep or new take releases them. Re-taking recovers an existing
branch when available. `release --force AL-12` recovers only stale ownership, never a live claim.
Branches preserve commits; push them so another clone can recover the work.

## Landing and gates

`done` verifies ownership, rebases onto current main, checks paths, runs the project gate, and
rechecks the lease. It publishes code and task completion in **one atomic Git push**. If another
worker lands first, AgentLane refreshes, rebases and checks again. A failing gate, expired lease
or rejected ref update does not mark work complete. Direct landing requires a remote supporting
atomic pushes; there is no unsafe fallback.

`.harness/gate/code.sh` defines acceptable work. The sample detects Python, Node package managers,
Go, Rust, Elixir and Java. Documentation paths use `.harness/gate/docs.sh`. Missing gates fail
closed. See [gate customization](docs/gates.md).

For GitHub review, set `landing_mode` to `pr` in `.harness/config.json`, or use `done --pr`.
Successful submission releases ownership into review; after merging or closing, run
`agentlane sync --reviews`. GitHub CLI/authentication are needed only for PR operations.
PR submission is not completion. Core direct landing works with a writable Git remote.

For independent review before **direct** landing, opt in with `"require_review": true`.
The policy is read from exact target main and takes effect after the enabling change lands.
A registered reviewer who has never owned implementation tests the pushed commit in a separate
clean checkout and records `approve TASK --commit FULL_SHA --base FULL_SHA --evidence TEXT`.
Engineering keeps the claim and runs `done`; changed commits, bases or leases need fresh approval.
PR mode is incompatible with this policy. See the [review workflow](docs/example.md#optional-independent-review).

## Integrations and optional automation

Claude Code, Codex, Cursor, Gemini CLI, VS Code/Copilot, OpenCode and shell agents all follow
[AGENTS.md](AGENTS.md). CLI and MCP share one engine. Source checkouts retain `tools/board-mcp`
and existing configurations; installed projects can launch `agentlane mcp`.
See [integration details](docs/integrations.md).

GitHub Actions and Slack are optional. Workflows can check landed work, recover failed landings
when a board is present, and expire stale claims. They target `main`/`board`; adapt them for custom
branch names. Set `SLACK_WEBHOOK_URL` for concise notices. Core commands need neither GitHub
Actions nor Slack, and can use a local bare Git remote.

## Contribute and learn more

Contributions go back to [thetrueshags/AgentLane](https://github.com/thetrueshags/AgentLane).
Source contributors use ordinary branches and PRs without board membership.

- [Contribution guide](CONTRIBUTING.md)
- [Architecture and recovery](docs/architecture.md)
- [Three-worker example](docs/example.md)
- [Changelog](CHANGELOG.md) and [release instructions](docs/releasing.md)
- [Community guidelines](CODE_OF_CONDUCT.md) and [private security reporting](SECURITY.md)

Run `python3 -m unittest discover -v` for the complete credential-free suite. Build instructions
live in CONTRIBUTING.md. AgentLane is [MIT licensed](LICENSE).
