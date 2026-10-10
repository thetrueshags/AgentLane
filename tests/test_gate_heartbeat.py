"""Explicit heartbeat concurrency against real CLI processes and a local remote."""
import json
import datetime as dt
import os
from pathlib import Path
import subprocess
import shlex
import sys
import time
from unittest.mock import patch

from agentlane import board as core
from tests.support import BOARD, ROOT, Fixture, TestCase, sh, stop_process_tree


class GateHeartbeatTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.board_branch = "board"
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "test")
        Path(self.a, ".harness/gate/code.sh").write_text(
            '#!/bin/sh\n"$AGENTLANE_PYTHON" - <<\'PY\'\n'
            'from pathlib import Path\nimport time\n'
            'Path(".git/gate-ready").write_text("ready")\n'
            'while not Path(".git/gate-release").exists():\n'
            '    time.sleep(0.02)\n'
            'raise SystemExit(int(Path(".git/gate-exit").read_text()) if Path(".git/gate-exit").exists() else 0)\n'
            'PY\n', encoding="utf-8")
        cfg_path = Path(self.a, ".harness/config.json")
        cfg = json.loads(cfg_path.read_text())
        cfg["require_review"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        sh(["git", "add", ".harness"], self.a)
        sh(["git", "commit", "-qm", "barrier gate"], self.a)
        sh(["git", "push", "-q", "origin", "main"], self.a)
        result = self.fx.data(self.fx.board("alice", "take", "--new", "Auth",
                                            "--paths", "src/auth/**"))
        self.claim = result["claim"]
        # Age the fixture explicitly; no race against wall-clock rollover.
        self.board = core.Board(core.Repo(self.a))
        def age_claim(board):
            claim = board.claim(self.claim["id"])
            claim["created"] = core.iso(core.now() - dt.timedelta(minutes=3))
            claim["last_heartbeat"] = core.iso(core.now() - dt.timedelta(minutes=2))
            board.save_claim(claim)
            return claim
        self.claim = self.board.txn("test: age heartbeat", age_claim)

    def start_gate(self, *command):
        env = dict(os.environ, BOARD_MEMBER="alice", BOARD_AGENT="test")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, BOARD, "--json", *command],
                                   cwd=self.a, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        self.addCleanup(self.stop_gate, process)
        deadline = time.monotonic() + 20
        while not Path(self.a, ".git/gate-ready").exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail("gate exited before readiness: %s\n%s" % (stdout, stderr))
            self.assertLess(time.monotonic(), deadline, "gate did not reach readiness")
            time.sleep(0.02)
        return process

    def stop_gate(self, process):
        Path(self.a, ".git/gate-release").touch()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stop_process_tree(process)
            process.communicate(timeout=10)

    def remote_claim(self):
        result = sh(["git", "show", "%s:claims/%s.json" % (self.board_branch, self.claim["id"])], self.fx.origin)
        return json.loads(result.stdout)

    def test_explicit_heartbeat_persists_while_gate_holds_checkout_lock(self):
        process = self.start_gate("gate", "--all")
        denied = self.fx.board("alice", "sync", check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("Another AgentLane command", denied.stdout)
        heartbeat = self.fx.board("alice", "heartbeat", "--force", check=False)
        self.assertEqual(heartbeat.returncode, 0, heartbeat.stdout + heartbeat.stderr)
        self.assertEqual(self.fx.data(heartbeat)["written"], 1)
        saved = self.remote_claim()
        self.assertEqual(saved["lease"], self.claim["lease"])
        self.assertEqual(saved["expires"], self.claim["expires"])
        self.assertEqual(saved["owner"], "alice")
        self.assertGreater(saved["last_heartbeat"], self.claim["last_heartbeat"])
        Path(self.a, ".git/gate-release").touch()
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertTrue(json.loads(stdout)["ok"])

    def test_installed_push_hook_allows_heartbeat_but_still_refuses_checkout_mutations(self):
        self.fx.board("alice", "install")
        hook = Path(self.a, ".git/hooks/pre-push")
        hook.write_text(hook.read_text().replace("set -euo pipefail", "set -euo pipefail\nprintf 'called' > .git/custom-hook-called"), encoding="utf-8")
        process = self.start_gate("gate", "--all")
        heartbeat = self.fx.board("alice", "heartbeat", "--force", check=False)
        self.assertEqual(heartbeat.returncode, 0, heartbeat.stdout + heartbeat.stderr)
        self.assertGreater(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        self.assertEqual(Path(self.a, ".git/custom-hook-called").read_text(), "called")
        main_update = "refs/heads/main %s refs/heads/main %s\n" % ("1" * 40, "2" * 40)
        board_update = "refs/heads/board %s refs/heads/board %s\n" % ("1" * 40, "2" * 40)
        for updates in (main_update, board_update + main_update, "malformed\n", ""):
            with self.subTest(updates=updates):
                denied = self.fx.board("alice", "check-push", check=False, input_text=updates)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("Another AgentLane command", denied.stdout)
        self.finish_gate(process)
        denied = self.fx.board("alice", "check-push", check=False, input_text=main_update)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("direct pushes", denied.stdout)

    def test_installed_hook_uses_custom_board_branch_from_project_checkout(self):
        self.board_branch = "team-board"
        sh(["git", "update-ref", "refs/heads/" + self.board_branch, "refs/heads/board"], self.fx.origin)
        config_path = Path(self.a, ".harness/config.json")
        cfg = json.loads(config_path.read_text())
        cfg["board_branch"] = self.board_branch
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        sh(["git", "add", ".harness/config.json"], self.a)
        sh(["git", "commit", "-qm", "custom board branch"], self.a)
        self.fx.board("alice", "install")
        process = self.start_gate("gate", "--all")
        self.fx.board("alice", "heartbeat", "--force")
        self.assertGreater(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        self.finish_gate(process)

    def candidate(self, approved=False):
        self.base = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        Path(self.a, "src/auth/login.py").write_text("reviewed change\n", encoding="utf-8")
        sh(["git", "add", "src/auth/login.py"], self.a)
        sh(["git", "commit", "-qm", "candidate"], self.a)
        self.sha = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        sh(["git", "push", "-q", "origin", self.claim["branch"]], self.a)
        if approved:
            reviewer = self.fx.clone("bob")
            self.fx.board("bob", "join", "--name", "bob", "--agent", "qa")
            sh(["git", "checkout", "-q", "--detach", self.sha], reviewer)
            self.fx.board("bob", "approve", self.claim["id"], "--commit", self.sha,
                          "--base", self.base, "--evidence", "fixture candidate checked")

    def finish_gate(self, process, expected=0):
        Path(self.a, ".git/gate-release").touch()
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, expected, stdout + stderr)
        return json.loads(stdout)

    def test_heartbeat_during_done_preserves_exact_approval_and_landing(self):
        self.candidate(approved=True)
        process = self.start_gate("done", self.claim["id"])
        prepared = Path(self.a, ".git/board-wt/claims", self.claim["id"] + ".json").read_bytes()
        self.fx.board("alice", "heartbeat", "--force")
        self.assertEqual(Path(self.a, ".git/board-wt/claims", self.claim["id"] + ".json").read_bytes(), prepared)
        self.assertGreater(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        result = self.finish_gate(process)
        self.assertEqual(result["landed"]["after"], self.sha)
        self.assertIn("approval", result["landed"])
        self.assertEqual(sh(["git", "rev-parse", "main"], self.fx.origin).stdout.strip(), self.sha)

    def test_heartbeat_during_done_cannot_replace_independent_approval(self):
        self.candidate()
        process = self.start_gate("done", self.claim["id"])
        self.fx.board("alice", "heartbeat", "--force")
        result = self.finish_gate(process, expected=1)
        self.assertIn("Independent approval required", result["error"])
        self.assertEqual(sh(["git", "rev-parse", "main"], self.fx.origin).stdout.strip(), self.base)
        self.assertEqual(self.remote_claim()["lease"], self.claim["lease"])

    def test_heartbeat_at_atomic_publication_retries_gate_and_exact_approval(self):
        self.candidate(approved=True)
        ready = Path(self.fx.tmp, "push-ready")
        release = Path(self.fx.tmp, "push-release")
        barrier = Path(self.fx.tmp, "push-barrier.py")
        barrier.write_text("from pathlib import Path\nimport time\n"
                           "Path(%r).touch()\nwhile not Path(%r).exists():\n    time.sleep(0.02)\n" %
                           (str(ready), str(release)), encoding="utf-8")
        hook = Path(self.fx.origin, "hooks/pre-receive")
        hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                        "  if [ \"$ref\" = refs/heads/main ]; then %s %s; fi\n"
                        "done\n" % (shlex.quote(sys.executable.replace("\\", "/")),
                                     shlex.quote(str(barrier).replace("\\", "/"))), encoding="utf-8")
        hook.chmod(0o700)
        self.addCleanup(release.touch)
        process = self.start_gate("done", self.claim["id"])
        Path(self.a, ".git/gate-release").touch()
        deadline = time.monotonic() + 20
        while not ready.exists():
            self.assertIsNone(process.poll(), "done exited before publication barrier")
            self.assertLess(time.monotonic(), deadline, "done did not reach publication")
            time.sleep(0.02)
        self.fx.board("alice", "heartbeat", "--force")
        self.assertGreater(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        self.assertFalse(Path(self.a, ".git/board-wt/claims", self.claim["id"] + ".json").exists())
        release.touch()
        result = self.finish_gate(process)
        self.assertEqual(len(result["log"]), 2, result)
        self.assertEqual(result["landed"]["after"], self.sha)
        self.assertIn("approval", result["landed"])

    def test_failed_gate_retains_heartbeat_and_releases_checkout_lock(self):
        process = self.start_gate("gate", "--all")
        self.fx.board("alice", "heartbeat", "--force")
        Path(self.a, ".git/gate-exit").write_text("1")
        self.assertFalse(self.finish_gate(process, expected=1)["ok"])
        self.fx.board("alice", "status")
        self.assertGreater(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])

    def test_killed_gate_releases_checkout_lock_and_does_not_renew_automatically(self):
        process = self.start_gate("gate", "--all")
        stop_process_tree(process)
        process.communicate(timeout=10)
        self.fx.board("alice", "status")
        self.assertEqual(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        self.fx.board("alice", "heartbeat", "--force")

    def test_explicit_board_directory_retains_exclusive_checkout_lock(self):
        process = self.start_gate("gate", "--all")
        result = self.fx.board("alice", "--board-dir", str(Path(self.a, ".git/board-wt")),
                               "heartbeat", "--force", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another AgentLane command", result.stdout)
        self.finish_gate(process)

    def test_ttl_and_missing_heartbeat_still_expire_claims(self):
        original = self.remote_claim()
        for boundary in (core.parse_iso(original["expires"]),
                         core.parse_iso(original["last_heartbeat"]) + dt.timedelta(minutes=15)):
            with self.subTest(boundary=boundary), patch.object(core, "now", return_value=boundary):
                args = core.build_parser().parse_args(["heartbeat", "--force"])
                with self.assertRaisesRegex(core.BoardError, "expired"):
                    core.cmd_heartbeat(args, core.Repo(self.a))
            self.assertEqual(self.remote_claim(), original)

    def test_heartbeat_cannot_renew_another_owners_recovered_lease_during_gate(self):
        self.fx.clone("bob")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "test")
        process = self.start_gate("gate", "--all")
        other_board = core.Board(core.Repo(self.fx.clones["bob"]))
        def abandon(board):
            claim = board.claim(self.claim["id"])
            claim["created"] = core.iso(core.now() - dt.timedelta(minutes=20))
            claim["last_heartbeat"] = claim["created"]
            board.save_claim(claim)
        other_board.txn("test: abandon old claim", abandon)
        recovered = self.fx.data(self.fx.board("bob", "take", self.claim["id"]))["claim"]
        self.assertNotEqual(recovered["lease"], self.claim["lease"])
        heartbeat = self.fx.data(self.fx.board("alice", "heartbeat", "--force"))
        self.assertEqual(heartbeat["written"], 0)
        self.assertEqual(self.remote_claim(), recovered)
        self.finish_gate(process)

    def test_heartbeat_lock_serializes_heartbeats_independently_of_gate(self):
        process = self.start_gate("gate", "--all")
        repo = core.Repo(self.a)
        with core.checkout_lock(repo, "agentlane-heartbeat.lock"), patch.dict(os.environ):
            os.environ.pop("AGENTLANE_LOCK_HELD", None)
            denied = self.fx.board("alice", "heartbeat", "--force", check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("Another AgentLane command", denied.stdout)
        self.fx.board("alice", "heartbeat", "--force")
        self.finish_gate(process)

    def test_killed_heartbeat_lock_owner_releases_lock_without_renewal(self):
        gate = self.start_gate("gate", "--all")
        ready = Path(self.a, ".git/heartbeat-lock-ready")
        script = ("import sys,time\nfrom pathlib import Path\nsys.path.insert(0, %r)\n"
                  "from agentlane import board\n"
                  "with board.checkout_lock(board.Repo(), 'agentlane-heartbeat.lock'):\n"
                  "    Path(%r).touch()\n    while True: time.sleep(0.02)\n" % (ROOT, str(ready)))
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        holder = subprocess.Popen([sys.executable, "-c", script], cwd=self.a,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        def cleanup_holder():
            if holder.poll() is None:
                stop_process_tree(holder)
            holder.communicate(timeout=10)
        self.addCleanup(cleanup_holder)
        deadline = time.monotonic() + 20
        while not ready.exists():
            self.assertIsNone(holder.poll(), "heartbeat lock owner exited before readiness")
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        denied = self.fx.board("alice", "heartbeat", "--force", check=False)
        self.assertNotEqual(denied.returncode, 0)
        stop_process_tree(holder)
        holder.communicate(timeout=10)
        self.assertEqual(self.remote_claim()["last_heartbeat"], self.claim["last_heartbeat"])
        self.fx.board("alice", "heartbeat", "--force")
        self.finish_gate(gate)
