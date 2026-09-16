"""Backlog and review workflows against real Git repos; GitHub responses are stubbed."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests.support import TestCase, Fixture, load_board_module, sh
from tests.test_mcp_and_ci import mcp_session


class AgentLaneTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "codex")
        self.m = load_board_module()

    def invoke(self, *argv, gh_result=None):
        args = self.m.build_parser().parse_args(["--json", *argv])
        real_run = self.m.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh":
                self.gh_calls.append(cmd)
                if isinstance(gh_result, Exception):
                    raise gh_result
                return subprocess.CompletedProcess(cmd, *(gh_result or (0, "https://github.com/team/project/pull/1\n", "")))
            return real_run(cmd, **kwargs)

        self.gh_calls = []
        output = io.StringIO()
        with patch.dict(os.environ, {"BOARD_MEMBER": "alice", "BOARD_AGENT": "codex"}), \
                patch.object(self.m, "run", side_effect=fake_run), contextlib.redirect_stdout(output):
            args.fn(args, self.m.Repo(self.a))
        return json.loads(output.getvalue())

    def prepare_change(self, text="# Report\n", path="docs/report.md"):
        task = self.fx.data(self.fx.board("alice", "take", "--new", "Report", "--globs", "docs/**"))
        with Path(self.a, path).open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        sh(["git", "add", path], self.a)
        sh(["git", "commit", "-q", "-m", "Write report"], self.a)
        return task

    def status(self):
        return self.fx.data(self.fx.board("alice", "status"))

    def test_plan_without_paths_or_branch_change_then_take(self):
        before = sh(["git", "rev-parse", "HEAD"], self.a).stdout
        task = self.fx.data(self.fx.board("alice", "add", "Investigate latency", "--kind", "research",
                                         "--description", "Measure p95 and record findings"))
        self.assertEqual(sh(["git", "branch", "--show-current"], self.a).stdout.strip(), "main")
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], self.a).stdout, before)
        self.assertEqual(self.status()["my_claims"], [])
        self.assertEqual(self.status()["open"][0]["description"], "Measure p95 and record findings")
        refused = self.fx.board("alice", "take", task["id"], check=False)
        self.assertNotEqual(refused.returncode, 0)
        taken = self.fx.data(self.fx.board("alice", "take", task["id"], "--globs", "docs/research/**"))
        self.assertEqual(taken["task"]["kind"], "research")

    def test_backlog_can_overlap_live_claim_and_blank_title_is_refused(self):
        self.prepare_change()
        self.fx.board("alice", "add", "Later report", "--globs", "docs/**", "--kind", "design")
        self.assertEqual(len(self.status()["my_claims"]), 1)
        self.assertEqual(len(self.status()["open"]), 1)
        self.assertNotEqual(self.fx.board("alice", "add", "  ", check=False).returncode, 0)

    def test_mcp_backlog_and_review_sync(self):
        replies = mcp_session(self.a, {"BOARD_MEMBER": "alice"}, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "board_add", "arguments": {"title": "Runbook", "kind": "ops",
                "description": "Document rollback", "globs": ["docs/runbooks/**"]}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "board_sync_reviews", "arguments": {}}},
        ])
        self.assertTrue(all(not r["result"]["isError"] for r in replies), replies)
        task = self.status()["open"][0]
        self.assertEqual(task["description"], "Document rollback")
        self.assertEqual(task["globs"], ["docs/runbooks/**"])

    def test_project_review_policy_submits_without_landing(self):
        cfg = Path(self.a, ".harness/config.json")
        data = json.loads(cfg.read_text())
        data["landing_mode"] = "pr"
        cfg.write_text(json.dumps(data))
        # Project policy belongs in committed configuration, before taking work.
        sh(["git", "add", ".harness/config.json"], self.a)
        sh(["git", "commit", "-q", "-m", "Require reviews"], self.a)
        sh(["git", "push", "-q", "origin", "main"], self.a)
        self.prepare_change()
        before = sh(["git", "rev-parse", "origin/main"], self.a).stdout
        task = self.invoke("done")
        self.assertEqual(task["status"], "review")
        self.assertEqual(self.status()["my_claims"], [])
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.a).stdout, before)
        self.assertEqual(len(self.gh_calls), 1)

    def test_pr_creation_failure_keeps_claim(self):
        self.prepare_change()
        for result in ((1, "", "authentication failed"), (0, "", ""), FileNotFoundError("gh missing")):
            with self.subTest(result=result), self.assertRaises(self.m.BoardError):
                self.invoke("done", "--pr", gh_result=result)
            self.assertEqual(len(self.status()["my_claims"]), 1)
            self.assertEqual(self.status()["ready_to_test"], [])

    def test_review_submission_rejects_failed_gate(self):
        self.prepare_change("<<<<<<< HEAD\nbroken\n")
        with self.assertRaisesRegex(self.m.BoardError, "gate failed"):
            self.invoke("done", "--pr")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(len(self.status()["my_claims"]), 1)

    def test_review_submission_rejects_unclaimed_paths(self):
        self.prepare_change(path="README.md")
        with self.assertRaisesRegex(self.m.BoardError, "outside your claim"):
            self.invoke("done", "--pr")
        self.assertEqual(self.gh_calls, [])

    def test_sync_merged_review_verifies_remote_main_and_is_idempotent(self):
        self.prepare_change()
        sha = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        self.invoke("done", "--pr")
        response = (0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": sha}, "baseRefName": "main"}), "")
        with self.assertRaisesRegex(self.m.BoardError, "not on main"):
            self.invoke("sync-reviews", gh_result=response)
        self.assertEqual(len(self.status()["ready_to_test"]), 1)
        # Stand in for a hosting service's successful merge.
        sh(["git", "push", "-q", "origin", "HEAD:main"], self.a)
        result = self.invoke("sync-reviews", gh_result=response)
        self.assertEqual(result["updated"][0]["status"], "done")
        self.assertEqual(result["updated"][0]["landed"][0]["after"], sha)
        self.assertEqual(self.invoke("sync-reviews")["updated"], [])

    def test_sync_open_closed_and_failed_lookup(self):
        self.prepare_change()
        self.invoke("done", "--pr")
        with self.assertRaises(self.m.BoardError):
            self.invoke("sync-reviews", gh_result=(1, "", "network unavailable"))
        self.assertEqual(self.invoke("sync-reviews", gh_result=(0, '{"state":"OPEN"}', ""))["updated"], [])
        result = self.invoke("sync-reviews", gh_result=(0, '{"state":"CLOSED"}', ""))["updated"][0]
        self.assertEqual(result["status"], "open")
        self.assertIsNone(result["owner"])
        self.assertIn("last_branch", result)
        self.assertIn("last_pr", result)

    def test_invalid_review_policy_is_rejected(self):
        cfg = Path(self.a, ".harness/config.json")
        cfg.write_text('{"landing_mode":"typo"}')
        result = self.fx.board("alice", "status", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("landing_mode", self.fx.data(result)["error"])


if __name__ == "__main__":
    unittest.main()
