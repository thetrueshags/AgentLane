# Agent integrations

Every agent follows AGENTS.md and uses the same engine. MCP is a thin stdio adapter with no
separate state or process rules.

| Tool | Source-checkout configuration |
| --- | --- |
| Claude Code | .mcp.json and .claude/settings.json |
| Codex | .codex/config.toml |
| Cursor | .cursor/mcp.json |
| Gemini CLI | .gemini/settings.json |
| Copilot / VS Code | .vscode/mcp.json |
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

Claude's optional hooks show session status and heartbeat at turn completion. Other workers
should call `agentlane heartbeat` during active work. Reads do not refresh claims. No agent
process is automatically kept alive or restarted by AgentLane. Editor trust and execution
settings still apply; use the shell CLI if a client cannot launch project MCP servers.
Cloud workers need Git credentials and a writable clone like any other worker.
