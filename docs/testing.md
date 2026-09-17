# Developing and diagnosing tests

Run `python tools/check` and `python tools/test` from the source checkout. The test runner
uses standard-library unittest discovery and prints the ten slowest tests, including setup
and cleanup. Ordinary `python -m unittest discover -v` remains supported.

To reproduce one failure, pass its full test ID:

```sh
python tools/test tests.test_review.ReviewTests.test_happy_path_creator_can_review_and_history_survives
```

To save evidence, add `--report test-results/local.jsonl`. Each test start, failure, skip
and finish is flushed immediately, followed by the final suite summary. Finish records
include duration and outcome; failures include tracebacks and failing subtest parameters.
An interrupted run retains its completed results and the ID of the test that was running.
The runner prints thread stacks if a test takes longer than two minutes.

## CI coverage and runtime

Linux runs the full suite on Python 3.9 and 3.12. Windows runs three disjoint partitions
on separate runners, each with a 15-minute limit. Every discovered test is assigned exactly
once on Windows, including newly added tests. Partitioning sorts full test IDs and takes
every third entry; adding tests can change their shard. Import errors fail every shard,
and duplicate IDs or empty selections fail rather than produce a green run.

Reproduce a Windows partition with:

```sh
python tools/test --shard-index 0 --shard-count 3 --report test-results/shard-0.jsonl
```

Indices are zero-based. Add `--list` to inspect the selection without running it. CI uploads
each job's JSONL report even after test failure. The existing Windows check name aggregates
all Windows shards and the Windows wheel job, and fails when any fail or are cancelled.
Linux Python 3.12 and the separate Windows wheel job install the built package into an isolated environment and exercise a standalone project.

## Writing tests that produce useful evidence

- Inherit from `tests.support.TestCase` and call `super().setUp()` when overriding setup.
  This isolates Git configuration, repository routing variables and AgentLane identity.
  Tests can explicitly inject the environment they are testing after setup.
- Register `self.addCleanup(fixture.cleanup)` immediately after creating a `Fixture`.
  Cleanup then runs even when later setup fails. Temporary-directory cleanup handles
  Windows read-only Git objects and reports errors instead of silently leaving repositories.
- Fixtures copy an immutable seed built once per test process. Refs, configuration, Git
  objects and working files are private copies; member clones and real remote operations
  still run normally. The cache contains no members, claims, approvals or running workers.
  Git automatic maintenance is disabled only inside the tests' temporary configuration.
  The seed includes a tiny project test: changing `src/auth/health.txt` from `healthy`
  makes the default code gate fail. Successful landings therefore execute a real test
  runner, and a regression verifies both refusal on failure and landing after repair.
- Use `sh` for bounded CLI, Git and MCP commands. Its default deadline is 60 seconds;
  timeout errors name the command and working directory, preserve output and stop its tree.
  Long-running worker lifecycle tests own their processes and their cleanup explicitly.
- Use readiness and release signals for concurrency tests. Keep actual short deadlines
  in tests specifically exercising timeouts. A locking test must not depend on completing
  unrelated Git operations before a worker expires.
- For heartbeat and expiry boundaries, patch the board module's `now()` and call the command
  in process while retaining real Git transactions. Assert saved timestamps and board commits
  as well as command output. Sleeping inside a short claim lifetime makes runner speed part
  of the assertion and can reject a claim before the behavior under test is reached.
- Keep real Git coverage for ownership, hooks, races, review and atomic landing. Test pure
  validation directly. Improving runtime must not remove assertions or replace the behavior
  under test with mocks. Use the per-test timings to select further optimizations.

`tests/test_harness.py` checks partition completeness, real failure reporting, fixture
isolation, cleanup after setup failure, environment isolation and process-tree timeouts.
