"""Foreground worker lifecycle tests with real processes and local Git repositories."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.test_board import BOARD, ROOT, Fixture, sh
from tests.test_mcp_and_ci import mcp_session


@contextlib.contextmanager
def windows_processes(pids):
    """Hold process identities, await termination, and clean up even on a test failure."""
    import ctypes as ct
    from ctypes import wintypes as wt
    api = ct.WinDLL("kernel32", use_last_error=True)
    for name, result, arguments in [
        ("OpenProcess", wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
        ("WaitForSingleObject", wt.DWORD, [wt.HANDLE, wt.DWORD]),
        ("TerminateProcess", wt.BOOL, [wt.HANDLE, wt.UINT]),
        ("CloseHandle", wt.BOOL, [wt.HANDLE]),
    ]:
        function = getattr(api, name)
        function.restype, function.argtypes = result, arguments
    handles = []
    try:
        for pid in pids:
            handle = api.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | TERMINATE
            if not handle:
                raise ct.WinError(ct.get_last_error())
            handles.append(handle)
        yield lambda: all(api.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
    finally:
        for handle in handles:
            if api.WaitForSingleObject(handle, 0) == 258:
                api.TerminateProcess(handle, 1)
                api.WaitForSingleObject(handle, 5000)
            api.CloseHandle(handle)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agentlane-worker-")
        self.base = Path(self.tmp.name)
        self.coordinator = self.base / "coordinator"
        self.coordinator.mkdir()
        sh(["git", "init", "-q", "-b", "main"], str(self.coordinator))
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "initial"], str(self.coordinator))
        self.clone = self.base / "worker with spaces"
        sh(["git", "clone", "-q", str(self.coordinator), str(self.clone)], str(self.base))
        self.env = dict(os.environ, PYTHONPATH=ROOT)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
            process.communicate(timeout=20)
        self.tmp.cleanup()

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, BOARD, "--json", "worker", *map(str, args)],
                              cwd=cwd or self.coordinator, env=self.env, capture_output=True,
                              text=True, timeout=25)

    def run_worker(self, code="pass", options=(), argv=(), clone=None, cwd=None):
        return self.cli("run", "--clone", clone or self.clone, "--name", "engineer", *options,
                        "--", sys.executable, "-c", code, *argv, cwd=cwd)

    def start(self, clone=None, code=None, options=(), prefix=None):
        command = [sys.executable, BOARD] if prefix is None else prefix
        process = subprocess.Popen(command + ["--json", "worker", "run", "--clone", str(clone or self.clone),
                                   "--name", "engineer", *options, "--", sys.executable, "-c",
                                   code or "import time; print('ready', flush=True); time.sleep(60)"],
                                   cwd=self.coordinator, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        return process

    def wait_for(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("Timed out waiting for worker evidence")

    def running(self, count=1):
        def read():
            runs = json.loads(self.cli("list").stdout)
            return runs if len(runs) == count and all(r["child_pid"] and r["status"] == "running" for r in runs) else None
        return self.wait_for(read)

    def test_success_literal_arguments_stdin_logs_and_no_board(self):
        prompt = self.base / "prompt with spaces.txt"
        prompt.write_bytes(b"literal $() 'prompt'\nsecond line\n")
        literals = ["a b", 'a"b', "$(echo no)", "; exit 9", "", "--json", "back\\slash"]
        code = ("import json,os,sys; print(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),"
                "'stdin':sys.stdin.read(),'bypass':os.environ.get('AGENTLANE_LOCK_HELD')})); "
                "print('separate error',file=sys.stderr)")
        self.env["AGENTLANE_LOCK_HELD"] = "must not reach child"
        result = self.run_worker(code, options=("--stdin-file", str(prompt), "--task", "AL-123"), argv=literals)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "exited")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["task"], "AL-123")
        self.assertFalse(receipt["task_verified"])
        output = json.loads(Path(receipt["stdout_log"]).read_text())
        self.assertEqual(output["args"], literals)
        self.assertEqual(Path(output["cwd"]), self.clone.resolve())
        self.assertEqual(output["stdin"], prompt.read_text())
        self.assertIsNone(output["bypass"])
        self.assertEqual(Path(receipt["stderr_log"]).read_text(), "separate error\n")
        self.assertNotIn("must not reach child", result.stdout)
        self.assertEqual(json.loads(self.cli("show", receipt["run_id"]).stdout), receipt)
        self.assertFalse((self.coordinator / ".harness").exists())
        self.assertFalse((self.clone / ".harness").exists())
        for repo in (self.clone, self.coordinator):
            self.assertEqual(sh(["git", "status", "--porcelain"], str(repo)).stdout, "")
            self.assertNotIn("board", sh(["git", "branch", "-a"], str(repo)).stdout)
        self.assertFalse((self.clone / ".git/agentlane-worker.json").exists())
        human = subprocess.run([sys.executable, BOARD, "worker", "show", receipt["run_id"]],
                               cwd=self.coordinator, capture_output=True, text=True)
        self.assertIn("not task completion", human.stdout)

    def test_unicode_clone_path_preserves_working_directory(self):
        clone = self.base / "worker spaces \u03bb"
        sh(["git", "clone", "-q", str(self.coordinator), str(clone)], str(self.base))
        wrong_clone = self.base / "worker spaces \u03bb".encode("utf-8").decode("cp1252")
        sh(["git", "clone", "-q", str(self.coordinator), str(wrong_clone)], str(self.base))
        result = self.run_worker("import json,os; print(json.dumps(os.getcwd()))", clone=clone)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "exited")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertEqual(Path(json.loads(Path(receipt["stdout_log"]).read_text())), clone.resolve())
        self.assertFalse((clone / ".git/agentlane-worker.json").exists())

    def test_failed_exit_and_missing_executable_have_receipts(self):
        failed = self.run_worker("import sys; print('before failure'); sys.exit(7)")
        receipt = json.loads(failed.stdout)
        self.assertEqual(failed.returncode, 7)
        self.assertEqual((receipt["status"], receipt["exit_code"]), ("failed", 7))
        missing = self.cli("run", "--clone", self.clone, "--name", "engineer", "--",
                           str(self.base / "nonexistent-executable"))
        receipt = json.loads(missing.stdout)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(receipt["status"], "failed")
        self.assertIsNone(receipt["child_pid"])
        self.assertIsNone(receipt["exit_code"])
        self.assertIsNotNone(receipt["finished"])
        self.assertEqual(len(json.loads(self.cli("list").stdout)), 2)
        self.assertEqual(self.run_worker().returncode, 0)

    def test_child_environment_drops_coordinator_routing_bypasses_and_identity(self):
        removed = {"AGENTLANE_LOCK_HELD", "BOARD_LAND", "BOARD_SCAFFOLD", "BOARD_MEMBER", "BOARD_AGENT",
                   "BOARD_REPO_ROOT", "BOARD_PATHS", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
                   "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                   "GIT_QUARANTINE_PATH", "GIT_PREFIX", "GIT_SHALLOW_FILE", "GIT_NAMESPACE", "GIT_CONFIG",
                   "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
        self.env.update({key: "coordinator-context" for key in removed})
        self.env.update(GIT_DIR=str(self.coordinator / ".git"), GIT_COMMON_DIR=str(self.coordinator / ".git"),
                        GIT_WORK_TREE=str(self.coordinator), GIT_INDEX_FILE=str(self.coordinator / ".git/index"),
                        GIT_OBJECT_DIRECTORY=str(self.coordinator / ".git/objects"),
                        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.worktree", GIT_CONFIG_VALUE_0=str(self.coordinator),
                        BOARD_LAND="1", BOARD_SCAFFOLD="1", BOARD_MEMBER="coordinator", BOARD_AGENT="coordinator")
        config = self.base / "user.gitconfig"
        config.write_text("")
        preserved = {"WORKER_MODEL": "user-model", "WORKER_PERMISSIONS": "user-permissions",
                     "SSH_AUTH_SOCK": "test-auth-socket", "GIT_CONFIG_GLOBAL": str(config)}
        self.env.update(preserved)
        code = ("import json,os,subprocess; "
                "print(json.dumps({'env':{k:os.environ.get(k) for k in %r}, "
                "'root':subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip(), "
                "'remote':subprocess.check_output(['git','ls-remote','origin','refs/heads/main'],text=True).strip(), "
                "'common':subprocess.check_output(['git','rev-parse','--git-common-dir'],text=True).strip()}))"
                % sorted(removed | set(preserved)))
        result = self.run_worker(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        output = json.loads(Path(receipt["stdout_log"]).read_text())
        self.assertEqual(Path(output["root"]), self.clone.resolve())
        expected_main = sh(["git", "rev-parse", "HEAD"], str(self.coordinator)).stdout.strip()
        self.assertEqual(output["remote"], expected_main + "\trefs/heads/main")
        self.assertEqual((self.clone / output["common"]).resolve(), self.clone / ".git")
        for key in removed:
            self.assertIsNone(output["env"][key], key)
        for key, value in preserved.items():
            self.assertEqual(output["env"][key], value)

    def test_corrupt_receipts_fail_usefully_in_list_and_show(self):
        receipt = json.loads(self.run_worker().stdout)
        path = Path(receipt["stdout_log"]).with_name("receipt.json")
        invalid = [[], None, "text", {}, {"status": "running"}]
        for key, value in (("git_common_dir", []), ("status", {}), ("command", [None]),
                           ("name", 42), ("child_pid", True), ("run_id", "wrong-run")):
            invalid.append(dict(receipt, **{key: value}))
        try:
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    for args in (("list",), ("show", receipt["run_id"])):
                        result = self.cli(*args)
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("Invalid local worker", json.loads(result.stdout)["error"])
                        self.assertIn("receipt.json", json.loads(result.stdout)["error"])
                        self.assertNotIn("Traceback", result.stderr)
            path.write_text("{broken JSON")
            result = self.cli("show", receipt["run_id"])
            self.assertEqual(result.returncode, 1)
            self.assertIn("Invalid local worker JSON", json.loads(result.stdout)["error"])
        finally:
            path.write_text(json.dumps(receipt))

    def test_refuses_self_worktree_invalid_inputs_and_reads_are_read_only(self):
        self.assertEqual(json.loads(self.cli("list").stdout), [])
        self.assertFalse((self.coordinator / ".git/agentlane-workers").exists())
        worktree = self.base / "coordinator-linked"
        sh(["git", "worktree", "add", "-q", "--detach", str(worktree)], str(self.coordinator))
        for clone in (self.coordinator, worktree):
            result = self.run_worker(clone=clone)
            self.assertIn("separate clone", result.stdout)
            self.assertEqual(result.returncode, 1)
        for timeout in ("0", "-1", "nan", "inf"):
            self.assertEqual(self.run_worker(options=("--timeout", timeout)).returncode, 1)
        self.assertEqual(self.cli("show", "../escape").returncode, 1)
        self.assertEqual(self.run_worker(options=("--stdin-file", "missing-file")).returncode, 1)

    def test_same_clone_alias_worktree_and_other_registry_refused(self):
        process = self.start(options=("--timeout", "5"))
        receipt = self.running()[0]
        worktree = self.base / "worker-linked"
        sh(["git", "worktree", "add", "-q", "--detach", str(worktree)], str(self.clone))
        for target in (self.clone, self.clone / ".." / self.clone.name, worktree):
            result = self.cli("run", "--clone", target, "--name", "different-name", "--",
                              sys.executable, "-c", "pass")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("lock", result.stdout)
        second_coordinator = self.base / "second-coordinator"
        sh(["git", "clone", "-q", str(self.coordinator), str(second_coordinator)], str(self.base))
        self.assertEqual(self.run_worker(cwd=second_coordinator).returncode, 1)
        self.assertEqual(self.cli("resolve", receipt["run_id"], "--acknowledge-stopped").returncode, 1)
        shown = json.loads(self.cli("show", receipt["run_id"]).stdout)
        self.assertEqual(shown["status"], "running")
        self.assertIn("ready", Path(shown["stdout_log"]).read_text())
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(json.loads(stdout)["status"], "interrupted", stderr)

    def test_simultaneous_same_clone_and_different_clones(self):
        first = self.start(options=("--timeout", "3"))
        second = self.start(options=("--timeout", "3"))
        outputs = [p.communicate(timeout=15) for p in (first, second)]
        self.assertEqual(sorted(p.returncode for p in (first, second)), [1, 124], outputs)
        other = self.base / "other-clone"
        sh(["git", "clone", "-q", str(self.coordinator), str(other)], str(self.base))
        processes = [self.start(clone=clone, options=("--timeout", "3")) for clone in (self.clone, other)]
        self.wait_for(lambda: len([r for r in json.loads(self.cli("list").stdout) if r["status"] == "running"]) == 2)
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(json.loads(stdout)["status"], "interrupted", stderr)

    def tree_code(self, exit_parent=False):
        # A descendant continually changes a file so cleanup can be proved without trusting a PID.
        pulse = self.base / "pulse"
        grandchild = ("import time; from pathlib import Path; p=Path(%r); "
                      "exec('while True:\\n p.write_text(str(time.time_ns()))\\n time.sleep(.05)')" % str(pulse))
        code = ("import subprocess,sys,time; from pathlib import Path; "
                "p=subprocess.Popen([sys.executable,'-c',%r]); "
                "Path(%r).write_text(str(p.pid)); print('tree ready',flush=True); "
                % (grandchild, str(self.base / "descendant.pid")))
        code += ("exec(%r)" % ("while not Path(%r).exists():\n time.sleep(.02)" % str(self.base / "exit-parent"))
                 if exit_parent else "time.sleep(60)")
        return pulse, code

    def assert_stopped(self, pulse):
        self.assertTrue(pulse.exists())
        first = pulse.read_bytes()
        time.sleep(0.25)
        self.assertEqual(pulse.read_bytes(), first, "descendant is still running")

    def test_timeout_stops_tree_and_allows_new_run(self):
        pulse, code = self.tree_code()
        process = self.start(code=code, options=("--timeout", "1.5"))
        stdout, stderr = process.communicate(timeout=20)
        receipt = json.loads(stdout)
        self.assertEqual(process.returncode, 124, stderr)
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["status"], "interrupted")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertIsNotNone(receipt["exit_code"])
        self.assert_stopped(pulse)
        self.assertEqual(self.run_worker().returncode, 0)

    def test_parent_exit_stops_ordinary_descendant_before_releasing_clone(self):
        pulse, code = self.tree_code(exit_parent=True)
        process = self.start(code=code)
        self.wait_for(pulse.exists)
        descendant = int((self.base / "descendant.pid").read_text())
        guard = windows_processes([descendant]) if os.name == "nt" else contextlib.nullcontext(None)
        with guard as stopped:
            (self.base / "exit-parent").touch()
            stdout, stderr = process.communicate(timeout=20)
            receipt = json.loads(stdout)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(receipt["status"], "exited")
            self.assertTrue(receipt["cleanup_confirmed"])
            if stopped is not None:
                self.assertTrue(stopped(), "descendant process handle did not become signaled")
            self.assert_stopped(pulse)
            self.assertFalse((self.clone / ".git/agentlane-worker.json").exists())
        self.assertEqual(self.run_worker().returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows job lifetime")
    def test_windows_supervisor_death_kills_child_and_descendant_but_keeps_marker(self):
        pulse, code = self.tree_code()
        process = self.start(code=code)
        receipt = self.running()[0]
        self.wait_for(pulse.exists)
        descendant = int((self.base / "descendant.pid").read_text())
        with windows_processes([receipt["child_pid"], descendant]) as stopped:
            process.kill()
            process.communicate(timeout=15)
            self.assertTrue(stopped(), "supervisor death left a job process alive")
            self.assert_stopped(pulse)
        shown = json.loads(self.cli("show", receipt["run_id"]).stdout)
        self.assertEqual(shown["status"], "unknown")
        self.assertIsNone(shown["exit_code"])
        self.assertTrue((self.clone / ".git/agentlane-worker.json").exists())
        self.assertEqual(self.run_worker().returncode, 1)
        self.assertEqual(self.cli("resolve", receipt["run_id"], "--acknowledge-stopped").returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows job containment failure")
    def test_windows_containment_failure_never_runs_uncontained_child(self):
        ran = self.base / "must-not-run"
        for failure in ("assignment", "resume"):
            with self.subTest(failure=failure):
                wrapper = self.base / "fail-containment.py"
                patch = ("original = WindowsJob.__init__\n"
                         "def initialize(self):\n original(self)\n"
                         " self.api.AssignProcessToJobObject = lambda *args: 0\n"
                         "WindowsJob.__init__ = initialize\n" if failure == "assignment" else
                         "def refuse(self,pid):\n raise OSError('injected resume failure')\n"
                         "WindowsJob._resume = refuse\n")
                wrapper.write_text("import runpy\nfrom agentlane.windows_job import WindowsJob\n" + patch +
                                   "runpy.run_path(%r,run_name='__main__')\n" % BOARD)
                process = self.start(code="from pathlib import Path; Path(%r).touch()" % str(ran),
                                     prefix=[sys.executable, str(wrapper)])
                stdout, stderr = process.communicate(timeout=20)
                receipt = json.loads(stdout)
                self.assertEqual(process.returncode, 1, stderr)
                self.assertEqual(receipt["status"], "failed")
                self.assertIsNotNone(receipt["child_pid"])
                self.assertIsNotNone(receipt["exit_code"])
                self.assertTrue(receipt["cleanup_confirmed"])
                self.assertFalse(ran.exists())
                self.assertEqual(self.run_worker().returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows job error cleanup")
    def test_windows_supervisor_error_after_resume_drains_entire_job(self):
        pulse, code = self.tree_code()
        trigger = self.base / "fail-supervisor"
        wrapper = self.base / "fail-after-resume.py"
        wrapper.write_text("import runpy,time\nfrom pathlib import Path\n"
                           "from agentlane.windows_job import WindowsJob\n"
                           "original = WindowsJob.contain\n"
                           "def contain(self,child):\n original(self,child)\n"
                           " while not Path(%r).exists():\n  time.sleep(.02)\n"
                           " raise OSError('injected supervisor failure after resume')\n"
                           "WindowsJob.contain = contain\nrunpy.run_path(%r,run_name='__main__')\n"
                           % (str(trigger), BOARD))
        process = self.start(code=code, prefix=[sys.executable, str(wrapper)])
        receipt = self.running()[0]
        self.wait_for(pulse.exists)
        descendant = int((self.base / "descendant.pid").read_text())
        with windows_processes([receipt["child_pid"], descendant]) as stopped:
            trigger.touch()
            stdout, stderr = process.communicate(timeout=20)
            receipt = json.loads(stdout)
            self.assertEqual(process.returncode, 1, stderr)
            self.assertEqual(receipt["status"], "failed")
            self.assertTrue(receipt["cleanup_confirmed"])
            self.assertTrue(stopped())
        self.assert_stopped(pulse)
        self.assertEqual(self.run_worker().returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows job cleanup uncertainty")
    def test_unconfirmed_job_cleanup_keeps_unknown_receipt_and_clone_marker(self):
        wrapper = self.base / "unconfirmed-cleanup.py"
        wrapper.write_text("import runpy\nfrom agentlane.windows_job import WindowsJob\n"
                           "original = WindowsJob.stop\n"
                           "def stop(self):\n original(self)\n return False\n"
                           "WindowsJob.stop = stop\nrunpy.run_path(%r,run_name='__main__')\n" % BOARD)
        process = self.start(code="pass", prefix=[sys.executable, str(wrapper)])
        stdout, stderr = process.communicate(timeout=20)
        receipt = json.loads(stdout)
        self.assertEqual(process.returncode, 1, stderr)
        self.assertEqual(receipt["status"], "unknown")
        self.assertFalse(receipt["cleanup_confirmed"])
        self.assertEqual(self.run_worker().returncode, 1)
        self.assertEqual(self.cli("resolve", receipt["run_id"], "--acknowledge-stopped").returncode, 0)

    def test_foreground_interruption_retains_logs_and_stops_tree(self):
        pulse, code = self.tree_code()
        if os.name == "nt":
            # CI may have no console for GenerateConsoleCtrlEvent. Deliver SIGINT inside
            # the real supervisor instead, exercising the same installed signal handler.
            wrapper = self.base / "interrupt.py"
            wrapper.write_text("import runpy,signal,sys,threading,time\n"
                               "def interrupt():\n time.sleep(2); signal.raise_signal(signal.SIGINT)\n"
                               "threading.Thread(target=interrupt,daemon=True).start()\n"
                               "sys.argv[0] = %r\nrunpy.run_path(%r,run_name='__main__')\n" % (BOARD, BOARD))
            process = self.start(code=code, prefix=[sys.executable, str(wrapper)])
        else:
            process = self.start(code=code)
            self.wait_for(pulse.exists)
            process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=20)
        receipt = json.loads(stdout)
        self.assertEqual(process.returncode, 130, stderr)
        self.assertEqual((receipt["status"], receipt["reason"]), ("interrupted", "signal"))
        self.assertIn("tree ready", Path(receipt["stdout_log"]).read_text())
        self.assert_stopped(pulse)

    def test_supervisor_hard_kill_is_unknown_and_requires_explicit_resolution(self):
        pulse = self.base / "orphan-pulse"
        code = ("import time; from pathlib import Path; p=Path(%r); "
                "exec('while True:\\n p.write_text(str(time.time_ns()))\\n time.sleep(.05)')" % str(pulse))
        process = self.start(code=code)
        receipt = self.running()[0]
        self.wait_for(pulse.exists)
        try:
            process.kill()
            process.communicate(timeout=15)
            shown = json.loads(self.cli("show", receipt["run_id"]).stdout)
            self.assertEqual(shown["status"], "unknown")
            self.assertIsNone(shown["exit_code"])
            self.assertEqual(self.run_worker().returncode, 1)
            self.assertEqual(self.cli("resolve", receipt["run_id"]).returncode, 1)
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(receipt["child_pid"]), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(receipt["child_pid"], signal.SIGKILL)
            time.sleep(0.2)
        self.assert_stopped(pulse)
        result = self.cli("resolve", receipt["run_id"], "--acknowledge-stopped")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "unknown")
        self.assertIn("resolution", json.loads(result.stdout))
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(json.loads(self.cli("show", receipt["run_id"]).stdout)["status"], "unknown")

    def test_mcp_reads_no_board_and_does_not_expose_launch(self):
        receipt = json.loads(self.run_worker().stdout)
        replies = mcp_session(str(self.coordinator), self.env, [
            {"id": 1, "method": "tools/list"},
            {"id": 2, "method": "tools/call", "params": {"name": "board_worker_list"}},
            {"id": 3, "method": "tools/call", "params": {"name": "board_worker_show",
                                                        "arguments": {"run_id": receipt["run_id"]}}},
        ])
        names = [tool["name"] for tool in replies[0]["result"]["tools"]]
        self.assertEqual(sorted(name for name in names if "worker" in name),
                         ["board_worker_list", "board_worker_show"])
        for reply in replies[1:]:
            self.assertFalse(reply["result"]["isError"], reply)
            self.assertIn(receipt["run_id"], reply["result"]["content"][0]["text"])


class WorkerBoardCompatibilityTests(unittest.TestCase):
    def test_child_board_command_uses_own_checkout_lock_and_task_is_unchanged(self):
        fx = Fixture()
        try:
            coordinator = fx.clone("coordinator")
            clone = fx.clone("worker")
            fx.board("coordinator", "init")
            fx.board("worker", "join", "--name", "worker", "--agent", "test")
            task = fx.data(fx.board("worker", "add", "Unclaimed work"))
            before = sh(["git", "rev-parse", "refs/heads/board"], fx.origin).stdout
            # This invokes Repo and the ordinary checkout lock while the worker lock is held.
            result = sh([sys.executable, BOARD, "--json", "worker", "run", "--clone", clone,
                         "--name", "worker", "--task", task["id"], "--timeout", "10", "--",
                         sys.executable, "-m", "agentlane", "--json", "whoami"], coordinator,
                        env={"PYTHONPATH": ROOT, "AGENTLANE_LOCK_HELD": "must be stripped",
                             "BOARD_MEMBER": "coordinator", "BOARD_AGENT": "coordinator-agent",
                             "BOARD_LAND": "1", "BOARD_SCAFFOLD": "1"})
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "exited")
            self.assertEqual(json.loads(Path(receipt["stdout_log"]).read_text())["member"], "worker")
            self.assertEqual(json.loads(Path(receipt["stdout_log"]).read_text())["agent"], "test")
            self.assertEqual(sh(["git", "rev-parse", "refs/heads/board"], fx.origin).stdout, before)
            self.assertEqual(fx.data(fx.board("worker", "show", task["id"]))["status"], "open")
        finally:
            fx.cleanup()
