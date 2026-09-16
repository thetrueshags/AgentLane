# Local foreground workers

Worker supervision is optional. From a coordinator Git checkout, run an explicit command
in a **separate clone**. No AgentLane board, membership or installation in either checkout
is required for supervision. Python 3.9+ and Git are required; the selected worker executable
must already be available.

```sh
agentlane worker run --clone ../worker-clone --name engineer \
  --task AL-12 --stdin-file ./prompt.txt --timeout 1800 -- python worker.py "literal argument"
agentlane worker list
agentlane --json worker show RUN_ID
```

Put worker arguments after `--`. The command is an argv array executed with `shell=False`
in the canonical target working-tree root. There is no shell interpolation by AgentLane;
the invoking shell still parses your CLI arguments. `--stdin-file` supplies the file's bytes
without embedding the prompt in a command. Its path is relative to the invoking directory.
Without it, stdin is EOF. stdout and stderr go to separate local files, not the terminal;
another terminal can inspect `worker list`, `worker show`, and the log paths while a run lives.

Use foreground commands. Remaining ordinary descendants are stopped when the command exits;
this is not a daemon launcher.
No provider settings, model, permission flags, authentication, prompt or resume arguments are
added. Supply any exact provider session resume arguments yourself. The environment is inherited
except for coordinator context: `AGENTLANE_LOCK_HELD`, `BOARD_LAND`, `BOARD_SCAFFOLD`, `BOARD_MEMBER`,
`BOARD_AGENT`, `BOARD_REPO_ROOT` and `BOARD_PATHS` are removed. Repository-local Git variables
(including `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE`, index/object/quarantine paths and inline
Git configuration overrides) are removed for both discovery and execution. Child Git commands
resolve the target clone, and child AgentLane commands use that clone's configured identity.
Unrelated model, permission and authentication settings, including user-wide Git configuration
and SSH credentials, are preserved.

`--name` labels this run; it does not register a member or change the target's identity.
`--task` is **unverified metadata**, even if it happens to name a real task. Supervision never
claims work, heartbeats, commits, hands off, lands, restarts or marks a task done. Installed
project workers can call normal AgentLane commands themselves using their own identity and
the usual project rules. The supervisor does not hold the checkout/board lock.

## Receipts and status

Each run has a random 32-character ID. The invoking coordinator's common Git directory contains
`agentlane-workers/RUN_ID/receipt.json`, `stdout.log` and `stderr.log`. Linked coordinator worktrees
share this local registry. Nothing is stored in the tracked tree or board or sent to a remote.
Receipts are atomically replaced after flushing file contents; directory updates are also fsynced
on POSIX. Windows uses flushed files and atomic replacement, without directory fsync support.

Receipts include worker name, canonical clone/common Git directory, unverified task, literal argv,
UTC start/finish times, supervisor/child PIDs, observed child exit code, timeout, status and log
paths. `cleanup_confirmed` means the supervisor observed cleanup of its job/process group and
reaped any spawned direct child; it does not certify task results or external services. New receipts
identify `containment` as `windows-job` or `posix-process-group` once launch containment succeeds.
They do not snapshot environment variables, credentials or prompt contents. **Arguments
and worker logs can themselves contain secrets**: use files or the worker's own authentication
mechanism for secrets rather than command-line arguments, and protect local Git metadata.

| Status | Meaning |
| --- | --- |
| `running` | The run's marker and active supervisor lock are present. |
| `exited` | The supervised command exited with zero and remaining ordinary descendants were stopped. This does not mean its task is complete. |
| `failed` | Nonzero process exit or launch failure, including a missing executable. |
| `interrupted` | Timeout or foreground interruption stopped/reaped the command. `reason` distinguishes them. |
| `unknown` | The supervisor disappeared without an exit receipt, or tree cleanup could not be confirmed. |

A failure before process creation has no child PID or exit code. Containment failures can
include the stopped child's PID and exit code. Unknown runs retain the last observed evidence;
read-only list/show do not invent an exit code or finish time. CLI exit codes are the child's
nonnegative exit code (1 for signal exits/launch errors), 124 for timeout, 130 for interruption,
and 1 when cleanup is uncertain. `--json` works before `worker`, or on its subcommands before `--`.
Malformed local receipts produce a CLI error identifying the file; inspect or restore that
receipt without deleting the target's unresolved marker.

## Duplicate protection and recovery

A dedicated OS lifetime lock at `<target-common-git-dir>/agentlane-worker.lock` excludes other
supervisors. Names, path aliases, linked worktrees and different coordinator registries cannot
bypass it. The coordinator itself and its linked worktrees are refused as targets. Different
clones can run concurrently; there is no scheduler or global concurrency limit.

Before spawning, the supervisor durably writes `<target-common-git-dir>/agentlane-worker.json`,
which identifies the run and its coordinator receipt. A successful observed exit/cleanup clears
it. A hard-killed supervisor releases its OS lock, but **does not clear this marker**. Relaunch is
refused regardless of whether a recorded PID exists: a PID can be reused and descendants can live
after their parent dies. On Windows, job containment normally terminates children on supervisor
death too, but without the supervisor's final observation the receipt stays unknown and the
marker remains. Do not delete marker or lock files to bypass this protection.

To recover an orphan, inspect the marker and receipt in the original coordinator. Using OS
process inspection, verify the command's identity/start time and all descendants, stop any
remaining processes, and inspect logs/working-tree changes. A missing parent PID alone is not
verification. If you cannot establish that all worker activity stopped, keep the clone blocked.
After verification, explicitly acknowledge it in that coordinator:

```sh
agentlane worker resolve RUN_ID --acknowledge-stopped
```

Resolution refuses while the supervisor lock is active or if the marker belongs to another run.
It records the acknowledgement and releases the marker; the historical process status remains
unknown when its exit cannot be proved. It does not terminate processes or certify task results.
The acknowledgement is an operator assertion, not a PID-based automatic reconciliation.

## Process lifetime limits

Keep the CLI in the foreground. Ctrl-C (and POSIX termination signals/Windows Ctrl-Break when
delivered to the supervisor) triggers cleanup and preserves logs and an exit receipt. A timeout
does the same. POSIX workers start in a new session; cleanup kills the process group and reaps
the direct child. Windows workers use `CREATE_NO_WINDOW | CREATE_SUSPENDED`. Before the initial
thread resumes, the supervisor assigns the child to an anonymous, non-inheritable Windows job
with kill-on-close enabled and no breakaway permission. Failure to establish containment stops
the suspended child; execution never falls back to an uncontained launch. Cleanup terminates
the job and waits for active processes to drain, then waits for the direct child's process
handle to signal. This covers ordinary descendants even after the parent exits. The job handle
is closed on every exit path; supervisor death also closes it at the OS level. See Microsoft's
[job object documentation](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Repeated interruption requests do not interrupt cleanup.

These mechanisms are not an OS sandbox. POSIX descendants that deliberately create another
session can escape the group. Work delegated to external services outside the process group or
Windows job is not supervised. If tree termination cannot be confirmed, the marker stays and
status becomes unknown. A supervisor hard kill before Windows job assignment can leave a
suspended child; a hard kill after assignment still leaves an unobserved exit. OS crashes or
power loss can also leave incomplete evidence. Use the manual recovery procedure in these cases.
Network filesystems must support OS locks and atomic replacement; use
local Git metadata for this local facility.

## MCP

`board_worker_list` and `board_worker_show` expose the same read-only coordinator-local status
and receipt paths. There is no worker run/resolve MCP tool. Long-running execution belongs in
the foreground CLI: tying a worker lifetime to a synchronous board tool call would make client
timeouts and cancellation ambiguous and could leave unseen agent activity. MCP reads do not
refresh task claims or perform orphan reconciliation.
