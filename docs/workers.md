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

Use foreground commands that own and wait for their children. This is not a daemon launcher.
No provider settings, model, permission flags, authentication, prompt or resume arguments are
added. Supply any exact provider session resume arguments yourself. The environment is inherited
except for the internal `AGENTLANE_LOCK_HELD` bypass, which is removed. Do not launch from a shell
with Git routing overrides such as `GIT_DIR`/`GIT_WORK_TREE`: although clone identity discovery
ignores these overrides, child commands inherit your environment.

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
paths. They do not snapshot environment variables, credentials or prompt contents. **Arguments
and worker logs can themselves contain secrets**: use files or the worker's own authentication
mechanism for secrets rather than command-line arguments, and protect local Git metadata.

| Status | Meaning |
| --- | --- |
| `running` | The run's marker and active supervisor lock are present. |
| `exited` | The supervised command exited with zero. This does not mean its task is complete. |
| `failed` | Nonzero process exit or launch failure, including a missing executable. |
| `interrupted` | Timeout or foreground interruption stopped/reaped the command. `reason` distinguishes them. |
| `unknown` | The supervisor disappeared without an exit receipt, or tree cleanup could not be confirmed. |

A launch failure has no child PID or exit code. Unknown runs retain the last observed evidence;
read-only list/show do not invent an exit code or finish time. CLI exit codes are the child's
nonnegative exit code (1 for signal exits/launch errors), 124 for timeout, 130 for interruption,
and 1 when cleanup is uncertain. `--json` works before `worker`, or on its subcommands before `--`.

## Duplicate protection and recovery

A dedicated OS lifetime lock at `<target-common-git-dir>/agentlane-worker.lock` excludes other
supervisors. Names, path aliases, linked worktrees and different coordinator registries cannot
bypass it. The coordinator itself and its linked worktrees are refused as targets. Different
clones can run concurrently; there is no scheduler or global concurrency limit.

Before spawning, the supervisor durably writes `<target-common-git-dir>/agentlane-worker.json`,
which identifies the run and its coordinator receipt. A successful observed exit/cleanup clears
it. A hard-killed supervisor releases its OS lock, but **does not clear this marker**. Relaunch is
refused regardless of whether a recorded PID exists: a PID can be reused and descendants can live
after their parent dies. Do not delete marker or lock files to bypass this protection.

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
the direct child. Windows workers use `CREATE_NO_WINDOW`; cleanup uses `taskkill /T /F` while the
parent exists, then reaps it. Repeated interruption requests do not interrupt cleanup.

These mechanisms are not an OS sandbox. POSIX descendants that deliberately create another
session can escape the group. Windows `taskkill /T` cannot reliably recover descendants whose
parent already exited, so commands must not detach/background work, including on successful
exit. If tree termination fails, the marker stays and status becomes unknown. A supervisor
hard kill, OS crash or power loss can leave children or incomplete evidence; use the manual
recovery procedure. Network filesystems must support OS locks and atomic replacement; use
local Git metadata for this local facility.

## MCP

`board_worker_list` and `board_worker_show` expose the same read-only coordinator-local status
and receipt paths. There is no worker run/resolve MCP tool. Long-running execution belongs in
the foreground CLI: tying a worker lifetime to a synchronous board tool call would make client
timeouts and cancellation ambiguous and could leave unseen agent activity. MCP reads do not
refresh task claims or perform orphan reconciliation.
