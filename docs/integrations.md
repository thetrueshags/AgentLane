# Agent integrations

Every agent follows AGENTS.md and uses the same engine. MCP is a thin stdio adapter with no
separate state or process rules.

| Tool | Source-checkout configuration |
| --- | --- |
| Claude Code | .mcp.json and .claude/settings.json |
| Codex | .codex/config.toml |
| Cursor | .cursor/mcp.json |
| Gemini CLI | .gemini/settings.json |
| Copilot / VS Code | .vscode/mcp.json and .github/copilot-instructions.md |
| Copilot cloud agent | Generated administrator configuration; see below |
| OpenCode | opencode.json |
| Shell agent or human | agentlane CLI and AGENTS.md |

Source configurations retain `python3 tools/board-mcp` for compatibility. In installed projects,
`agentlane install --agent codex` (or cursor, gemini, claude-code, copilot, opencode) creates a
missing configuration using `agentlane mcp`. Existing files are preserved; integrate the server
entry into them when needed. Launch in the project directory, or set `BOARD_REPO_ROOT` explicitly.

Tools include board_doctor, board_list, board_show, board_add, board_take, board_note, board_sync,
board_done, board_release, board_approve, board_withdraw and the existing recovery/review tools.
`board_approve` requires `task_id`, full `commit` and `base` SHAs, and test `evidence`;
`board_withdraw` accepts `task_id` and an optional `approval` ID. These use the same independent
review checks as the CLI, including exact checkout, registration and retained implementation
owners. Old names remain valid.
JSON output comes from the same commands that humans run in a terminal.

Optional [local foreground supervision](workers.md) runs an explicit provider-neutral argv
in a separate clone: `agentlane worker run --clone PATH --name NAME -- COMMAND ARGS...`.
Use `--stdin-file FILE` for a prompt and supply provider resume arguments yourself when needed.
`board_worker_list` and `board_worker_show` expose read-only local process receipts, including
log paths, without board initialization. Launch and orphan resolution stay in the foreground
CLI; synchronous MCP tool timeouts/cancellation cannot safely define a long-running worker's
lifetime. A zero exit code is a process outcome, never task completion.

`agentlane install --project --agent claude-code` installs session status and turn-completion
heartbeat hooks and merges their entries into `.claude/settings.json`. Existing hooks, permissions
and custom hook scripts are preserved; repeated installs do not duplicate entries. Use
`--no-claude-hooks` to skip this addition. The hook scripts use the installed `agentlane` CLI.
They remain idle until this checkout has a local member identity. Other workers
should call `agentlane heartbeat` during active work. Reads do not refresh claims. No agent
process is automatically kept alive or restarted by AgentLane. Editor trust and execution
settings still apply; use the shell CLI if a client cannot launch project MCP servers.
Cloud workers need Git credentials and a writable clone like any other worker.

`board_add` accepts an optional `external_ref` string; `board_show` returns it with the task.
`board_export` accepts required `out` and optional `since`/`until` timestamps. It reads board
history and writes a local evidence bundle without changing the board; see [architecture](architecture.md).

If a local harness requires Git writes in a subagent, invoke mutating CLI/MCP commands from that
subagent. Shell-command guards do not necessarily inspect Git operations inside MCP processes.
AgentLane does not override that harness policy.

## GitHub Copilot in VS Code

For a project adopting AgentLane, install the CLI from your approved source, then run:

```sh
agentlane install --project --agent copilot
```

This creates a missing `.vscode/mcp.json` stdio server and
`.github/copilot-instructions.md` linking the workflow to `AGENTS.md`. The `vscode` alias
does the same. Existing configuration and instructions are preserved byte for byte;
merge the suggested server or instructions manually if those files already exist.
Review and commit the generated project files through your normal setup process.
Open the worker's own clone as the VS Code workspace. The generated server sets
`cwd` to `${workspaceFolder}`; ensure an inherited `BOARD_REPO_ROOT` does not point elsewhere.
Allow the server through your organization's MCP policy and VS Code's trust prompts,
then check that `board_status` returns the intended project before claiming work.
Each simultaneous Copilot worker needs a separate clone and unique member identity.

For an inspection-only pilot, create configuration with:

```sh
agentlane install --project --agent copilot --mcp-read-only
```

The new server invokes `agentlane mcp --read-only`. If configuration already exists,
the installer reports that the restriction was not applied; change its arguments manually
and restart the server. This option also works for newly generated configurations for
the other supported MCP clients. Default installations keep the existing full tool set.

Read-only mode exposes only `board_doctor`, `board_status`, `board_list`, `board_show`,
`board_worker_list` and `board_worker_show`. The server refuses direct calls to all other
tools before CLI dispatch, even if a client cached their names. Board reads fetch Git
data and refresh AgentLane's local board worktree; they do not publish task changes,
claim work, run gates or land code. Diagnostics may contact configured Git remotes or
check existing GitHub authentication. This is a tool restriction, not an OS sandbox or
a restriction on the client's separate terminal tools.
For source-checkout configurations using `tools/board-mcp`, append `--read-only` to the
launcher's arguments. The compatibility launcher honors the same flag and rejects unknown
options rather than silently starting an unrestricted session.

## Copilot cloud agent

The VS Code file does not configure Copilot on GitHub. A repository administrator must
configure that service separately and install an approved AgentLane version in its runtime.
Generate an inspection-only configuration for that administrator with:

```sh
agentlane install --project --agent copilot-cloud
```

This adds missing `.github/copilot-instructions.md` instructions and writes
`.harness/integrations/copilot-cloud.json`. It preserves an existing cloud configuration,
instructions and the VS Code configuration, so both environments can coexist in one project.
Cloud configuration always starts with `--read-only` and an explicit three-tool allowlist.
The generated JSON is an administrator handoff artifact; GitHub does not load it from this
path automatically. Copy its contents to the repository's Copilot MCP settings:

```json
{
  "mcpServers": {
    "board": {
      "type": "stdio",
      "command": "agentlane",
      "args": ["mcp", "--read-only"],
      "tools": ["board_status", "board_list", "board_show"]
    }
  }
}
```

Launch in the intended clone, or supply its absolute path through `BOARD_REPO_ROOT` in
the runtime. Give the runtime read access to the board branch as well as source.
Cloud-agent repository MCP tools can execute without individual approval, so avoid a
wildcard tool allowlist. Full AgentLane coordination requires permission to publish
board state and worker branches; ordinary cloud-agent PR permissions may not provide it.
AgentLane's claim branch also has to fit the service's branch/PR lifecycle. Validate those
conditions in a disposable repository before enabling write tools. A cloud-agent PR
alone does not complete a board task. This release does not automate cloud runtime setup
or claim compatibility with every hosted Copilot workflow.

Configuration formats and service policies follow the official
[VS Code MCP documentation](https://code.visualstudio.com/docs/agent-customization/mcp-servers),
[Copilot repository instructions](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions)
and [Copilot repository MCP documentation](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers).
See [corporate deployment](corporate.md) for permissions, data handling and pilot acceptance.
