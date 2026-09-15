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
