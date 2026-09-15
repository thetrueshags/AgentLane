# AgentLane architecture

## The coordination layer is Git

Each worker has a separate clone, identity and work branch. Shared JSON state lives on an orphan
`board` branch on the same remote as the project. Fetches expose ownership; commits preserve
history; compare-and-swap ref updates serialize changes. No daemon, database or agent runtime
owns the board.

```text
  Alice / Claude       Bob / Codex        Carol / Cursor
  separate clone       separate clone     separate clone
       |                    |                   |
       +------ fetch / claim / note / push ------+
                            |
                  shared Git remote
             +------------------------------+
             | board: tasks, claims, notes, |
             |        events, members       |
             +------------------------------+
       |                    |                   |
   claim/AL-1-*         claim/AL-2-*         claim/AL-3-*
       |                    |                   |
       +---- rebase on main; local gate ---------+
                            |
                atomic fast-forward push
               (main + board completion)
                            |
                           main
                            |
           optional CI gate / recovery / Slack
```

Gates run before publishing. Optional CI checks the landed result again. Hosting services may
provide PR review, but they do not own AgentLane's core coordination state.

## Code and installation

`agentlane/board.py` contains the existing engine. `inspect.py` implements read-only views and
doctor; `setup.py` adds missing project files. `mcp.py` delegates tools to the same CLI. Packaging
uses setuptools for the `agentlane` entry point with zero runtime dependencies. `python -m
agentlane`, `bin/board` and `tools/board-mcp` remain supported.

The package ships default templates; installed project files are authoritative. Existing gates,
instructions and agent configurations are preserved. Installation refuses to replace a different
pre-push hook. Hooks and shared gates remain inspectable files.

## Board lifecycle and representation

`init` requires a reachable remote main branch with an initial commit. It creates an orphan board
branch without project history. Commands use a disposable board worktree under Git's common
directory. An OS lock serializes CLI commands in the same checkout; process exit releases it.
Different workers still need separate clones.

- `tasks/ID.json`: title, description, kind, state, paths (`globs`), owner, created/updated time,
  last branch and landing/PR history.
- `claims/ID.json`: owner, agent, branch, base, paths, timestamps, TTL, extensions, hot flag and
  a unique lease token for new claims.
- `members/NAME.json`: worker identity.
- `notes/NAME.jsonl`, `events/NAME.jsonl`: append-only context and meaningful events.
- `narrator.json`: notification progress; `BOARD.md`: generated human view.

New IDs are `AL-N`, allocated inside a transaction. Legacy hexadecimal IDs remain valid. Missing
optional fields such as `updated` or a lease token are tolerated. Field names and states are
retained; no schema migration discards history. Dependencies are not implemented: describe them
in task descriptions or notes.

JSON uses UTF-8, sorted keys and atomic file replacement. Reads validate records, IDs, paths and
claim/task consistency. Corrupt state stops operations instead of being treated as an empty board.
Restore damaged records from a known-good commit in a separate repair clone, review the diff,
then push normally. Never force-reset the shared board and discard other workers' activity.

## Claim atomicity

A mutation fetches the board, checks ownership, writes records, commits and pushes. Two workers
can compute competing claims, but only one can fast-forward the remote ref from the same starting
point. A rejection causes a fresh fetch and replay. The loser sees the winner and refuses overlap.
Bounded backoff avoids retry storms.

An accepted claim precedes switching branches. If checkout fails, a compensating transaction
releases that lease. Re-taking uses a preserved local or remote branch when available. Planning
does not reserve paths. Blocked tasks with a live claim remain owned. Stacking is limited to
disjoint claims based on the worker's own branches.

### Path ownership

Paths are repository-relative and use `/`; Windows separators are normalized on input. Absolute
paths, traversal, Git metadata and control characters are rejected. Exact files match exactly.
`directory/**` includes the directory and descendants. Other patterns use Python `fnmatch`
semantics: `*` may cross `/`; `?` and character classes are supported. Quote shell wildcards.

Overlap checks err toward refusal when prefixes cannot prove separation. Partial prefixes
(`src/a*` and `src/auth/**`) overlap. Ownership is compared case-insensitively to accommodate
Windows and Unix workers. Some disjoint wildcard patterns may be refused; prefer precise claims.
`src/auth/**` and `src/authz/**` are separate. Changed paths use no rename detection, so moves
require ownership of both old and new locations.

Hot files need a short `--hot` claim containing only hot paths; these cannot be extended.
Normal durations and extension limits come from `.harness/config.json`.

## Landing serialization

Direct `done` requires a clean tracked worktree on the owned branch. It fetches main, rebases,
validates paths and runs the repository gate. A gate changing tracked files or HEAD requires
review and another attempt. Missing and failing gates stop landing.

After the gate, AgentLane fetches the board and verifies owner, lease, paths and expiry. It prepares
completion and claim removal, then sends the work SHA to main and the board SHA to board in one
`git push --atomic`. Both refs advance or neither does. Competing updates cause another rebase
and gate attempt. No force push is used. Remotes without atomic-push support fail clearly;
PR mode is the alternative.

A process can die after a successful push but before displaying success. Inspect `agentlane show
ID` and remote history before retrying: the atomic completion is the receipt. Local work branches
and any previously pushed remote branches are retained for inspection and rollback recovery.
Delete them through normal Git once no longer needed. Local cleanup never determines shared success.

PR mode runs pre-submission checks, pushes the branch and opens a draft PR. Failed creation keeps
the claim. Successful submission moves the task to review and releases ownership. The host's merge
workflow then applies. `sync --reviews` verifies merged commits against remote main or reopens
closed PRs. It is explicit, not a background service.

## Failure recovery

- **Crash or sleep:** commits remain on the branch. Push it for recovery from another clone.
- **Stale claim:** status/show expose stale state. A take or expiry sweep releases leases after
  the configured TTL or inactivity window. `release --force ID` only recovers stale ownership.
- **Active work:** send `heartbeat` every few minutes. Reads do not refresh leases.
- **Gate failure:** inspect output, fix and commit the change, then retry `done`.
- **Rebase conflict:** the failed rebase is aborted; resolve on the work branch and retry.
- **Network failure:** no success is reported. Fetch and inspect before retrying.
- **Post-merge gate failure:** optional automation reverts the failed range and reopens its task.
  It checks the base first to avoid reverting an innocent landing on already-broken main.
  The revert and board update use one atomic push, retrying competing ref updates. Failed
  recovery requires maintainer inspection; it never reports a partial rollback as success.

## Events and Slack

Events retain IDs, timestamps, worker, agent, type, task and text. Joining, planning, claiming,
releasing, completing, blocking, noting and reopening leave history. `BOARD.md` shows available
work, active/stale claims, blocked/review work, completions, team members, notes and activity.

Slack is optional. `narrate` reads `SLACK_WEBHOOK_URL` or the legacy explicit option. Without it,
notices print locally. Cursors track per-file record counts so events in the same second are not
skipped; legacy timestamp cursors remain readable. Run one narrator per project, as the provided
workflow does. Delivery is at-least-once: failure after sending but before saving the cursor can
repeat notices. Errors hide the webhook URL. Slack availability does not affect core claims.

## Boundaries

Workers must use the same remote, board branch and timing policy, distinct names and reasonably
synchronized clocks. Hooks are cooperative guardrails, not a security boundary against someone
permitted to bypass them. Branch protection and deployment authorization are separate choices.
Workflows target `main`/`board` and need adapting for custom names. Installation does not create
remotes or publish project files.
