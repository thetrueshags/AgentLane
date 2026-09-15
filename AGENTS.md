# AgentLane agent protocol

## Choose the operating mode first

When the person is developing AgentLane itself, follow their source-maintenance instructions.
Read `CONTRIBUTING.md` for contributions to the public AgentLane repository. Use a normal branch
and a pull request back to `thetrueshags/AgentLane`; outside contributors do not need write access
or board membership. The source owner may direct ordinary maintenance commits and pushes.
Do not require team registration, board initialization or claims unless they ask to coordinate
that maintenance through a shared board. Do not initialize a board merely because this file exists.

The protocol below applies when using AgentLane to coordinate shared repository work. A person
can direct multiple agents, or several people can work together. Give each concurrent worker a
unique member name and a separate clone. Existing user instructions take precedence.

You may be one of several agents working on this project at the same time, directed by one or
more people, possibly through different tools. The board keeps you from colliding. Read this
whole file before doing anything else.

## The person you work for may not be a developer

Assume they can talk to you and nothing else. Never ask them to run a command, edit a file, or read
JSON. Translate everything into plain language. The only thing you may hand back to them is
`gh auth login`, because it opens a browser they must click through.

## First session in this checkout

1. If `.harness/member.json` is missing, ask the person their first name and confirm which tool you
   are (Claude Code, Cursor, Codex, Gemini, Copilot, other). Then call `board_join` with that
   name and agent, or run `python3 bin/board join --name <name> --agent <agent>`.
2. Run `python3 bin/board install --name <name> --agent <agent>` so the safety hook is in place.
3. Call `board_status` and tell the person, in two or three sentences, what the team is doing and
   which open tasks fit what they said they want to do. Non-code tasks count: research, runbooks,
   test plans, copy, design notes, user testing.

## Every task, in order

1. `board_status` first. Read the recent notes; someone may have already solved your problem.
   Use `board_add` to plan future work without reserving paths. Include the expected outcome
   and acceptance criteria in the description. Claim only work you are about to perform.
2. `board_take` with the task id, or with a title and the paths you will touch. Paths are globs
   like `src/auth/**` or `docs/pitch/**`. If the board refuses because of an overlap, tell the
   person who holds it and offer a different task. Do not edit anything until you hold a claim.
3. You are now on a branch named `claim/<id>-<slug>`, cut from `main`. Edit only under your
   claimed paths. If you need a file outside them, stop and take a second claim stacked on your
   branch with `base`, or ask the owner of that path to hand it off.
4. Commit at every green step, small and often. Push the branch whenever you commit. The
   pre-push hook refuses paths outside your claim and direct pushes to `main`.
5. `board_note` every finding, decision, dead end and test result another member would need.
6. `board_done` when the task works. It rebases onto the configured main branch and runs the
   gate. With `landing_mode: direct`, it lands and releases the claim. With `landing_mode: pr`,
   it opens a draft PR and releases the claim into review. A PR submission is not a completed
   task. Use `board_sync_reviews` after review to record merges or reopen closed PRs.
   If the gate or PR creation fails, fix and retry. Resolve rebase conflicts in this worktree.
7. Tell the person what landed or was submitted for review in one sentence, then call
   `board_status` and offer the next task.

## Time

By default, claims last 45 minutes and you may extend twice. Read `.harness/config.json` for
the project's actual timing settings. After the extension limit, land what you have or split the rest
into a new task. Claims with no heartbeat for 15 minutes are released automatically, and the branch
keeps the work. Small landings every 20 to 40 minutes are the target. Never sit on a branch.

## Hot paths

Files everyone needs, such as the dependency manifest and the main router, are listed under
`hot_paths` in `.harness/config.json`. Normal claims may not cover them. Take a `hot` claim for
exactly those files, make the change, land it within ten minutes.

## Stuck

Call `board_stuck` with one sentence. Keep working on something else or hand off with
`board_handoff`. Everyone sees stuck items on the board and in the team channel.

## Testing is a task

When a task is in review, test its PR branch in a fresh worktree. When the person wants to try
landed work, test the configured main branch. Record findings with `board_note` (kind `test`)
or propose bug tasks with `board_add` so another worker can claim them.

## Never

- Never push to `main` except through `board_done`.
- Never force push.
- Never work on a branch owned by another member.
- Never claim paths you are not about to edit.
- Never report a task as done before `board_done` has confirmed a direct landing or
  `board_sync_reviews` has verified its PR merge.
