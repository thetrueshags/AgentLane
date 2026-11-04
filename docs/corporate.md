# Corporate deployment

AgentLane coordinates trusted collaborators through Git. A corporate rollout needs the
organization's repository, endpoint and AI-provider controls in addition to these local
guardrails. This guide supports an evaluated pilot; it is not a compliance certification
or a claim that arbitrary agent execution is safe. See [security reporting](../SECURITY.md).

## Permission and execution boundaries

| Surface | AgentLane behavior | Deployment control |
| --- | --- | --- |
| Git access | Uses the process's existing Git credentials; board state is on a Git branch | SSO and scoped credentials; restrict repository and branch writes on the server |
| Worker names | Local member names label claims and approval history | Do not treat names as authenticated corporate identities; require host-enforced reviewer identities |
| Hooks and claims | Local coordination checks for cooperative workers | Branch protection/rulesets, required checks and reviews; prevent bypass and force pushes |
| Gates | Project-controlled Bash scripts execute with caller privileges | Review gate changes; run builds on isolated endpoints/CI without production credentials |
| MCP | Stdio child process with inherited OS access and credentials | Approved executable/version, MCP allowlist and endpoint isolation; start with `--read-only` |
| Local workers | Runs an explicitly supplied command in a separate clone | Approved provider/command, minimal environment and isolated execution; separate clone is not a sandbox |
| Dashboard | Read-only loopback HTTP | Trusted local users only; no authenticated multi-user web service or public exposure |
| Board history | Tasks, notes, events and approvals are retained in Git | Repository access controls, retention/export policy and secret scanning |

Local hooks, MCP configuration and review policy can be changed by anyone with the relevant
filesystem access. Read-only MCP rejects write tools within that server session; it does not
restrict the agent's separate shell, filesystem tools, other servers or stolen credentials.
Git operations can also invoke local credential helpers and hooks. Use trusted Git config
and approved repositories even for inspection. Treat issue text, notes and tool output as
untrusted input, and keep policy changes behind review.

## Establish an approved pilot

1. Choose an approved repository and low-sensitivity test data. Record the exact AgentLane
   commit or wheel hash and Python/Git/Bash versions. Distribute reviewed artifacts through
   an approved internal channel; avoid installing unpinned code during agent startup.
2. Have repository administrators configure required reviews/checks, block force pushes and
   restrict bypass privileges. Require owner review for `.harness/config.json`, gates, hooks,
   workflows, agent instructions and MCP settings. Apply appropriate controls to the board
   branch too; its records are ordinary Git data, not a tamper-proof audit store.
3. Start [Copilot integration](integrations.md#github-copilot-in-vs-code) with
   `agentlane install --project --agent copilot --mcp-read-only`. Review generated files,
   inspect preserved configurations and confirm the running process uses `mcp --read-only`.
   Approve its exact executable and arguments through your managed MCP settings.
   For a cloud pilot too, run `agentlane install --project --agent copilot-cloud` and have
   an administrator review/apply the generated JSON in GitHub's Copilot MCP settings.
   That configuration defaults to read-only and needs an approved runtime installation.
4. Use credentials scoped to the pilot. Keep production secrets, broad user tokens and SSH
   agent access out of agent/build environments. Use your organization's secret manager;
   never commit credential values to MCP JSON, board notes or prompt files.
5. Before enabling write tools, choose a landing model. On GitHub, set
   `"landing_mode": "pr"` in `.harness/config.json`, require server-side reviews/checks,
   and validate PR creation using the pilot identity. Task completion follows a verified
   merge and `agentlane sync --reviews`. For direct landing, validate atomic-push support,
   branch restrictions and the optional independent review policy. That local policy is
   incompatible with PR mode and does not replace host authentication.
6. Test recovery and operational ownership: expired claims, handoff, failed gates, rejected
   pushes, interrupted workers, credential revocation and escalation to a responsible human.

Do not automatically enable remote write credentials for hosted Copilot workers. Their
permissions and PR lifecycle need a separate evaluation as described in the integration guide.
Direct Git coordination can use private writable remotes; built-in PR operations use GitHub
CLI and are not an Azure DevOps PR integration.

## Data handling and audit

The coordination engine does not require a hosted AgentLane service or send model requests.
It reads/writes configured Git remotes; GitHub PR operations use `gh`. Optional Slack posting
sends notices to the configured webhook. Copilot or another agent provider independently
handles prompts and tool results under its own contract and configuration. Approve provider,
model access, retention, residency and network destinations with the organization's AI policy.
Read-only tools still return potentially sensitive task descriptions and board notes.

Do not put credentials, customer records or production incident payloads into tasks or logs.
Worker receipts include command arguments and paths; stdout/stderr logs and prompt files can
contain sensitive content. Inspect all of these before sharing evidence. Disable optional
integrations unless approved and restrict local evidence with endpoint ACLs. Define retention
for local receipts/logs and Git history; deleting a note in the latest commit does not erase
older commits or other clones. Rotate exposed secrets and use the organization's Git-history
remediation procedure. Export required audit evidence to an access-controlled external system;
Git author metadata and AgentLane member names do not provide non-repudiation.

## Acceptance evidence

In a disposable repository with the intended corporate identity and endpoint policy, retain:

- Artifact version/hash, reviewed configuration and approved credential scopes (no secret values).
- Copilot loading the intended instructions and returning the correct board through MCP.
- Read-only `tools/list` containing only the six inspection tools and direct `board_take` and
  `board_done` calls returning errors without publishing refs or running a gate.
- For a write-enabled pilot: conflicting claims refused, failed gate blocked, protected main
  rejecting an unauthorized direct push, reviewed PR merged, and board reconciled afterward.
- Recovery ownership, credential revocation outcome, and retention locations for evidence.

Automated source tests cover generated configuration, preserved user files, restricted stdio
tool discovery/dispatch and unchanged remote refs after rejected calls. They do not certify
a live Copilot tenant, organization rulesets or a compliance framework. Obtain deployment
approval from the responsible corporate owners after the pilot evidence is reviewed.

Administrator references:
[GitHub Copilot MCP configuration](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers),
[VS Code enterprise policies](https://code.visualstudio.com/docs/enterprise/policies),
[GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
