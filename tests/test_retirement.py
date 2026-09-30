"""Task retirement preserves board history without landing application code."""
import argparse
import copy
import json
from pathlib import Path
from unittest.mock import patch

from agentlane import board as core, retirement, ui
from tests.support import Fixture, TestCase, sh
from tests.test_mcp_and_ci import mcp_session


class RetirementTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        for member in ("alice", "bob"):
            self.fx.board(member, "join", "--name", member, "--agent", "codex")
        self.task = self.call("add", "Obsolete plan", "--description", "Keep acceptance history")

    def call(self, *args, member="alice"):
        return self.fx.data(self.fx.board(member, *args))

    def test_roundtrip_preserves_identity_notes_main_and_audit(self):
        task = self.task["id"]
        replacement = self.call("add", "Replacement")["id"]
        self.call("note", task, "Prior decision")
        before = sh(["git", "ls-remote", self.fx.origin, "refs/heads/main"], self.a).stdout
        branch = sh(["git", "branch", "--show-current"], self.a).stdout
        retired = self.call("retire", task, "--reason", "Duplicate plan", "--superseded-by", replacement)
        self.assertEqual(retired["status"], "retired")
        self.assertNotIn("landed", retired)
        audit = retired["retirement_history"][0]
        self.assertEqual((audit["action"], audit["actor"], audit["reason"], audit["superseded_by"]),
                         ("retire", "alice", "Duplicate plan", replacement))
        self.assertIsNotNone(core.parse_iso(audit["timestamp"]).tzinfo)
        self.call("sync", member="bob")
        shown = self.call("show", task, member="bob")
        self.assertEqual(shown["retirement_history"], retired["retirement_history"])
        self.assertTrue(any(n["text"] == "Prior decision" for n in shown["notes"]))
        self.assertEqual(shown["description"], self.task["description"])
        self.assertNotIn(task, [t["id"] for t in self.call("list", "--available")["tasks"]])
        self.assertNotIn(task, [t["id"] for t in self.call("status")["open"]])
        self.assertIn(task, [t["id"] for t in self.call("list")["tasks"]])
        human = self.fx.board("bob", "show", task).stdout
        self.assertIn("Duplicate plan", human)
        self.assertNotEqual(self.call("add", "Next plan")["id"], task)
        reopened = self.call("unretire", task, "--reason", "Needed again", member="bob")
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["retirement_history"][0], audit)
        self.assertEqual(reopened["retirement_history"][1]["actor"], "bob")
        self.assertEqual(before, sh(["git", "ls-remote", self.fx.origin, "refs/heads/main"], self.a).stdout)
        self.assertEqual(branch, sh(["git", "branch", "--show-current"], self.a).stdout)
        self.call("take", task, "--paths", "src/auth/**")

    def test_refusals_leave_board_unchanged(self):
        task = self.task["id"]
        for argv in [("unretire", task, "--reason", "No"),
                     ("retire", task, "--reason", "  "),
                     ("retire", task, "--reason", "No", "--superseded-by", task),
                     ("retire", task, "--reason", "No", "--superseded-by", "MISSING"),
                     ("retire", task, "--reason", "No", "--superseded-by", "../bad")]:
            with self.subTest(argv=argv):
                before = sh(["git", "ls-remote", self.fx.origin, "refs/heads/board"], self.a).stdout
                self.assertNotEqual(self.fx.board("alice", *argv, check=False).returncode, 0)
                self.assertEqual(before, sh(["git", "ls-remote", self.fx.origin, "refs/heads/board"], self.a).stdout)
        self.call("retire", task, "--reason", "No longer needed")
        self.assertNotEqual(self.fx.board("alice", "retire", task, "--reason", "Again", check=False).returncode, 0)
        self.assertNotEqual(self.fx.board("alice", "take", task, "--paths", "src/auth/**", check=False).returncode, 0)
        self.call("unretire", task, "--reason", "Back")
        self.call("take", task, "--paths", "src/auth/**")
        self.assertNotEqual(self.fx.board("bob", "retire", task, "--reason", "Claimed", check=False).returncode, 0)

    def test_retry_has_one_audit_entry_and_retired_replacement_is_refused(self):
        task = self.task["id"]
        marker = self.fx.reject_next_push_to("refs/heads/board")
        result = self.call("retire", task, "--reason", "Obsolete")
        self.assertTrue(Path(marker).exists())
        self.assertEqual(len(result["retirement_history"]), 1)
        other = self.call("add", "Another")["id"]
        refused = self.fx.board("alice", "retire", other, "--reason", "Duplicate",
                                "--superseded-by", task, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.call("show", other)["status"], "open")
        self.call("unretire", task, "--reason", "Restore")
        again = self.call("retire", task, "--reason", "Obsolete again")
        self.assertEqual([e["action"] for e in again["retirement_history"]],
                         ["retire", "unretire", "retire"])

    def test_sync_refuses_dangling_retirement_reference(self):
        task = self.task["id"]
        replacement = self.call("add", "Replacement")["id"]
        self.call("retire", task, "--reason", "Duplicate", "--superseded-by", replacement)
        board = Path(self.a) / ".git" / "board-wt"
        sh(["git", "rm", "tasks/" + replacement + ".json"], str(board))
        sh(["git", "commit", "-m", "simulate invalid board reference"], str(board))
        sh(["git", "push", "origin", "HEAD:board"], str(board))
        refused = self.fx.board("bob", "sync", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("missing replacement", self.fx.data(refused)["error"])

    def test_mcp_roundtrip(self):
        task = self.task["id"]
        replies = mcp_session(self.a, {"BOARD_MEMBER": "alice", "BOARD_AGENT": "codex"}, [
            {"id": 1, "method": "tools/call", "params": {"name": "board_retire", "arguments": {
                "task_id": task, "reason": "--literal reason"}}},
            {"id": 2, "method": "tools/call", "params": {"name": "board_unretire", "arguments": {
                "task_id": task, "reason": "Restore"}}}])
        for reply in replies:
            self.assertFalse(reply["result"]["isError"], reply)
        self.assertEqual(len(self.call("show", task)["retirement_history"]), 2)


class RetirementValidationTests(TestCase):
    def test_rejects_nonopen_claimed_and_landed_states(self):
        from unittest.mock import Mock
        for status, owner, claim, landed in [
                ("claimed", "alice", {}, None), ("blocked", None, None, None),
                ("review", None, None, None), ("done", None, None, [{}]),
                ("retired", None, None, None), ("open", None, {"stale": True}, None),
                ("open", "alice", None, None), ("open", None, None, [{"after": "old"}])]:
            with self.subTest(status=status, owner=owner, claim=claim, landed=landed):
                board = Mock()
                board.task.return_value = {"id": "AL-1", "status": status, "owner": owner, "landed": landed}
                board.claim.return_value = claim
                board.txn.side_effect = lambda message, mutate: mutate(board)
                repo = Mock(member="alice")
                args = argparse.Namespace(cmd="retire", task="AL-1", reason="Obsolete", board_dir=None)
                with patch.object(core, "Board", return_value=board), self.assertRaises(core.BoardError):
                    retirement.cmd_retirement(args, repo)
                board.save_task.assert_not_called()

    def test_malformed_history_and_state_are_rejected(self):
        entry = {"action": "retire", "actor": "alice", "timestamp": core.iso(core.now()), "reason": "Old"}
        valid = {"id": "AL-1", "title": "Old", "status": "retired", "retirement_history": [entry]}
        core.validate_record("tasks", valid)
        for changes in ({"retirement_history": []}, {"status": "open"}, {"owner": "alice"},
                        {"landed": [{}]}, {"retirement_history": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(core.BoardError):
                core.validate_record("tasks", dict(valid, **changes))
        for changes in ({"actor": "../bad"}, {"reason": " "}, {"timestamp": "yesterday"},
                        {"superseded_by": "AL-1"}, {"action": "unretire"}):
            task = copy.deepcopy(valid)
            task["retirement_history"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(core.BoardError):
                core.validate_record("tasks", task)
