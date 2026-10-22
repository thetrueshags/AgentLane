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
                if "--body-file" in cmd:
                    self.pr_bodies.append(Path(cmd[cmd.index("--body-file") + 1]).read_text())
                if isinstance(gh_result, Exception):
                    raise gh_result
                result = gh_result(cmd) if callable(gh_result) else gh_result
                return subprocess.CompletedProcess(cmd, *(result or (0, "https://github.com/team/project/pull/1\n", "")))
            return real_run(cmd, **kwargs)

        self.gh_calls = []
        self.pr_bodies = []
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
                "description": "Document rollback", "external_ref": "TEAM-1", "globs": ["docs/runbooks/**"]}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "board_sync_reviews", "arguments": {}}},
        ])
        self.assertTrue(all(not r["result"]["isError"] for r in replies), replies)
        task = self.status()["open"][0]
        self.assertEqual(task["description"], "Document rollback")
        self.assertEqual(task["external_ref"], "TEAM-1")
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
        self.assertIn("--fill", self.gh_calls[0])

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

    def test_external_ref_roundtrip_and_pr_evidence(self):
        task = self.fx.data(self.fx.board("alice", "add", "Report", "--ref", "TEAM-123", "--globs", "docs/**"))
        taken = self.fx.data(self.fx.board("alice", "take", task["id"]))
        self.assertIn("team-123", taken["claim"]["branch"])
        Path(self.a, "docs/report.md").write_text("# Report\n")
        sh(["git", "add", "docs/report.md"], self.a)
        sh(["git", "commit", "-qm", "Report"], self.a)
        self.assertEqual(self.status()["my_claims"][0]["external_ref"], "TEAM-123")
        for command in ("show", "list", "status"):
            argv = [command, task["id"]] if command == "show" else [command]
            self.assertIn("TEAM-123", self.fx.board("alice", *argv).stdout)
        board = self.m.Board(self.m.Repo(self.a))
        board.sync()
        self.assertIn("TEAM-123", Path(board.dir, "BOARD.md").read_text())
        result = self.invoke("done", "--pr")
        self.assertEqual(result["external_ref"], "TEAM-123")
        self.assertIn("TEAM-123", self.pr_bodies[0])
        self.assertEqual(result["submissions"][0]["results"][0]["exit"], 0)

    def test_target_pr_policy_cannot_be_disabled_by_candidate(self):
        config = Path(self.a, ".harness/config.json")
        cfg = json.loads(config.read_text())
        cfg.update(landing_mode="pr", pr_draft=False)
        config.write_text(json.dumps(cfg))
        sh(["git", "add", ".harness/config.json"], self.a)
        sh(["git", "commit", "-qm", "PR policy"], self.a)
        sh(["git", "push", "-q", "origin", "main"], self.a)
        self.fx.board("alice", "take", "--new", "Policy", "--globs", ".harness/config.json")
        cfg.update(landing_mode="direct", pr_draft=True)
        config.write_text(json.dumps(cfg))
        sh(["git", "add", ".harness/config.json"], self.a)
        sh(["git", "commit", "-qm", "Candidate config"], self.a)
        before = sh(["git", "ls-remote", "origin", "refs/heads/main"], self.a).stdout
        result = self.invoke("done")
        self.assertEqual(result["status"], "review")
        self.assertNotIn("--draft", self.gh_calls[0])
        self.assertEqual(sh(["git", "ls-remote", "origin", "refs/heads/main"], self.a).stdout, before)

    def test_ref_schema_rejects_nonstring(self):
        with self.assertRaisesRegex(self.m.BoardError, "external_ref"):
            self.m.validate_record("tasks", {"id": "AL-1", "title": "Work", "status": "open", "external_ref": 123})

    def test_recovery_handles_host_merge_commit(self):
        self.prepare_change()
        branch = sh(["git", "branch", "--show-current"], self.a).stdout.strip()
        self.invoke("done", "--pr")
        seed = str(Path(self.fx.tmp, "seed"))
        before = sh(["git", "rev-parse", "HEAD"], seed).stdout.strip()
        sh(["git", "fetch", "-q", "origin", branch], seed)
        sh(["git", "merge", "--no-ff", "-qm", "Host merge", "FETCH_HEAD"], seed)
        after = sh(["git", "rev-parse", "HEAD"], seed).stdout.strip()
        sh(["git", "push", "-q", "origin", "main"], seed)
        response = (0, json.dumps({"state": "MERGED", "baseRefName": "main", "mergeCommit": {"oid": after}}), "")
        self.invoke("sync-reviews", gh_result=response)
        result = self.invoke("revert-failed", before, after)
        self.assertEqual(result["task"]["status"], "done")
        self.assertNotEqual(sh(["git", "cat-file", "-e", "HEAD:docs/report.md"], self.a, check=False).returncode, 0)
        self.assertEqual(sh(["git", "rev-parse", "origin/main"], self.a).stdout.strip(), after)

    def test_recovery_opens_pr_and_reopens_only_after_merge(self):
        self.prepare_change()
        landed = self.invoke("done")["landed"]
        before = sh(["git", "ls-remote", "origin", "refs/heads/main"], self.a).stdout
        result = self.invoke("revert-failed", landed["before"], landed["after"], "--reason", "gate failed")
        self.assertEqual(result["task"]["status"], "done")
        self.assertIsNone(result["reverted"])
        self.assertEqual(sh(["git", "ls-remote", "origin", "refs/heads/main"], self.a).stdout, before)
        sha = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        response = (0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": sha}, "baseRefName": "main"}), "")
        with self.assertRaisesRegex(self.m.BoardError, "not on target main"):
            self.invoke("sync-reviews", gh_result=response)
        sh(["git", "push", "-q", "origin", "HEAD:main"], self.a)
        changed = self.invoke("sync-reviews", gh_result=response)["updated"][0]
        self.assertEqual(changed["status"], "open")
        self.assertEqual(changed["recoveries"][0]["merge_commit"], sha)

    def test_recovery_before_original_pr_sync_reopens_task_after_both_merge(self):
        taken = self.prepare_change()
        before = sh(["git", "rev-parse", "origin/main"], self.a).stdout.strip()
        original = self.invoke("done", "--pr")
        host = str(Path(self.fx.tmp, "seed"))
        sh(["git", "fetch", "-q", "origin", taken["claim"]["branch"]], host)
        sh(["git", "merge", "--no-ff", "-qm", "Host merge", "FETCH_HEAD"], host)
        after = sh(["git", "rev-parse", "HEAD"], host).stdout.strip()
        sh(["git", "push", "-q", "origin", "main"], host)
        recovery_url = "https://github.com/team/project/pull/2"
        outcomes = {original["pr"]: after}

        def github(command):
            if command[1:3] == ["pr", "create"]:
                return 0, recovery_url + "\n", ""
            return 0, json.dumps({"state": "MERGED", "baseRefName": "main",
                                  "mergeCommit": {"oid": outcomes[command[3]]}}), ""

        proposal = self.invoke("revert-failed", before, after, gh_result=github)
        self.assertEqual(proposal["task"]["id"], taken["task"]["id"])
        self.assertEqual(proposal["task"]["status"], "review")
        recovery_sha = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        sh(["git", "fetch", "-q", "origin", proposal["recovery"]["branch"]], host)
        sh(["git", "merge", "--ff-only", "-q", "FETCH_HEAD"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        outcomes[recovery_url] = recovery_sha
        self.invoke("sync-reviews", gh_result=github)
        task = self.fx.data(self.fx.board("alice", "show", taken["task"]["id"]))
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["recoveries"][0]["merge_commit"], recovery_sha)
        self.assertEqual(task["landed"][0]["after"], after)
        self.assertEqual(self.invoke("sync-reviews", gh_result=github)["updated"], [])

    def test_recovery_preserves_changes_before_and_after_second_parent_landing(self):
        self.prepare_change()
        host = str(Path(self.fx.tmp, "seed"))
        Path(host, "docs/healthy.md").write_text("Healthy main change\n")
        sh(["git", "add", "docs/healthy.md"], host)
        sh(["git", "commit", "-qm", "Healthy main change"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        sh(["git", "fetch", "-q", "origin", "main"], self.a)
        sh(["git", "merge", "--no-ff", "-qm", "Merge main into candidate", "origin/main"], self.a)
        landed = self.invoke("done")["landed"]
        sh(["git", "pull", "--ff-only", "-q"], host)
        Path(host, "docs/later.md").write_text("Later main change\n")
        sh(["git", "add", "docs/later.md"], host)
        sh(["git", "commit", "-qm", "Later main change"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        self.invoke("revert-failed", landed["before"], landed["after"])
        self.assertEqual(Path(self.a, "docs/healthy.md").read_text(), "Healthy main change\n")
        self.assertEqual(Path(self.a, "docs/later.md").read_text(), "Later main change\n")
        self.assertFalse(Path(self.a, "docs/report.md").exists())
        difference = sh(["git", "diff", "--name-only", landed["before"], "HEAD"], self.a).stdout.splitlines()
        self.assertEqual(difference, ["docs/later.md"])

    def test_recovery_conflict_keeps_remote_main_and_board_unchanged(self):
        taken = self.prepare_change()
        landed = self.invoke("done")["landed"]
        host = str(Path(self.fx.tmp, "seed"))
        sh(["git", "pull", "--ff-only", "-q"], host)
        Path(host, "docs/report.md").write_text("# Later edits to preserve\n")
        sh(["git", "add", "docs/report.md"], host)
        sh(["git", "commit", "-qm", "Later report edits"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        refs = sh(["git", "ls-remote", "--heads", "origin"], self.a).stdout
        with self.assertRaisesRegex(self.m.BoardError, "Recovery conflicts"):
            self.invoke("revert-failed", landed["before"], landed["after"])
        self.assertEqual(sh(["git", "ls-remote", "--heads", "origin"], self.a).stdout, refs)
        self.assertEqual(self.gh_calls, [])
        task = self.fx.data(self.fx.board("alice", "show", taken["task"]["id"]))
        self.assertEqual(task["status"], "done")
        self.assertNotIn("recoveries", task)

    def test_recovery_restores_binary_endpoint_changes(self):
        self.prepare_change()
        payload = bytes(range(256))
        Path(self.a, "docs/state.bin").write_bytes(payload)
        sh(["git", "add", "docs/state.bin"], self.a)
        sh(["git", "commit", "-qm", "Add binary state"], self.a)
        landed = self.invoke("done")["landed"]
        self.invoke("revert-failed", landed["before"], landed["after"])
        self.assertFalse(Path(self.a, "docs/state.bin").exists())
        self.assertEqual(sh(["git", "diff", "--name-only", landed["before"], "HEAD"], self.a).stdout, "")

    def test_recovery_ignores_display_only_diff_configuration(self):
        self.prepare_change()
        landed = self.invoke("done")["landed"]
        sh(["git", "config", "diff.noprefix", "true"], self.a)
        sh(["git", "config", "diff.external", "missing-display-diff-command"], self.a)
        sh(["git", "config", "color.ui", "always"], self.a)
        self.invoke("revert-failed", landed["before"], landed["after"])
        self.assertFalse(Path(self.a, "docs/report.md").exists())

    def test_recovery_includes_gitlinks_even_when_display_diff_hides_submodules(self):
        host = str(Path(self.fx.tmp, "seed"))
        original = sh(["git", "rev-parse", "HEAD"], host).stdout.strip()
        sh(["git", "update-index", "--add", "--cacheinfo", "160000,%s,docs/submodule" % original], host)
        sh(["git", "commit", "-qm", "Add gitlink"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        self.prepare_change()
        changed = sh(["git", "rev-parse", "HEAD"], self.a).stdout.strip()
        sh(["git", "update-index", "--cacheinfo", "160000,%s,docs/submodule" % changed], self.a)
        sh(["git", "commit", "-qm", "Change gitlink"], self.a)
        landed = self.invoke("done")["landed"]
        sh(["git", "config", "diff.ignoreSubmodules", "all"], self.a)
        sh(["git", "config", "diff.submodule", "log"], self.a)
        self.invoke("revert-failed", landed["before"], landed["after"])
        restored = sh(["git", "rev-parse", "HEAD:docs/submodule"], self.a).stdout.strip()
        self.assertEqual(restored, original)

    def test_recovery_preserves_crlf_bytes_in_files_marked_non_text(self):
        host = str(Path(self.fx.tmp, "seed"))
        attributes = Path(host, ".gitattributes")
        attributes.write_text(attributes.read_text() + "\ndocs/crlf.txt -text\n")
        original = b"old\r\nline\r\n"
        Path(host, "docs/crlf.txt").write_bytes(original)
        sh(["git", "add", ".gitattributes", "docs/crlf.txt"], host)
        sh(["git", "commit", "-qm", "Preserve CRLF bytes"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        self.prepare_change()
        Path(self.a, "docs/crlf.txt").write_bytes(b"new\r\nline\r\n")
        sh(["git", "add", "docs/crlf.txt"], self.a)
        sh(["git", "commit", "-qm", "Change CRLF file"], self.a)
        landed = self.invoke("done")["landed"]
        self.invoke("revert-failed", landed["before"], landed["after"])
        self.assertEqual(Path(self.a, "docs/crlf.txt").read_bytes(), original)

    def test_recovery_preserves_whitespace_despite_apply_configuration(self):
        host = str(Path(self.fx.tmp, "seed"))
        original = b"old  \n"
        Path(host, "docs/spaces.txt").write_bytes(original)
        sh(["git", "add", "docs/spaces.txt"], host)
        sh(["git", "commit", "-qm", "Original whitespace"], host)
        sh(["git", "push", "-q", "origin", "main"], host)
        self.prepare_change()
        Path(self.a, "docs/spaces.txt").write_bytes(b"new\n")
        sh(["git", "add", "docs/spaces.txt"], self.a)
        sh(["git", "commit", "-qm", "Change whitespace file"], self.a)
        landed = self.invoke("done")["landed"]
        sh(["git", "config", "apply.whitespace", "fix"], self.a)
        sh(["git", "config", "apply.ignoreWhitespace", "change"], self.a)
        self.invoke("revert-failed", landed["before"], landed["after"])
        # Git may use native checkout line endings; compare committed bytes.
        restored = subprocess.run(["git", "show", "HEAD:docs/spaces.txt"], cwd=self.a,
                                  capture_output=True, check=True).stdout
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
