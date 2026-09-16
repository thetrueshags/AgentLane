# Changelog

## Unreleased

- Optional provider-neutral foreground worker supervision in separate local clones, with
  atomic local receipts, separate logs, stdin files, timeout/interruption cleanup, common Git
  directory exclusion, conservative orphan recovery, and read-only MCP worker list/show.
- Contain Windows workers in kill-on-close jobs before their initial thread resumes, stop
  ordinary descendants after parent exit, isolate child Git/AgentLane context from the
  coordinator, and report malformed local receipts as actionable CLI errors.

## 0.1.0 — prepared, unreleased

- Installable CLI, Python module entry point and shared version metadata; no runtime dependencies.
- Additive installer, read-only doctor, list/show, paths and positional task arguments, AL-N IDs.
- Conservative overlap detection, state validation, atomic JSON writes and local serialization.
- Atomic main + board landing, post-gate ownership checks and explicit stale recovery.
- Missing-gate refusal, gate diagnostics, non-extensible hot claims and retained branch recovery.
- Per-file notification cursors, optional Slack environment configuration and board activity.
- Packaging, concurrency and failure tests; architecture docs, examples and release instructions.
- MIT licensing and contribution workflow.

There was no published package version before 0.1.0. Historical reports in sim/ describe the
earlier implementation and are retained as evidence, not current benchmarks.
