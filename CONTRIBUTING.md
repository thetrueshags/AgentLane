# Contributing to AgentLane

Help improve the shared project at **[thetrueshags/AgentLane](https://github.com/thetrueshags/AgentLane)**.
We welcome fixes, documentation, tests, agent integrations, design feedback and reports from
people using AgentLane. You can contribute through a conversation with your agent.

## Find a place to start

- [Report a bug or propose an improvement](https://github.com/thetrueshags/AgentLane/issues/new/choose).
  Search existing issues first, and include a concrete example of the problem.
- [Ask a question or share an idea](https://github.com/thetrueshags/AgentLane/discussions).
- Improve a confusing setup step, reproduce a reported bug, or add a regression test.
- Look for issues labeled `good first issue` or `help wanted`. If none fit, propose something.

For a substantial feature, start with an issue so the maintainer and other contributors can
discuss scope before you invest time. Small fixes and documentation corrections can go straight
to a pull request. Comment on an existing issue when starting work to help avoid duplicate effort.

## Contribute through your agent

You can say:

> Help me contribute to https://github.com/thetrueshags/AgentLane. Read CONTRIBUTING.md and
> AGENTS.md, find a useful issue, make a focused change, test it, and prepare a pull request
> back to thetrueshags/AgentLane.

AgentLane source contributors use GitHub issues and pull requests. You do **not** need a board
membership, a shared claim, write access to this repository, or a running board service.
Do not run `agentlane init`, `agentlane join`, `agentlane install` or `agentlane done` for source contributions.
Those commands are for projects using AgentLane to coordinate their own workers.

## Development reference

Requirements: Git, Python 3.9 or newer, and Bash. On Windows, install Git for Windows;
the CLI selects its Bash for gates. AgentLane uses the Python standard library, so the test
suite has no runtime dependencies. Install the CLI for development with `python3 -m pip install -e .`. GitHub CLI is optional for opening your contribution PR.

1. Fork the repository to your GitHub account and clone that fork. A contribution fork is a
   workspace for sending changes back to AgentLane; you do not need to maintain a separate product.
2. Add `https://github.com/thetrueshags/AgentLane.git` as the `upstream` remote.
3. Create a branch from the latest `upstream/main` and make one focused change.
4. Run the tests:

   ```sh
   python3 tools/check
   python3 -m unittest discover -v
   ```

   On Windows, `python` can be used if it selects the intended interpreter. Tests use the
   selected Python interpreter, temporary Git repositories and simulated
   GitHub PR responses; they do not need access to a live team board or GitHub credentials.

5. Commit and push your branch to your fork, then open a PR with **base repository
   `thetrueshags/AgentLane`, base branch `main`**. The PR template helps explain the change.

## What makes a useful pull request

- Explain the problem, resulting behavior, and any related issue.
- Keep changes focused and preserve existing `.harness/` paths and `board_*` tools unless an
  agreed migration accompanies the change.
- Add meaningful regression coverage for behavior changes. Documentation-only changes do not
  need new tests. Describe manual checks where automated coverage is not practical.
- Update the relevant documentation and MCP wrapper when changing CLI behavior.
- Do not include credentials, personal board data or unrelated generated files.
- Agent-assisted contributions are welcome. Review the result yourself and report what was tested.

The PR checks run on Linux with Python 3.9 and 3.12, and on Windows with Python 3.12. They use
a read-only token and no repository secrets. GitHub may ask a maintainer to approve a first-time
contributor's workflow run. A passing check supports review; the maintainer decides when to merge.

## Review and community

[@thetrueshags](https://github.com/thetrueshags) currently maintains AgentLane and makes final
decisions about scope and merging. Feedback and alternative approaches are welcome. Review
times depend on maintainer availability; use the PR discussion for follow-up and revisions.

Please follow our [community guidelines](CODE_OF_CONDUCT.md). Report vulnerabilities through
the [private security reporting channel](SECURITY.md), rather than a public issue.

## Release preparation

See [release instructions](docs/releasing.md) for local wheel builds and upgrade checks.
Publishing packages, releases or tags needs explicit maintainer authorization.

## License

AgentLane is [MIT licensed](LICENSE). By submitting a contribution, you agree to make it
available under that license and confirm that you have the right to contribute it. Contributors
retain their copyright; no copyright assignment is required.
