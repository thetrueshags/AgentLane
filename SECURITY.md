# Reporting a security vulnerability

Please use [GitHub's private vulnerability reporting for AgentLane](https://github.com/thetrueshags/AgentLane/security/advisories/new).
Avoid posting exploit details, credentials or private repository data in public issues.

Include the affected commit or version, steps to reproduce, expected impact, and a minimal
example using a disposable repository. Redact any secrets from logs. You do not need to prepare
a fix before reporting a problem.

The maintainer will investigate and coordinate a fix and disclosure through the private report.
Response times depend on availability; the project does not currently offer a response-time
guarantee or a bug bounty. Security fixes currently target `main`; there are no separately
maintained release lines.

AgentLane hooks coordinate cooperative workers. They are local guardrails, not a security
boundary against someone who already has permission to rewrite local hooks or repository files.
Unexpected command execution, credential exposure, or remote writes beyond the intended
operation should still be reported.
