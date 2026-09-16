"""Optional foreground processes. Receipts and clone exclusion are independent of boards."""
import argparse
import contextlib
import datetime as dt
import errno
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid

from agentlane.board import BoardError


class LockBusy(OSError):
    """Only a conflicting OS lock, not a missing/inaccessible lock file."""


def add_parser(sub):
    parser = sub.add_parser("worker", help="supervise explicit local foreground worker commands")
    commands = parser.add_subparsers(dest="worker_command", required=True)
    run = commands.add_parser("run", help="run argv in a separate clone until it exits")
    run.add_argument("--clone", required=True)
    run.add_argument("--name", required=True)
    run.add_argument("--task", help="unverified association only; does not claim or complete work")
    run.add_argument("--stdin-file", help="feed this local file as stdin; otherwise use EOF")
    run.add_argument("--timeout", type=float, help="positive seconds before stopping the process tree")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="-- executable argument ...")
    commands.add_parser("list", help="read local run status; exit is not task completion")
    show = commands.add_parser("show", help="read a run receipt and local log paths")
    show.add_argument("run_id")
    resolve = commands.add_parser("resolve", help="acknowledge manual orphan cleanup and release its clone")
    resolve.add_argument("run_id")
    resolve.add_argument("--acknowledge-stopped", action="store_true",
                         help="assert you verified that this run and all descendants have stopped")
    for command in commands.choices.values():
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git_paths(cwd):
    # Git routing inherited from hooks must not redirect clone identity checks.
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)

    def query(option):
        result = subprocess.run(["git", "rev-parse", option], cwd=cwd, env=env,
                                capture_output=True, text=True)
        if result.returncode:
            raise BoardError("Worker commands require a local Git working tree: %s" % cwd)
        return Path(cwd, result.stdout.strip()).resolve()

    return query("--show-toplevel"), query("--git-common-dir")


def sync_directory(path):
    # Windows has no stdlib directory fsync. File contents are flushed before replace.
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers can briefly deny replacement of an open receipt.
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


@contextlib.contextmanager
def clone_lock(common, create=False):
    """Dedicated OS lock, never the checkout lock and never inherited by the worker."""
    path = common / "agentlane-worker.lock"
    with path.open("a+b" if create else "r+b") as stream:
        if create and path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise LockBusy(str(error)) from error
            raise
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_clone(common):
    # Keep lock acquisition errors distinct from errors inside the protected operation.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(clone_lock(common, create=True))
        except OSError as error:
            raise BoardError("Clone worker lock is active or inaccessible: %s (%s)" % (common, error))
        yield


def receipt_path(registry, run_id):
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise BoardError("Invalid worker run ID; use agentlane worker list.")
    return registry / run_id / "receipt.json"


def status(path):
    receipt = read_json(path)
    if receipt["status"] != "running":
        return receipt
    common = Path(receipt["git_common_dir"])
    try:
        with clone_lock(common):
            # Re-read under the lock: the supervisor may just have finished.
            receipt = read_json(path)
            if receipt["status"] == "running":
                receipt["status"] = "unknown"
    except LockBusy:
        # A lock alone does not identify a run. Require its durable marker too.
        try:
            marker = read_json(common / "agentlane-worker.json")
            if marker["run_id"] != receipt["run_id"]:
                receipt["status"] = "unknown"
        except (OSError, ValueError, KeyError):
            receipt["status"] = "unknown"
    except OSError:
        receipt["status"] = "unknown"
    return receipt


@contextlib.contextmanager
def interruption():
    """Defer signals during spawn/receipt writes, then stop the child in the wait loop."""
    received = []
    previous = {}

    def handler(number, frame):
        received.append(number)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.signal(number, handler)
    try:
        yield received
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def stop_tree(child):
    """Stop the process group/tree before reaping its leader. Return cleanup evidence."""
    if os.name == "nt":
        # CREATE_NO_WINDOW children cannot reliably receive console control events.
        # taskkill /T enumerates descendants while the parent still exists.
        try:
            result = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                    capture_output=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            stopped = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            stopped = False
        if child.poll() is None:
            try:
                child.kill()
            except OSError:
                stopped = False
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
            stopped = True
        except ProcessLookupError:
            stopped = True
        except OSError:
            stopped = False
            if child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return stopped


def run_worker(args, registry, coordinator_common):
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command or not command[0] or not args.name.strip():
        raise BoardError("Provide a worker name and explicit argv after --.")
    if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
        raise BoardError("Worker timeout must be a finite positive number.")
    clone, common = git_paths(Path(args.clone).resolve())
    if os.path.samefile(common, coordinator_common):
        raise BoardError("Worker target must be a separate clone, not the coordinator or its linked worktrees.")
    marker_path = common / "agentlane-worker.json"
    with exclusive_clone(common), contextlib.ExitStack() as files:
        if marker_path.exists():
            raise BoardError("Clone has an unresolved worker marker: %s. Verify orphan processes manually, "
                             "then use worker resolve in the original coordinator." % marker_path)
        stdin = files.enter_context(open(args.stdin_file, "rb")) if args.stdin_file else subprocess.DEVNULL
        run_id = uuid.uuid4().hex
        directory = registry / run_id
        directory.mkdir(parents=True, mode=0o700)
        sync_directory(registry.parent)
        sync_directory(registry)
        stdout = files.enter_context((directory / "stdout.log").open("wb"))
        stderr = files.enter_context((directory / "stderr.log").open("wb"))
        path = directory / "receipt.json"
        receipt = {"run_id": run_id, "name": args.name, "clone": str(clone),
                   "git_common_dir": str(common), "task": args.task, "task_verified": False,
                   "command": command, "started": timestamp(), "finished": None,
                   "supervisor_pid": os.getpid(), "child_pid": None, "exit_code": None,
                   "status": "running", "stdout_log": str(directory / "stdout.log"),
                   "stderr_log": str(directory / "stderr.log"), "timeout": args.timeout}
        child = None
        clear_marker = False
        exit_status = 1
        with interruption() as signals:
            atomic_json(path, receipt)
            atomic_json(marker_path, {"run_id": run_id, "receipt": str(path)})
            try:
                env = dict(os.environ)
                env.pop("AGENTLANE_LOCK_HELD", None)
                options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                           else {"start_new_session": True})
                child = subprocess.Popen(command, shell=False, cwd=clone, env=env, stdin=stdin,
                                         stdout=stdout, stderr=stderr, close_fds=True, **options)
                receipt["child_pid"] = child.pid
                atomic_json(path, receipt)
                deadline = time.monotonic() + args.timeout if args.timeout else None
                while True:
                    reason = "signal" if signals else "timeout" if deadline and time.monotonic() >= deadline else None
                    if reason:
                        clear_marker = stop_tree(child)
                        receipt.update(status="interrupted", reason=reason)
                        exit_status = 124 if reason == "timeout" else 130
                        break
                    try:
                        code = child.wait(timeout=0.1)
                        receipt["status"] = "exited" if code == 0 else "failed"
                        exit_status = code if code >= 0 else 1
                        # Also stop descendants left in the POSIX foreground group.
                        clear_marker = True if os.name == "nt" else stop_tree(child)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except (OSError, ValueError, KeyboardInterrupt) as error:
                receipt.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                               error=str(error))
                exit_status = 130 if isinstance(error, KeyboardInterrupt) else 1
                clear_marker = stop_tree(child) if child is not None else True
            finally:
                # Any unexpected supervisor error still attempts cleanup and retains the marker.
                if child is not None and child.poll() is None:
                    stop_tree(child)
                receipt["exit_code"] = child.returncode if child is not None else None
                receipt["finished"] = timestamp()
                receipt["cleanup_confirmed"] = clear_marker
                if not clear_marker:
                    receipt["status"] = "unknown"
                for stream in (stdout, stderr):
                    stream.flush()
                    os.fsync(stream.fileno())
                atomic_json(path, receipt)
                if clear_marker:
                    marker_path.unlink()
                    sync_directory(common)
        emit(args, receipt)
        return exit_status if clear_marker else 1


def resolve(args, registry):
    if not args.acknowledge_stopped:
        raise BoardError("Verify the worker and ALL descendants have stopped, then pass --acknowledge-stopped. "
                         "A missing or reused PID is not proof. See docs/workers.md.")
    path = receipt_path(registry, args.run_id)
    receipt = read_json(path)
    common = Path(receipt["git_common_dir"])
    with exclusive_clone(common):
        marker_path = common / "agentlane-worker.json"
        marker = read_json(marker_path)
        if marker.get("run_id") != args.run_id or Path(marker["receipt"]).resolve() != path.resolve():
            raise BoardError("Clone marker belongs to a different run/coordinator; refusing resolution.")
        receipt = read_json(path)
        if receipt["status"] == "running":
            receipt["status"] = "unknown"
        receipt["resolution"] = {"acknowledged_stopped": timestamp(), "operator_pid": os.getpid()}
        atomic_json(path, receipt)
        marker_path.unlink()
        sync_directory(common)
    emit(args, receipt)
    return 0


def emit(args, data):
    if args.json:
        print(json.dumps(data))
    elif isinstance(data, list):
        for receipt in data:
            print("%s  %s  %s  task=%s (unverified)" %
                  (receipt["run_id"], receipt["name"], receipt["status"], receipt["task"] or "-"))
        if not data:
            print("No local worker runs.")
    else:
        print("%s  %s  %s (process status; not task completion)" %
              (data["run_id"], data["name"], data["status"]))
        print(json.dumps(data, indent=2))


def dispatch(args):
    _, common = git_paths(Path.cwd())
    registry = common / "agentlane-workers"
    if args.worker_command == "run":
        return run_worker(args, registry, common)
    if args.worker_command == "resolve":
        return resolve(args, registry)
    if args.worker_command == "show":
        data = status(receipt_path(registry, args.run_id))
    else:
        data = [status(path) for path in sorted(registry.glob("*/receipt.json"))]
    emit(args, data)
    return 0
