# Changelog

## Unreleased

- Opt-in independent review for direct landing, governed by exact target main policy; default off.
- Structured CLI/MCP approvals and withdrawals tied to commit, main base and live lease, with
  durable implementation provenance and explicit incompatibility with PR mode.
- Exact expected-main guard on atomic publication, with a separate fast-forward ancestry proof.

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
