# EVAL 001: simulation and real-agent evaluation of the board harness

Date: 2026-09-15. Harness at `~/Projects/hackathon-harness`, uncommitted. All runs on one
macOS laptop against local bare git origins, so latencies exclude network round trips to GitHub
(add roughly one second per fetch or push for a hosted remote).

## Takeaway

The harness holds its safety invariants under concurrent synthetic agents at 2, 4, 6, 8 and 12
members with injected crashes and bad landings, and a real headless Claude Code session acting for
a non-developer completed the full join, take, edit, land, note, report loop with no errors.
The simulation found four defects, all fixed with regression tests; two were shim bugs that
would have shown up at a real event, two were sim-scale artefacts that still exposed a config
coupling worth clamping.

## Pre-registered hypotheses

- H1: no two live claims from different members ever overlap, in any board state.
- H2: every crashed agent's claim is released by the sweeper; work stays on its branch.
- H3: a landing that passes the local gate but breaks main is reverted and its task reopened,
  and clean landings are never reverted as collateral.
- H4: board push retries rise with team size but the retry budget is never exhausted at 8.
- H5: every commit on main is a landing or a revert.

## Method

`tools/simulate` builds a bare origin carrying the harness, then runs N agent threads, each in
its own clone, looping over take, one to three commits under the claimed paths, optional branch
push, heartbeat, note, then `board done`. Faults: 10% of claims crash (the agent stops and never
returns), 15% of code landings carry a marker that only the full main gate detects, 10% of takes
are hot-path micro-claims, 15% are docs tasks. A CI thread plays the two workflows: expire every
five seconds, and for each new commit on main run the full gate and call `revert-failed` on
failure. Claim TTL is one minute and stall release nine seconds so expiry is observable.
Afterwards the audit walks every commit of the board branch and checks pairwise overlap of live
claims, checks every main commit against recorded landings and reverts, and checks that every
done task's files exist on main.

Reproduce: `python3 tools/simulate --agents 8 --tasks-per-agent 4 --modules 6 --seed 2`.
Reports for every run cited here are in `sim/reports/`.

## Results

| Run | Agents | Overlaps (H1) | Unexplained main commits (H5) | Lost work | Takes ok / refused overlap | Take errors | Landings ok / failed | Crashed / expired (H2) | Bad landings / gate fails / reverts / revert failures (H3) | take p50 / p95 s | done p50 / p95 s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| pre-fix `run_a4_m6_s1` | 4 | 0 | 0 | 0 | 17 / 7 | 0 | 16 / 0 | 1 / 1 | 2 / 4 / 3 / 1 | 0.53 / 1.0 | 0.80 / 1.2 |
| pre-fix `run_a8_m6_s2` | 8 | 0 | 0 | 0 | 33 / 34 | 3 | 32 / 0 | 1 / 0 | 1 / 1 / 1 / 0 | 1.29 / 3.0 | 1.57 / 2.3 |
| pre-fix `run_a6_m3_s3` | 6 | 0 | 0 | 0 | 24 / 45 | 0 | 22 / 0 | 2 / 1 | 1 / 1 / 1 / 0 | 0.15 / 0.9 | 0.88 / 1.5 |
| final `final_a8_m6_s2` | 8 | 0 | 0 | 0 | 21 / 32 | 0 | 17 / 0 | 4 / 4 | 1 / 1 / 1 / 0 | 0.30 / 2.0 | 0.61 / 1.7 |
| final `final_a12_m8_s4` | 12 | 0 | 0 | 0 | 34 / 47 | 0 | 31 / 0 | 3 / 6 | 3 / 3 / 3 / 0 | 0.35 / 3.9 | 1.09 / 16.5 |
| final `final_a6_m3_s3` | 6 | 0 | 0 | 0 | 18 / 22 | 0 | 16 / 0 | 2 / 2 | 1 / 1 / 1 / 0 | 0.32 / 1.5 | 0.71 / 2.5 |

Pre-fix agents that crashed came back after the stall window and released their own claim, which
is why pre-fix crashed and expired counts differ; final runs keep crashed agents dead.

Verdicts:

- H1 holds in all nine runs (three smoke and intermediate runs not tabled also had zero).
- H5 holds in all runs. No lost work in any run.
- H2 holds: in final runs every crashed claim was released. The 12-agent run released three
  extra claims of live agents whose `done` took longer than the nine-second sim stall window;
  none lost their landing. At production settings (15 minute stall, operations of seconds) this
  cannot occur, but see defect 3.
- H3 failed pre-fix and holds post-fix: pre-fix 4-agent run had 4 gate failures and 3 reverts
  for 2 bad landings, meaning a clean landing was reverted because main was already red. Final
  runs show gate failures equal to bad landings equal to successful reverts, with zero failed
  reverts. The base-red skip path did not fire in the final runs because reverts landed before
  the next clean landing; it is covered by a unit test.
- H4 failed pre-fix and holds post-fix: pre-fix 8 agents produced 3 takes that exhausted 12
  immediate retries. With exponential backoff and jitter and heartbeat coalescing, 8 and 12
  agents produced zero take errors. The cost is tail latency: done p95 at 12 agents is 16.5
  seconds, driven by backoff waits under heavy contention. Refused-overlap counts are high by
  design, since the sim gives 12 agents only 8 modules.

## Defects found and fixed

1. Innocent landings reverted (shim, would occur at an event). Once main was red, the next clean
   landing failed the full gate and its own workflow run reverted it. Fix: `revert-failed
   --check-base` runs the gate at the base commit first and skips when main was already red,
   leaving the revert to the run that broke it. The workflow passes the flag. Test:
   `test_revert_is_skipped_when_main_was_already_red_at_base`.
2. Board retry livelock (shim, likely at 6 or more members). Retries after a lost push were
   immediate, so contending agents kept colliding. Fix: exponential backoff with jitter, capped
   at 8 seconds, and default retries raised from 6 to 8.
3. Every transaction wrote to the board even when nothing changed, because `BOARD.md` was
   re-rendered with a fresh timestamp first. Heartbeats therefore always cost a push. Fix: a
   transaction that changes no data returns before rendering. Heartbeats are also coalesced:
   a claim refreshed within the last 60 seconds is not rewritten, and the window is clamped to a
   third of the stall-release window so coalescing can never starve expiry. Tests:
   `test_heartbeats_inside_the_window_do_not_write_to_the_board`,
   `test_heartbeat_window_is_clamped_below_the_stall_window`.
4. `board done` after an expired claim said only "you hold no claim". Fix: it now recognises
   the branch, explains the claim expired, says the work is safe, and gives the exact re-take
   command. Test: `test_done_explains_an_expired_claim_and_how_to_recover`.

Simulator defects fixed along the way: missing `board init`, single-line JSON parsing, the CI
thread not passing the fault marker to the base check, and zsh word splitting in the runner.

## Real-agent evaluation

One headless Claude Code run (`claude -p`, v2.1.267) in a fresh clone with the committed
`.mcp.json`, tools restricted to the board MCP server plus read, write, edit and git add, commit,
status, diff, log. Prompt: "Hi, I'm Sam. I'm not a developer, I'm on the team for this
hackathon. What can I help with? Pick whatever fits me best and just do it, then tell me what
happened in plain words." Board had two open tasks: a code task under `src/auth/**` and a docs
task under `docs/pitch/**`.

Observed, verified against the board branch events and the origin git log:

- Registered Sam with `board_join`, called `board_status` before acting.
- Took the pitch task, not the code task, with the correct globs.
- Changed exactly one file, `docs/pitch/pitch.md`, under the claimed path.
- Committed, called `board_done`, landed on main. Board shows the task done under Sam.
- Wrote a decision note explaining the pitch covers the harness because the demo app is a stub.
- Final message to Sam was plain language with no commands or JSON, matched board reality, and
  offered three non-code follow-ups.
- 14 turns, 119 seconds, about $1.06, empty stderr: no hook, MCP, or permission errors.

One run is a smoke test, not a distribution. It shows the onboarding path and tool descriptions
work for the default agent; it says nothing about Cursor, Codex, Gemini or Copilot behaviour.

## Not covered

- No network: every run used a local bare origin. GitHub latency, rate limits, and the two
  workflows running on Actions are untested.
- Semantic conflicts between disjoint claims (a shared interface and its caller) are not
  simulated; the sim writes independent files.
- Only one real-agent run, only Claude Code, only the docs task path. The code task path,
  stacking, hot claims, handoff and stuck flows with a real agent are untested.
- Tail latency at 12 agents (done p95 16.5 s locally) will be worse over a network; whether that
  matters depends on how often real agents land, which at 20 to 40 minute cadence it should not.
- The base-red skip path fired zero times in simulation; it is exercised only by the unit test.

## Changes that need human judgment

- Backoff cap (8 s) and retry count (8) are chosen, not measured, for a network remote.
- The heartbeat clamp uses one third of the stall window; a different fraction is defensible.
- Whether to keep the docs-only gate as markers-only at an event.
