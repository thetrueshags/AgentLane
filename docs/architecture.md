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

`agentlane/board.py` contains the existing engine. `review.py` implements independent approvals
and target-base policy validation. `inspect.py` implements read-only views and
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
  last branch, implementation owners, structured approvals and landing/PR history.
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

The pre-push hook fetches target main for claim branches and measures paths and total
changed lines from its unique merge base with the candidate. Already-landed main work
imported by an ordinary merge is excluded; edits or reversions of that work still count.
An outdated candidate uses its shared ancestor with main, so newer main work it has not
imported is not counted as deletions. Repeated pushes count the full outstanding claim
diff, using target main's size limits and hot paths. Missing ownership, failed target
fetches and missing or ambiguous ancestry refuse the push. Nonclaim branches retain
their existing incremental push checks.

## Landing serialization

Direct `done` requires a clean tracked worktree on the owned branch. It fetches main and
rebases only when that main commit is not already an ancestor of the candidate. An
already-current candidate, including an ordinary merge, retains its exact SHA. It then
validates paths and runs the repository gate. A gate changing tracked files or HEAD
requires review and another attempt. Missing and failing gates stop landing.

After the gate, AgentLane fetches the board and verifies owner, lease, paths and expiry. It prepares
completion and claim removal, then sends the work SHA to main and the board SHA to board in one
`git push --atomic`. Both refs advance or neither does. Competing updates cause another rebase
and gate attempt. Main uses an explicit `--force-with-lease=refs/heads/MAIN:BEFORE` solely as
an exact expected-ref guard, **after a separate ancestry proof that BEFORE is an ancestor of
the candidate**. Git skips lease checks for up-to-date refs, so a mandatory temporary pre-push
guard also requires main to appear as a real update in Git's advertised push input, with exactly
BEFORE as its old SHA and the candidate as its new SHA. An omitted main update aborts before
board can publish. Movement after advertisement is rejected by the server's old-ref check.
This cannot rewrite history: movement of main rejects the push even when main reaches an
ancestor of the candidate or the exact candidate itself. There is no unconditional force flag.
Remotes without atomic-push support fail clearly; PR mode is an alternative only when
independent review is disabled.

The guard uses a hooks directory scoped to that single Git invocation. It resolves the existing
effective pre-push hook first, honoring custom `core.hooksPath` (including relative paths), and
runs an executable hook with its original arguments and a copy of the full input before its own
checks. A user hook's rejection stops publication without retrying past the rejection. The
original hook and persistent Git configuration are unchanged; temporary files are removed.
This uses a shell hook and does not depend on the newer `git hook run` command.

If another process publishes the exact candidate before or during the push, the board is not
silently marked done. A retry that observes main already at the candidate reports `nothing to
land`; an exhausted retry budget reports contention. Both retain the claim and approval history
for inspection of remote history. AgentLane reports no completion for its rejected publication.

A process can die after a successful push but before displaying success. Inspect `agentlane show
ID` and remote history before retrying: the atomic completion is the receipt. Local work branches
and any previously pushed remote branches are retained for inspection and rollback recovery.
Delete them through normal Git once no longer needed. Local cleanup never determines shared success.

`landing_mode` is read from the exact fetched target main configuration. A candidate cannot
turn a PR-only repository into a direct-landing repository. PR mode runs pre-submission checks,
pushes the branch and opens a PR. `pr_draft` defaults to `true` for compatibility; set it to
`false` on main when required review workflows need ready-for-review PRs. Confirm the repository's
reviewer workflow handles drafts before relying on draft submissions. Failed creation keeps
the claim. Successful submission moves the task to review and releases ownership. The host's merge
workflow then applies. `sync --reviews` verifies merged commits against remote main or reopens
closed PRs. It is explicit, not a background service.

### Opt-in independent review

`require_review` defaults to the JSON boolean `false`. Direct landing reads this policy from
the **exact fetched target main commit**, not from the candidate checkout. Enabling the policy
in a candidate is a bootstrap change: it becomes authoritative only once landed. Disabling
an already enabled policy still needs approval under the old base. PR mode and `done --pr`
fail explicitly when target main requires review. PR submission refreshes target policy again
after its gate to detect activation during tests. PR creation and later host merges are not
atomic with main's policy; AgentLane does not enforce host-side merges.

New tasks record versioned implementation provenance from their first claim onward. Every
owner acquired through take, recovery or handoff remains in that list across releases,
expiry, failed checkout recovery and rollback. A task creator is not an implementation owner
until taking or receiving a claim. Handoffs rotate the lease token. Missing legacy provenance
is never inferred or upgraded on re-take: such tasks remain readable and usable with the
default policy, but cannot satisfy required review. Finish legacy work before enabling the
policy, or arrange an explicit maintainer audit of provenance; there is no automatic migration.

`approve TASK --commit FULL_SHA --base FULL_SHA --evidence TEXT` requires a registered reviewer
who has never owned implementation of that task. In a separate clone/worktree with the reviewer
identity, they test a clean checkout of the exact pushed claim branch tip, verify the exact
remote main base is its ancestor, and supply test results as evidence. The command checks those
refs, live lease and path scope. It does not execute tests itself: the reviewer must run the
appropriate tests before approving. Notes are never approvals.

Approval records retain ID, reviewer, timestamp, candidate/base SHAs, evidence and claim identity
(owner, lease, branch, paths and hot flag). `withdraw TASK [--approval ID]` records a withdrawal
by the original reviewer without deleting the approval. Ambiguous withdrawals require an ID.
Added metadata is strictly validated; malformed booleans, records and provenance fail closed.

After each rebase and gate, `done` checks the fresh board for an unwithdrawn independent approval
of that exact HEAD, base and live claim. It records the approval ID in landing history. The
atomic board update rejects intervening withdrawal, handoff or recovery; the exact main guard
rejects base movement. Every contention retry fetches main, rebases if needed, and repeats
gate and approval checks. New commits, amendments, a different base or lease require a fresh approval. Heartbeats do not
change the lease identity. Engineering keeps the claim while QA reviews, then engineering lands;
handing implementation to QA makes QA an owner and therefore ineligible to approve.

### Identity and repository protection

`identity_mode` defaults to `self`, which accepts locally chosen member names. Joining captures
`git config user.email` and, when available, the authenticated `gh api user` login as `git_email`
and `github_login`. Events, notes, approvals and landing evidence retain these fields. Email is
Git configuration supplied by the worker; it is not independently verified.

Set `identity_mode` to `github` on target main to require an authenticated GitHub CLI. Join requires
the member name to match that login (lowercase). Mutations verify that login; reviewers must also
have a different GitHub login from every implementation owner. Ownership login snapshots survive
member re-registration. Existing members must rejoin to populate missing identities before mutations or direct landing.
This binds a worker to its current GitHub credentials, not to an independent agent attestation.

This remains a cooperative Git protocol. Review evidence is supplied by the reviewer, and writers
able to edit board history directly can bypass CLI policy. Protect the board branch like main:
restrict direct writers, disallow deletion/force pushes, and require an approved update path.
The approved board automation must still be able to perform the protocol's compare-and-swap
updates; a blanket PR requirement on board without such a writer prevents normal board commands.
Use repository rules to prevent humans and automation from bypassing main's PR and check requirements.
Git's `commit.gpgsign` configuration applies to board commits too; no code changes are needed for
signing. Signing requires the worker's Git signing configuration and available key.

### Task references and evidence

Tasks may carry an optional string `external_ref`, set with `add --ref TEAM-123`. It is shown in
CLI views and BOARD.md, included in PR titles/bodies, and slugged into claim branch names. AL task
IDs remain stable. References are metadata; no tracker synchronization is performed.

`agentlane export --out ./evidence [--since TIMESTAMP] [--until TIMESTAMP]` reads committed board
history without publishing board changes. Bounds use inclusive ISO-8601 timestamps with timezones
and select board commits by committer time, not individual record timestamps. All reachable
commits, including merged branch history, are visited with ancestors before descendants. A snapshot
before `--since`, ancestral to the first selected commit, is included as context so earlier approvals
and members remain interpretable. Clock skew cannot make a descendant the context for its ancestor.
When no commits match, the context is the latest eligible snapshot ancestral to the board head.
The bundle contains JSONL for tasks, historical claims, events, notes, members, approvals, landings,
gates and recovery proposals. Each row has `board_commit`, `context` and `record`. Distinct historical
versions are retained; identical records are deduplicated per class. A claim that was dropped at
landing is still available from history. `SUMMARY.md` lists landings and record counts.

The manifest identifies the branch, observed head, included commit range and exact commit list.
It hashes each file and the complete bundle: sort filenames, then hash each UTF-8 filename, a NUL
byte, and its exact file bytes in sequence with SHA-256. The manifest itself is excluded. Hashes
detect changes relative to the manifest; authenticity depends on trusted Git history and storage.
Exports require a new or empty output directory. They may contain identities and test output;
store them with the same access controls as repository evidence.

New landing/submission records include gate script paths, exit codes, bounded output (the final
4,000 characters per script), actor, agent and timestamp. PR reconciliation retains submission
evidence alongside the verified merge SHA and PR URL. Legacy records lacking evidence remain
explicitly incomplete; export never invents missing identity or gate results.

## Restricted automation credentials

Bundled workflows grant their default `GITHUB_TOKEN` only `contents: read`. To retain board
tending and automatic recovery PRs, configure repository secret `AGENTLANE_AUTOMATION_TOKEN`
and variable `AGENTLANE_AUTOMATION_MEMBER` with its lowercase GitHub login. Use a dedicated bot
user's fine-grained token limited to this repository, with Contents and Pull requests write
permissions. In GitHub identity mode, register that account on the board before enabling automation.
Both settings are required for automated writes. Board push events from that same account are
ignored to prevent recursive tending runs; scheduled and manual tending still run.

GitHub token permissions cannot be limited to individual branches. Repository administrators
must enforce main's PR requirement, required checks and reviews for this account as well, with
no bypass permission. Protect board against unapproved writers while allowing this account's
board updates and permitting work/recovery branches. The separate credential is restricted by
those host rules; declaring it a secret alone does not stop pushes to main. Validate the rules
with the bot account before enabling the workflows. Token creation, rotation and repository-rule
administration happen outside AgentLane. Without the credential, CLI workflows remain available
and the main gate still fails on test failures.

## Failure recovery

- **Crash or sleep:** commits remain on the branch. Push it for recovery from another clone.
- **Stale claim:** status/show expose stale state. A take or expiry sweep releases leases after
  the configured TTL or inactivity window. `release --force ID` only recovers stale ownership.
- **Active work:** send `heartbeat` every few minutes. Reads do not refresh leases.
- **Gate failure:** inspect output, fix and commit the change, then retry `done`.
- **Rebase conflict:** the failed rebase is aborted; resolve on the work branch and retry.
- **Network failure:** no success is reported. Fetch and inspect before retrying.
- **Post-merge gate failure:** the bundled main gate reports failure with a read-only default
  token. With the separately configured automation credential described above, it proposes a recovery PR.
  A maintainer can also run `agentlane revert-failed BEFORE_SHA AFTER_SHA --check-base --reason TEXT`
  from a clean checkout. It opens a recovery branch and PR; it never pushes main. Review and merge
  through the normal required checks, then run `sync --reviews`. Only a verified recovery merge
  reopens the original task. Recovery can locate an original PR that has merged but has not yet
  been reconciled by `sync --reviews`; both merge outcomes are then applied together.
  Recovery inverts the changes between the supplied commits and applies them to current main
  with a three-way merge, preserving unrelated earlier and later changes regardless of merge-parent
  order. Closed recovery PRs preserve task completion. Conflicts remain on the local recovery
  branch for maintainer resolution; no recovery branch or board update is published in that case. `--check-base` avoids proposing recovery for an already-failing base.
  A failed PR/board update can leave a published recovery branch or PR; inspect GitHub before retrying.

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
