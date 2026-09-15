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
