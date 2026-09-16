# Three workers in five minutes

Alice uses Claude Code, Bob uses Codex, and Carol uses Cursor. Each has a separate clone and the
CLI installed. A maintainer has committed the gates, initialized `board`, and configured Git
credentials. These are the commands their agents run.

## Alice plans the work

```sh
agentlane join --name alice --agent claude-code
agentlane add "Authentication API" --paths "src/auth/**" --description "Implement the login response"
agentlane add "Billing service" --paths "src/billing/**"
agentlane add "User guide" --kind docs --paths "docs/guide/**"
```

On an empty board these become `AL-1`, `AL-2` and `AL-3`. Use the returned IDs on an existing board.

## Everyone takes a lane

Alice:

```sh
agentlane take AL-1
```

Bob, in his own clone:

```sh
agentlane join --name bob --agent codex
agentlane take --new "More auth work" --paths "src/auth/login.py"
```

The claim is refused: `src/auth/login.py` overlaps Alice's `src/auth/**`. Bob does not edit those
files. The refused attempt leaves no backlog task behind. He takes independent work:

```sh
agentlane take AL-2
```

Carol, in her own clone:

```sh
agentlane join --name carol --agent cursor
agentlane take AL-3
```

Each worker has a different `claim/AL-N-*` branch. They inspect status/notes and send
`agentlane heartbeat` during longer sessions.

## Leave context and land

Alice edits only `src/auth/**`, then:

```sh
agentlane note AL-1 "The login response is documented; billing can use the user ID"
git add src/auth
git commit -m "Implement login response"
agentlane done AL-1
```

AgentLane rebases, runs the code gate and atomically publishes code and completion.
Bob edits only `src/billing/**`, then:

```sh
git add src/billing
git commit -m "Implement billing service"
agentlane sync
agentlane done AL-2
```

Bob's landing includes Alice's work after rebasing onto main. Carol writes her guide:

```sh
git add docs/guide
git commit -m "Document login and billing"
agentlane done AL-3
agentlane status
```

All three tasks are done. If workers finish simultaneously, Git accepts one atomic push first;
the others refresh, rebase and rerun their gates. A failed gate keeps work unfinished. Inspect
decisions with `agentlane show AL-1`.

The same flow works with the `board_*` MCP tools. In PR mode, `done` submits review work;
completion appears after merging and `agentlane sync --reviews`.

## Optional independent review

First land `"require_review": true` in `.harness/config.json`; only then does the setting require
approvals. Existing legacy tasks without complete implementation provenance must be resolved
before switching. PR mode is incompatible with this policy.

Alice keeps her implementation claim and prepares its final candidate:

```sh
git fetch origin main
git merge --no-edit origin/main
agentlane gate --all
git push origin HEAD
git rev-parse HEAD
git rev-parse origin/main
agentlane heartbeat
```

She gives Bob the two full SHAs as `CANDIDATE_SHA` and `BASE_SHA`. Bob uses his own registered
clone (or a separate clean review worktree with his reviewer identity), fetches the claim
branch and checks out its exact commit. For example, for Alice's `claim/AL-1-auth` branch:

```sh
git fetch origin main claim/AL-1-auth
git checkout --detach CANDIDATE_SHA
git status --porcelain
git rev-parse origin/main
agentlane gate --all
# Run the task's specific acceptance tests too; record actual results below.
agentlane approve AL-1 --commit CANDIDATE_SHA --base BASE_SHA --evidence "Full gate and login acceptance tests passed"
```

The SHA placeholders must be replaced with full commit IDs. Bob must test the exact clean
candidate, verify the base, and describe tests actually performed. Bob does not take Alice's
task or receive a handoff. Alice continues heartbeats during review, then runs:

```sh
agentlane done AL-1
```

An ordinary merge lets an already-pushed claim advance without rewriting its history.
`done` preserves that exact reviewed SHA while it contains current main. If main advances,
`done` rebases as needed; a changed commit/base or lease requires a fresh approval.
Alice pushes the updated candidate and Bob repeats testing and approval.
Bob can revoke his approval with `agentlane withdraw AL-1`;
when several of his approvals are active, add `--approval APPROVAL_ID`. Old records remain in
task history. MCP clients use `board_approve` and `board_withdraw` with the same fields.
