# Project quality gates

`.harness/gate/code.sh` is executable, repository-controlled policy. Keep it short, predictable
and useful. It runs from the project root, with `BOARD_PATHS` containing changed paths separated
by newlines and `AGENTLANE_PYTHON` selecting the CLI's interpreter. Return zero to accept work,
nonzero to reject it. Install project dependencies before running the gate.

The sample detects scripts in package.json and selects pnpm, Yarn or npm from lock files. It
runs Python unittest, or pytest when explicitly configured; Go, Cargo, Mix, Maven and Gradle
are also recognized. Missing ecosystem executables fail normally. If no runner is detected,
the sample prints a warning and succeeds: replace that default with your project's checks
before treating it as evidence of correctness.

When every changed path is in `docs_globs`, `.harness/gate/docs.sh` checks merge markers. Add
documentation builds or link checks there. Kind alone does not select a gate. Hot claims use
the code tier. An optional `.harness/gate/smoke.sh` adds a code check. A missing required script
is an error, never an implicit pass.

```sh
agentlane gate --all
agentlane done AL-12
```

Failures report the script, exit status and output tail. Gates must not commit, change branches
or alter tracked files; doing so requires review and another attempt. Long gates can outlive
claims and cannot land with expired ownership. Choose timing settings that accommodate your
checks. Gates should never log credentials.

For another ecosystem, edit the script directly. No plugin framework or service is required.

## Independent review before direct landing

Set `"require_review": true` in `.harness/config.json` to require structured independent review.
The setting takes effect when it reaches target main. A candidate cannot disable main's current
requirement. This policy is incompatible with PR mode, including `done --pr`.

The engineer rebases on current remote main, commits, runs tests, pushes the claim branch and
keeps the claim alive with heartbeats. A registered reviewer who has never owned that task
checks out the exact pushed SHA in a clean separate clone/worktree, runs the gate and relevant
tests, then records their results with `approve TASK --commit FULL_SHA --base FULL_SHA
--evidence TEXT`. Approval is an attestation that those tests were performed; the command
does not run them. A free-text note cannot satisfy this check. QA must not receive a handoff
just to review: receiving implementation ownership makes QA ineligible for this task.

The engineer runs `done TASK`. The normal gate, path checks and live claim checks still apply.
Approval must match the resulting post-rebase/gated HEAD, main base and lease. A changed commit,
moving base, withdrawal, handoff or recovered lease requires review again. See the
[worked example](example.md#optional-independent-review) and [protocol details](architecture.md#opt-in-independent-review).

At publication, a temporary pre-push guard requires an actual main update from the reviewed base
to the candidate; an already up-to-date main cannot let board completion publish on its own.
The existing effective pre-push hook still receives its original input and arguments, and its
rejection is honored. If another writer has already published the exact candidate, `done` fails
with `nothing to land` and retains the claim for inspection instead of recording a stale landing.
