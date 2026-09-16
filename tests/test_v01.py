"""Release behavior and deterministic races using independent clones of local remotes."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.support import TestCase, BOARD, Fixture, load_board_module, sh


class ValidationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.m = load_board_module()

    def test_conservative_partial_prefix_and_case_overlap(self):
        for a, b in (("src/a*", "src/auth/**"), ("*.py", "src/a.py"), ("SRC/Auth/**", "src/auth/a.py")):
            self.assertTrue(self.m.globs_overlap(a, b), (a, b))
            self.assertTrue(self.m.globs_overlap(b, a), (a, b))
        self.assertFalse(self.m.globs_overlap("src/auth/**", "src/authz/**"))

    def test_path_normalization_rejects_escape_and_git_metadata(self):
        self.assertEqual(self.m.normalize_paths(["./src/auth/**", "src\\auth\\**"]), ["src/auth/**"])
        for path in ("../src", "/tmp/**", "C:/src", ".git/**", "src/../../x", "src\nx"):
            with self.subTest(path=path), self.assertRaises(self.m.BoardError):
                self.m.normalize_paths([path])

    def test_json_corruption_reports_filename(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "broken.json")
            path.write_text("{broken")
            with self.assertRaisesRegex(self.m.BoardError, "broken.json"):
                self.m.read_json(str(path))

    def test_version_help_and_json_error_outside_repo(self):
        with tempfile.TemporaryDirectory() as root:
            for args in (("--help",), ("--version",), ("take", "--help")):
                p = sh([sys.executable, BOARD, *args], root)
                self.assertIn("agentlane", p.stdout.lower())
            p = sh([sys.executable, BOARD, "status", "--json"], root, check=False)
            self.assertEqual(p.returncode, 1)
            self.assertFalse(json.loads(p.stdout)["ok"])
            p = sh([sys.executable, BOARD, "doctor", "--json"], root, check=False)
            self.assertEqual(p.returncode, 1)
            self.assertFalse(json.loads(p.stdout)["ok"])
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_slack_failure_hides_webhook(self):
        secret = "https://hooks.slack.com/services/secret-token"
        with patch.object(self.m.urllib.request, "urlopen", side_effect=OSError(secret)):
            with self.assertRaises(self.m.BoardError) as error:
                self.m.post_slack(secret, "hello")
        self.assertNotIn("secret-token", str(error.exception))


class WorkflowTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        for name in ("alice", "bob"):
            self.fx.board(name, "join", "--name", name, "--agent", "test")
        self.m = load_board_module()

    def commit(self, root, path, text="work\n"):
        target = Path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="\n") as f:
            f.write(text)
        sh(["git", "add", path], root)
        sh(["git", "commit", "-q", "-m", "work"], root)

    def edit_board(self, edit):
        board = self.m.Board(self.m.Repo(self.b))
        board.txn("test state change", edit)

    def test_new_cli_end_to_end_list_show_note_done(self):
        task = self.fx.data(self.fx.board("alice", "add", "Auth", "--paths", "src/auth/**", "--description", "An endpoint"))
        self.assertEqual(task["id"], "AL-1")
        self.assertEqual(len(self.fx.data(self.fx.board("alice", "list", "--available"))["tasks"]), 1)
        self.fx.board("alice", "take", task["id"])
        self.fx.board("alice", "note", task["id"], "API ready")
        view = self.fx.data(self.fx.board("alice", "show", task["id"]))
        self.assertEqual(view["notes"][0]["text"], "API ready")
        self.assertIsNotNone(view["updated"])
        self.assertEqual(len(self.fx.data(self.fx.board("alice", "list", "--mine"))["tasks"]), 1)
        self.commit(self.a, "src/auth/login.py")
        self.fx.board("alice", "sync")
        result = self.fx.data(self.fx.board("alice", "done", task["id"]))
        self.assertEqual(result["task"]["status"], "done")

    def test_install_preserves_custom_hook_and_gate(self):
        hook = Path(self.a, ".git/hooks/pre-push")
        hook.write_text("#!/bin/sh\n# existing checks\n", encoding="utf-8")
        gate = Path(self.a, ".harness/gate/code.sh")
        original = gate.read_bytes()
        result = self.fx.board("alice", "install", "--project", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Existing pre-push hook preserved", self.fx.data(result)["error"])
        self.assertIn("existing checks", hook.read_text())
        self.assertEqual(gate.read_bytes(), original)

    def test_local_lock_refuses_competing_process_and_releases(self):
        repo = self.m.Repo(self.a)
        with self.m.checkout_lock(repo):
            env = dict(os.environ)
            env.pop("AGENTLANE_LOCK_HELD", None)
            result = sh([sys.executable, BOARD, "status", "--json"], self.a, env=env, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Another AgentLane command", json.loads(result.stdout)["error"])
        self.fx.board("alice", "status")

    def test_released_claim_branch_push_is_refused(self):
        self.fx.board("alice", "install")
        self.fx.board("alice", "take", "--new", "Auth", "--paths", "src/auth/**")
        self.commit(self.a, "src/auth/login.py")
        self.fx.board("alice", "release")
        result = sh(["git", "push", "origin", "HEAD"], self.a, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active claim", result.stderr)

    def test_rejected_board_update_cannot_publish_revert(self):
        self.fx.board("alice", "take", "--new", "Auth", "--paths", "src/auth/**")
        self.commit(self.a, "src/auth/login.py")
        result = self.fx.data(self.fx.board("alice", "done"))
        landed = result["landed"]
        self.fx.reject_next_push_to("refs/heads/board")
        repo = self.m.Repo(self.b)
        repo.cfg["land_retries"] = 1
        args = self.m.build_parser().parse_args(["revert-failed", landed["before"], landed["after"]])
        with self.assertRaises(self.m.BoardError):
            self.m.cmd_revert_failed(args, repo)
        sh(["git", "fetch", "origin", "main"], self.b)
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.b).stdout.strip(), landed["after"])
        task = self.fx.data(self.fx.board("bob", "show", result["task"]["id"]))
        self.assertEqual(task["status"], "done")

    def test_expired_claim_cannot_open_review(self):
        claim = self.fx.data(self.fx.board("alice", "take", "--new", "Auth", "--paths", "src/auth/**"))["claim"]
        self.commit(self.a, "src/auth/login.py")
        def expire(b):
            c = b.claim(claim["id"])
            c["expires"] = self.m.iso(self.m.now() - dt.timedelta(seconds=1))
            b.save_claim(c)
        self.edit_board(expire)
        repo = self.m.Repo(self.a)
        with self.assertRaisesRegex(self.m.BoardError, "expired"):
            self.m.submit_review(self.m.build_parser().parse_args(["done", "--pr"]), repo, self.m.Board(repo), claim)

    def test_concurrent_overlapping_claims_have_one_winner(self):
        barrier = threading.Barrier(2, timeout=20)
        original = self.m.Board.commit_and_push
        local = threading.local()

        def commit(board, message):
            if not getattr(local, "waited", False):
                local.waited = True
                barrier.wait()
            return original(board, message)

        def take(root):
            args = self.m.build_parser().parse_args(["take", "--new", "Auth", "--globs", "src/auth/**"])
            try:
                self.m.cmd_take(args, self.m.Repo(root))
                return "claimed"
            except self.m.Conflict:
                return "conflict"

        with patch.object(self.m.Board, "commit_and_push", commit), patch.object(self.m, "out"), concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(take, (self.a, self.b)))
        self.assertEqual(sorted(results), ["claimed", "conflict"])
        self.assertEqual(len(self.fx.data(self.fx.board("alice", "status"))["in_progress"]), 1)

    def test_simultaneous_landings_preserve_both_changes(self):
        for name, path in (("alice", "src/auth/alice.py"), ("bob", "src/billing/bob.py")):
            self.fx.board(name, "take", "--new", name, "--globs", path)
            self.commit(self.fx.clones[name], path)
        barrier = threading.Barrier(2, timeout=20)
        original = self.m.run_gate
        local = threading.local()

        def gate(*args):
            result = original(*args)
            if not getattr(local, "waited", False):
                local.waited = True
                barrier.wait()
            return result

        def land(root):
            args = self.m.build_parser().parse_args(["done"])
            self.m.cmd_done(args, self.m.Repo(root))

        with patch.object(self.m, "run_gate", gate), patch.object(self.m, "out"), concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(land, (self.a, self.b)))
        self.assertEqual(len(self.fx.data(self.fx.board("alice", "status"))["done"]), 2)
        sh(["git", "fetch", "origin", "main"], self.a)
        for path in ("src/auth/alice.py", "src/billing/bob.py"):
            self.assertEqual(sh(["git", "show", "origin/main:" + path], self.a).stdout, "work\n")

    def test_rejected_board_update_cannot_land_main(self):
        self.fx.board("alice", "take", "--new", "Auth", "--globs", "src/auth/**")
        self.commit(self.a, "src/auth/login.py")
        before = sh(["git", "rev-parse", "origin/main"], self.a).stdout
        self.fx.reject_next_push_to("refs/heads/board")
        repo = self.m.Repo(self.a)
        repo.cfg["land_retries"] = 1
        with self.assertRaises(self.m.BoardError):
            self.m.cmd_done(self.m.build_parser().parse_args(["done"]), repo)
        sh(["git", "fetch", "origin", "main"], self.a)
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.a).stdout, before)
        self.assertEqual(len(self.fx.data(self.fx.board("alice", "status"))["my_claims"]), 1)

    def test_stale_visibility_and_explicit_recovery(self):
        claim = self.fx.data(self.fx.board("alice", "take", "--new", "Auth", "--globs", "src/auth/**"))["claim"]
        refused = self.fx.board("bob", "release", "--force", claim["id"], check=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("live claim", self.fx.data(refused)["error"])
        def expire(b):
            c = b.claim(claim["id"])
            c["expires"] = self.m.iso(self.m.now() - dt.timedelta(seconds=1))
            b.save_claim(c)
        self.edit_board(expire)
        self.assertTrue(self.fx.data(self.fx.board("bob", "show", claim["id"]))["stale"])
        self.fx.board("bob", "release", "--force", claim["id"])
        self.assertEqual(self.fx.data(self.fx.board("alice", "show", claim["id"]))["status"], "open")

    def test_claim_expiring_during_gate_cannot_land(self):
        task = self.fx.data(self.fx.board("alice", "take", "--new", "Auth", "--globs", "src/auth/**"))["task"]
        self.commit(self.a, "src/auth/login.py")
        before = sh(["git", "rev-parse", "origin/main"], self.a).stdout
        def gate(*args):
            def expire(b):
                c = b.claim(task["id"])
                c["expires"] = self.m.iso(self.m.now() - dt.timedelta(seconds=1))
                b.save_claim(c)
            self.edit_board(expire)
            return True, []
        with patch.object(self.m, "run_gate", gate), self.assertRaisesRegex(self.m.BoardError, "expired"):
            self.m.cmd_done(self.m.build_parser().parse_args(["done"]), self.m.Repo(self.a))
        sh(["git", "fetch", "origin", "main"], self.a)
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.a).stdout, before)

    def test_missing_gate_fails_and_preserves_claim(self):
        self.fx.board("alice", "take", "--new", "Gate", "--globs", ".harness/gate/**")
        sh(["git", "rm", ".harness/gate/code.sh"], self.a)
        sh(["git", "commit", "-m", "Remove gate"], self.a)
        p = self.fx.board("alice", "done", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Missing gate", self.fx.data(p)["error"])

    def test_malformed_board_fails_closed_and_identifier_cannot_escape(self):
        def corrupt(b):
            self.m.write_json(b.path("claims", "bad.json"), {"id": "bad"})
        board = self.m.Board(self.m.Repo(self.b))
        board.sync()
        corrupt(board)
        board.commit_and_push("test corrupt state")
        p = self.fx.board("alice", "take", "--new", "Auth", "--globs", "src/auth/**", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Malformed claims", self.fx.data(p)["error"])
        with self.assertRaises(self.m.BoardError):
            board.task("../narrator")

    def test_narrator_does_not_skip_same_second_events(self):
        fixed = self.m.now()
        def note(text):
            board = self.m.Board(self.m.Repo(self.a))
            with patch.object(self.m, "now", return_value=fixed):
                board.txn("test note", lambda b: b.note("alice", "finding", text))
        note("first")
        self.fx.board("alice", "narrate")
        note("second")
        result = self.fx.data(self.fx.board("alice", "narrate"))
        self.assertEqual(result["posted"], 1)
        self.assertIn("second", result["lines"][0])

    def test_doctor_missing_gh_and_auth_in_pr_mode(self):
        from agentlane import inspect
        repo = inspect.core.Repo(self.a)
        repo.cfg["landing_mode"] = "pr"
        real_which = inspect.shutil.which
        def which(name):
            return None if name == "gh" else real_which(name)
        with patch.object(inspect.core, "Repo", return_value=repo), patch.object(inspect.shutil, "which", side_effect=which), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(inspect.doctor(argparse.Namespace(json=True)), 1)
        self.assertFalse(next(c for c in json.loads(output.getvalue())["checks"] if c["name"] == "github_cli")["ok"])
        real_run = inspect.core.run
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "not authenticated") if cmd[0] == "gh" else real_run(cmd, **kwargs)
        with patch.object(inspect.core, "Repo", return_value=repo), patch.object(inspect.shutil, "which", side_effect=lambda name: "gh" if name == "gh" else real_which(name)), patch.object(inspect.core, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(inspect.doctor(argparse.Namespace(json=True)), 1)
        check = next(c for c in json.loads(output.getvalue())["checks"] if c["name"] == "github_auth")
        self.assertIn("gh auth login", check["message"])
