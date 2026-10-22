# Changelog

## Unreleased

- Preserve task association when recovery precedes PR reconciliation, and preserve healthy main
  changes when recovering merge candidates with main as a second parent.
- Preserve binary/CRLF recovery patches and gitlinks independently of Git display settings.
- Recheck authenticated and registered GitHub identity after the gate before direct landing,
  retain merged board history and ancestry-safe export context, batch historical evidence reads,
  and install Claude hooks with LF endings on every platform.

- Enforce target-main PR landing policy, configure draft submission, and propose recovery through
  reviewed revert PRs. Bundled workflow tokens are read-only; optional automation uses a separate credential governed by repository rules.
- Add external task references across CLI/MCP views, claim branches and PRs.
- Add opt-in GitHub-bound member identity and retain login/email in review and activity evidence.
- Export committed board history, historical claims and gate evidence as JSONL with a hash manifest.
- Add Ruby/Rails gate detection, a project gate-command override and additive installed Claude hooks.
- Pass stable task/run/member correlation variables to supervised workers and retain effective values.

- Improve the local owner dashboard with responsive navigation, snapshot guidance, linked counts,
  task filters, blockers and full approval history. Correct future expiry wording and return 404
  for missing selected worker runs without showing unrelated logs.
- `agentlane ui`: a read-only localhost dashboard with overview, task detail, worker sessions and
  a merged activity timeline. Loopback-only bind, escaped agent-authored text, bounded log tails.
- Opt-in independent review for direct landing, governed by exact target main policy; default off.
- Structured CLI/MCP approvals and withdrawals tied to commit, main base and live lease, with
  durable implementation provenance and explicit incompatibility with PR mode.
- Exact expected-main guard on atomic publication, with a separate fast-forward ancestry proof.
- Guard Git's advertised pre-push input so an up-to-date main cannot publish board completion
  under a stale base. Chain existing custom hooks and honor their rejection.
- Refresh review policy after the gate before PR submission, detecting activation during tests.

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
