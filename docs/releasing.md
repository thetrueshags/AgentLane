# Preparing a release

Publish only with explicit maintainer authorization. These steps prepare local artifacts; they
do not authorize a tag push, GitHub release or package upload.

1. Update `agentlane/__init__.py` and CHANGELOG.md. Package, CLI and MCP share the version.
2. Run from a clean checkout:

   ```sh
   python3 -m pip install -e .
   python3 tools/check
   python3 -m unittest discover -v
   python3 -m pip wheel . --no-deps --wheel-dir dist
   python3 tools/check-package dist/agentlane-0.1.0-py3-none-any.whl
   ```

3. Install the wheel in a fresh virtual environment outside the source directory. Verify help,
   version, read-only doctor, project installation and the local three-worker example. Check
   that gate, hook, instructions and license templates are in the wheel.
4. Review Linux/Windows CI, known limitations, upgrade notes and the final diff. Normal tests
   use local remotes and stubbed GitHub responses, requiring no live credentials.
5. Once separately authorized, tag the reviewed commit and publish the tested artifact and
   release notes. Keep secrets, identities, temporary repos and caches out of artifacts.

## Upgrade notes

Install the new package and preserve existing `.harness/` configuration, gates and agent configs.
Review changed defaults manually; installation does not overwrite them. Existing IDs, records,
legacy entry points and `board_*` names remain valid. If copying the new `bin/board` launcher
into an old repo, copy the `agentlane/` package too or install it; it now delegates to that package.

Direct landing requires an atomic-push-capable remote. Missing gates and malformed state are
errors. Overlap checks are more conservative, including case-insensitive ownership. Resolve any
pre-existing overlapping claims before upgrading workers together. Use matching versions and
configuration in a shared session.

PR workflows require GitHub CLI/authentication and explicit syncing. Creation can succeed before
a later board write fails; inspect both before retrying. A rebased PR branch may need explicit
history recovery before it can be pushed fast-forward; AgentLane never force pushes for you.
Slack may repeat a notice after partial failure. See architecture.md for recovery boundaries.
