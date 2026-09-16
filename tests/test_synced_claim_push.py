"""Claim pushes and exact reviewed landings through installed hooks in real repos."""
import datetime as dt
import json
from pathlib import Path
import unittest

from agentlane import board as core
from tests.test_board import Fixture, sh


class SyncedClaimPushTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.seed = str(Path(self.fx.tmp, "seed"))
        cfg = json.loads(Path(self.seed, ".harness/config.json").read_text())
        cfg.update(require_review=True, warn_lines=10, refuse_lines=20)
        self.commit(self.seed, ".harness/config.json", json.dumps(cfg))
        self.git(self.seed, "push", "-q", "origin", "main")
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        for name in ("alice", "bob"):
            self.fx.board(name, "join", "--name", name, "--agent", "test")
            self.fx.board(name, "install", "--name", name, "--agent", "test")

    def git(self, root, *args):
        return sh(["git"] + list(args), root).stdout.strip()

    def commit(self, root, path, text):
        target = Path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.git(root, "add", path)
        self.git(root, "commit", "-qm", "work")
        return self.git(root, "rev-parse", "HEAD")

    def candidate(self, paths="src/auth/**"):
        result = self.fx.data(self.fx.board("alice", "take", "--new", "Auth", "--globs", paths))
        self.task = result["task"]["id"]
        self.branch = result["claim"]["branch"]
        self.sha = self.commit(self.a, "src/auth/login.py", "claim work\n")
        self.push()

    def push(self, check=True):
        return sh(["git", "push", "-q", "origin", "HEAD:refs/heads/" + self.branch], self.a, check=check)

    def advance_and_merge(self, path="README.md", text="landed work\n"):
        base = self.commit(self.seed, path, text)
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.a, "fetch", "-q", "origin", "main")
        self.git(self.a, "merge", "-q", "--no-edit", "FETCH_HEAD")
        return base, self.git(self.a, "rev-parse", "HEAD")

    def test_pushed_claim_merge_review_and_done_preserve_exact_sha(self):
        self.candidate()
        reviewer = self.fx.clone("reviewer")
        self.fx.board("reviewer", "join", "--name", "reviewer", "--agent", "test")
        self.git(reviewer, "checkout", "-q", "--detach", self.sha)
        base = self.git(self.a, "rev-parse", "origin/main")
        self.assertEqual(Path(reviewer, "src/auth/login.py").read_text(), "claim work\n")
        self.fx.board("reviewer", "approve", self.task, "--commit", self.sha,
                      "--base", base, "--evidence", "Verified auth content in clean checkout")
        # A separate claim lands through the real review and done commands.
        other = self.fx.data(self.fx.board("bob", "take", "--new", "Billing", "--globs", "src/billing/**"))
        billing = self.commit(self.b, "src/billing/provider.py", "provider = True\n")
        base = self.git(self.b, "rev-parse", "origin/main")
        self.git(self.b, "push", "-q", "origin", other["claim"]["branch"])
        self.git(reviewer, "fetch", "-q", "origin", other["claim"]["branch"])
        self.git(reviewer, "checkout", "-q", "--detach", billing)
        self.assertEqual(Path(reviewer, "src/billing/provider.py").read_text(), "provider = True\n")
        self.fx.board("reviewer", "approve", other["task"]["id"], "--commit", billing,
                      "--base", base, "--evidence", "Verified provider content in clean checkout")
        self.fx.board("bob", "done", other["task"]["id"])
        self.git(self.a, "fetch", "-q", "origin", "main")
        self.git(self.a, "merge", "-q", "--no-edit", "FETCH_HEAD")
        merged = self.git(self.a, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD^1"), self.sha)
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD^2"), billing)
        self.push()  # Ordinary fast-forward push of the already-pushed claim.
        stale = self.fx.board("alice", "done", self.task, check=False)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("Independent approval required", stale.stdout)
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD"), merged)
        self.git(reviewer, "fetch", "-q", "origin", self.branch)
        self.assertEqual(self.git(reviewer, "rev-parse", "FETCH_HEAD"), merged)
        self.git(reviewer, "checkout", "-q", "--detach", "FETCH_HEAD")
        self.assertEqual(Path(reviewer, "src/auth/login.py").read_text(), "claim work\n")
        self.assertEqual(Path(reviewer, "src/billing/provider.py").read_text(), "provider = True\n")
        self.assertEqual(self.git(reviewer, "status", "--porcelain"), "")
        approval = self.fx.data(self.fx.board("reviewer", "approve", self.task, "--commit", merged,
                                             "--base", billing, "--evidence", "Verified auth and provider content in clean checkout"))
        landed = self.fx.data(self.fx.board("alice", "done", self.task))["landed"]
        self.assertEqual(landed["after"], merged)
        self.assertEqual(landed["before"], billing)
        self.assertEqual(landed["approval"], approval["id"])
        self.assertEqual(self.git(self.a, "ls-remote", "origin", "refs/heads/main").split()[0], merged)

    def test_done_keeps_current_merge_candidate_when_approval_is_missing(self):
        self.candidate()
        base, merged = self.advance_and_merge()
        result = self.fx.board("alice", "done", self.task, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Independent approval required", result.stdout)
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.git(self.a, "ls-remote", "origin", "refs/heads/main").split()[0], base)

    def refused(self, text):
        previous = self.git(self.a, "ls-remote", "origin", "refs/heads/" + self.branch)
        result = self.push(check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)
        self.assertEqual(self.git(self.a, "ls-remote", "origin", "refs/heads/" + self.branch), previous)

    def test_imported_large_hot_main_change_is_excluded(self):
        self.candidate()
        self.advance_and_merge("package.json", "\n".join(str(i) for i in range(30)) + "\n")
        self.push()

    def test_merged_claim_rejects_outside_edit_and_revert(self):
        self.candidate()
        self.advance_and_merge()
        self.commit(self.a, "README.md", "claimant edited main work\n")
        self.refused("outside your claim")
        self.commit(self.a, "README.md", "# project\n")
        self.refused("outside your claim")

    def test_merge_resolution_cannot_drop_imported_main_work(self):
        self.candidate()
        self.commit(self.seed, "README.md", "landed work\n")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.a, "fetch", "-q", "origin", "main")
        self.git(self.a, "merge", "-q", "--no-commit", "FETCH_HEAD")
        # Simulate a bad merge resolution that restores the old main content.
        self.commit(self.a, "README.md", "# project\n")
        self.refused("outside your claim")

    def test_cumulative_claim_size_cannot_be_split_across_pushes(self):
        self.candidate()
        self.commit(self.a, "src/auth/login.py", "claim work\n" * 9)
        self.push()
        self.commit(self.a, "src/auth/login.py", "claim work\n" * 19)
        self.refused("over 20")

    def test_new_hot_path_in_claim_scope_still_requires_hot_claim(self):
        self.candidate()
        cfg = json.loads(Path(self.seed, ".harness/config.json").read_text())
        cfg["hot_paths"].append("src/auth/secret.py")
        self.advance_and_merge(".harness/config.json", json.dumps(cfg))
        self.push()
        self.commit(self.a, "src/auth/secret.py", "secret = True\n")
        self.refused("outside your claim")

    def test_unmerged_candidate_uses_shared_ancestor_not_target_tree(self):
        self.candidate()
        old_target = self.git(self.a, "rev-parse", "origin/main")
        self.commit(self.seed, "README.md", "concurrent main\n" * 30)
        self.git(self.seed, "push", "-q", "origin", "main")
        self.commit(self.a, "src/auth/login.py", "more claim work\n")
        self.push()
        self.assertNotEqual(self.git(self.a, "rev-parse", "origin/main"), old_target)

    def test_merged_candidate_fetches_main_even_with_stale_tracking_ref(self):
        self.candidate()
        old_target = self.git(self.a, "rev-parse", "origin/main")
        target = self.commit(self.seed, "README.md", "concurrent main\n")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.a, "fetch", "-q", "origin", target)
        self.git(self.a, "merge", "-q", "--no-edit", "FETCH_HEAD")
        self.assertEqual(self.git(self.a, "rev-parse", "origin/main"), old_target)
        self.push()
        self.assertEqual(self.git(self.a, "rev-parse", "origin/main"), target)

    def test_merged_candidate_rejects_expired_and_released_claim(self):
        self.candidate()
        self.advance_and_merge()
        board = core.Board(core.Repo(self.a))
        def expire(b):
            claim = b.claim(self.task)
            claim["expires"] = core.iso(core.now() - dt.timedelta(minutes=1))
            b.save_claim(claim)
        board.txn("test: expire claim", expire)
        self.refused("expired")
        self.fx.board("alice", "release", self.task)
        self.refused("No active claim")

    def test_missing_target_ref_fails_closed(self):
        self.candidate()
        self.commit(self.a, "src/auth/login.py", "more work\n")
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["main_branch"] = "missing-target"
        Path(self.a, ".harness/config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.refused("missing-target")

    def test_unrelated_target_fails_closed(self):
        self.candidate()
        tree = self.git(self.seed, "rev-parse", "HEAD^{tree}")
        unrelated = self.git(self.seed, "commit-tree", tree, "-m", "unrelated root")
        self.git(self.seed, "push", "-q", "origin", unrelated + ":refs/heads/unrelated")
        self.commit(self.a, "src/auth/login.py", "more work\n")
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["main_branch"] = "unrelated"
        Path(self.a, ".harness/config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.refused("Cannot establish a unique claim ancestor")

    def test_ambiguous_main_ancestry_fails_closed(self):
        self.candidate()
        main, merged = self.advance_and_merge()
        self.git(self.seed, "fetch", "-q", "origin", self.branch)
        # Criss-cross merge: neither shared parent is the unique best base.
        other = self.git(self.seed, "commit-tree", "HEAD^{tree}", "-p", main, "-p", self.sha, "-m", "other merge")
        self.git(self.seed, "push", "-q", "origin", other + ":refs/heads/main")
        self.assertEqual(self.git(self.a, "rev-parse", "HEAD"), merged)
        self.refused("Cannot establish a unique claim ancestor")

    def test_direct_main_push_is_still_rejected_after_merge(self):
        self.candidate()
        self.advance_and_merge()
        result = sh(["git", "push", "-q", "origin", "HEAD:main"], self.a, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agentlane done", result.stderr)

    def test_nonclaim_push_keeps_incremental_accounting(self):
        self.git(self.a, "checkout", "-qb", "ordinary")
        self.commit(self.a, "README.md", "ordinary work\n" * 9)
        self.git(self.a, "push", "-q", "origin", "ordinary")
        self.commit(self.a, "README.md", "ordinary work\n" * 20)
        self.git(self.a, "push", "-q", "origin", "ordinary")


if __name__ == "__main__":
    unittest.main()
