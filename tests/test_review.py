"""Independent review against real credential-free Git remotes and separate clones."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentlane import board as core, mcp, review
from tests.test_board import Fixture, sh


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        for name in ("alice", "bob"):
            self.fx.board(name, "join", "--name", name, "--agent", "test")
        self.configure(require_review=True)

    def git(self, root, *args):
        return sh(["git"] + list(args), root).stdout.strip()

    def commit(self, root, path, text="reviewed work\n"):
        Path(root, path).write_text(text, encoding="utf-8")
        self.git(root, "add", path)
        self.git(root, "commit", "-qm", "work")
        return self.git(root, "rev-parse", "HEAD")

    def configure(self, **values):
        seed = str(Path(self.fx.tmp, "seed"))
        cfg = json.loads(Path(seed, ".harness/config.json").read_text())
        cfg.update(values)
        self.commit(seed, ".harness/config.json", json.dumps(cfg))
        self.git(seed, "push", "-q", "origin", "main")
        for root in (self.a, self.b):
            self.git(root, "pull", "-q", "--ff-only")

    def candidate(self, paths="src/auth/**", creator=None):
        if creator:
            task = self.fx.data(self.fx.board(creator, "add", "Auth", "--globs", paths))
            result = self.fx.data(self.fx.board("alice", "take", task["id"]))
        else:
            result = self.fx.data(self.fx.board("alice", "take", "--new", "Auth", "--globs", paths))
        self.task = result["task"]["id"]
        self.branch = result["claim"]["branch"]
        self.base = self.git(self.a, "rev-parse", "HEAD")
        self.sha = self.commit(self.a, "src/auth/login.py", "work for %s\n" % self.task)
        self.git(self.a, "push", "-q", "origin", self.branch)
        self.checkout_reviewer()

    def checkout_reviewer(self, member="bob"):
        root = self.fx.clones[member]
        self.git(root, "fetch", "-q", "origin", self.branch)
        self.git(root, "checkout", "-q", "--detach", "FETCH_HEAD")

    def approve(self, member="bob", check=True, **values):
        return self.fx.board(member, "approve", self.task, "--commit", values.get("commit", self.sha),
                             "--base", values.get("base", self.base), "--evidence", "Ran auth tests: passed", check=check)

    def denied(self, result, text):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(text, self.fx.data(result)["error"])

    def done(self, check=True, *extra):
        return self.fx.board("alice", "done", self.task, *extra, check=check)

    def remote_main(self):
        return self.git(self.a, "ls-remote", "origin", "refs/heads/main").split()[0]

    def edit_task(self, edit):
        def mutate(b):
            task = b.task(self.task)
            edit(task)
            b.save_task(task)
        board = core.Board(core.Repo(self.b))
        board.sync()
        mutate(board)
        # Deliberately permit corrupt fixtures to reach the remote, bypassing render validation.
        self.assertTrue(board.commit_and_push("test task update"))

    def task_state(self):
        b = core.Board(core.Repo(self.b))
        b.sync()
        return b.task(self.task)

    def invoke_done(self):
        core.cmd_done(core.build_parser().parse_args(["done", "--task", self.task]), core.Repo(self.a))

    def test_happy_path_creator_can_review_and_history_survives(self):
        self.candidate(creator="bob")
        approval = self.fx.data(self.approve())
        self.assertEqual(approval["claim"]["owner"], "alice")
        self.assertEqual(approval["reviewer"], "bob")
        landed = self.fx.data(self.done())
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(landed["landed"]["approval"], approval["id"])
        self.assertEqual(landed["task"]["approvals"], [approval])
        self.assertEqual(landed["task"]["implementation"]["owners"], ["alice"])

    def test_no_approval_and_note_is_not_approval(self):
        self.candidate()
        self.fx.board("bob", "note", self.task, "APPROVED: tests passed")
        self.denied(self.done(False), "Independent approval required")
        self.assertEqual(self.remote_main(), self.base)

    def test_candidate_cannot_disable_target_policy(self):
        self.candidate("src/auth/**,.harness/config.json")
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["require_review"] = False
        self.sha = self.commit(self.a, ".harness/config.json", json.dumps(cfg))
        self.denied(self.done(False), "Independent approval required")
        self.assertEqual(self.remote_main(), self.base)

    def test_bootstrap_only_becomes_authoritative_after_landing(self):
        self.configure(require_review=False)
        self.candidate("src/auth/**,.harness/config.json")
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["require_review"] = True
        self.commit(self.a, ".harness/config.json", json.dumps(cfg))
        self.done()
        self.candidate()
        self.denied(self.done(False), "Independent approval required")

    def test_default_off_and_legacy_default_compatibility(self):
        self.configure(require_review=False)
        self.candidate()
        self.edit_task(lambda t: t.pop("implementation"))
        self.fx.board("bob", "show", self.task)
        self.done()
        self.assertEqual(self.remote_main(), self.sha)

    def test_legacy_provenance_remains_unknown_after_retake(self):
        self.candidate()
        self.edit_task(lambda t: t.pop("implementation"))
        self.fx.board("bob", "show", self.task)
        self.fx.board("alice", "release", self.task)
        self.fx.board("alice", "take", self.task)
        self.assertNotIn("implementation", self.task_state())
        self.denied(self.approve(check=False), "provenance")
        self.denied(self.done(False), "provenance")

    def test_self_and_prior_owner_after_handoff_cannot_review(self):
        self.candidate()
        self.denied(self.approve("alice", False), "independent")
        self.fx.board("alice", "handoff", "bob")
        self.denied(self.approve("bob", False), "independent")
        self.denied(self.approve("alice", False), "independent")
        self.assertEqual(self.task_state()["implementation"]["owners"], ["alice", "bob"])

    def test_unregistered_reviewer_cannot_approve(self):
        self.candidate()
        self.fx.clone("eve")
        self.checkout_reviewer("eve")
        self.denied(self.approve("eve", False), "registered")

    def test_review_needs_clean_exact_pushed_candidate_and_base(self):
        self.candidate()
        Path(self.b, "untracked.txt").write_text("dirty")
        self.denied(self.approve(check=False), "clean checkout")
        Path(self.b, "untracked.txt").unlink()
        self.denied(self.approve(check=False, commit=self.sha[:8]), "full lowercase")
        self.denied(self.approve(check=False, commit=self.base), "exact candidate")
        self.denied(self.approve(check=False, base=self.sha), "exact remote main")
        self.sha = self.commit(self.b, "src/auth/login.py", "unpublished\n")
        self.denied(self.approve(check=False), "pushed claim")

    def test_review_rejects_nonancestor_base_and_outside_paths(self):
        self.candidate()
        self.sha = self.commit(self.a, "README.md", "outside\n")
        self.git(self.a, "push", "-q", "origin", self.branch)
        self.checkout_reviewer()
        self.denied(self.approve(check=False), "outside the claim")
        self.advance_main()
        self.base = self.remote_main()
        self.denied(self.approve(check=False), "ancestor")

    def test_amendment_invalidates_approval(self):
        self.candidate()
        self.approve()
        self.git(self.a, "commit", "--amend", "-qm", "amended")
        self.denied(self.done(False), "Independent approval required")
        self.assertEqual(self.remote_main(), self.base)

    def advance_main(self):
        seed = str(Path(self.fx.tmp, "seed"))
        self.commit(seed, "README.md", "concurrent main\n")
        self.git(seed, "push", "-q", "origin", "main")

    def test_base_drift_requires_post_rebase_approval(self):
        self.candidate()
        self.approve()
        self.advance_main()
        new_base = self.remote_main()
        self.denied(self.done(False), "Independent approval required")
        self.assertNotEqual(self.git(self.a, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(self.remote_main(), new_base)

    def test_withdrawal_preserves_history_and_blocks_landing(self):
        self.candidate()
        first = self.fx.data(self.approve())
        self.approve()
        self.denied(self.fx.board("bob", "withdraw", self.task, check=False), "--approval ID")
        self.denied(self.fx.board("alice", "withdraw", self.task, "--approval", first["id"], check=False), "your active")
        self.fx.board("bob", "withdraw", self.task, "--approval", first["id"])
        self.fx.board("bob", "withdraw", self.task)
        self.denied(self.done(False), "Independent approval required")
        self.assertEqual(len(self.task_state()["approvals"]), 2)
        self.assertTrue(all("withdrawal" in a for a in self.task_state()["approvals"]))

    def test_retake_changes_lease_and_preserves_owners(self):
        self.candidate()
        old = self.fx.data(self.approve())
        self.fx.board("alice", "release", self.task)
        retake = self.fx.data(self.fx.board("alice", "take", self.task))
        self.assertNotEqual(old["claim"]["lease"], retake["claim"]["lease"])
        self.assertEqual(retake["task"]["implementation"]["owners"], ["alice"])
        self.denied(self.done(False), "Independent approval required")

    def test_stale_recovery_preserves_prior_owners(self):
        self.candidate()
        def expire(b):
            c = b.claim(self.task)
            c["expires"] = core.iso(core.now() - dt.timedelta(seconds=1))
            b.save_claim(c)
        core.Board(core.Repo(self.b)).txn("expire", expire)
        self.denied(self.approve(check=False), "expired")
        self.fx.board("bob", "release", "--force", self.task)
        self.fx.board("bob", "take", self.task)
        self.denied(self.approve("alice", False), "independent")
        self.assertEqual(self.task_state()["implementation"]["owners"], ["alice", "bob"])

    def test_pr_modes_are_explicitly_incompatible(self):
        self.candidate("src/auth/**,.harness/config.json")
        self.denied(self.done(False, "--pr"), "incompatible with PR mode")
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["landing_mode"] = "pr"
        self.commit(self.a, ".harness/config.json", json.dumps(cfg))
        self.denied(self.done(False), "incompatible with PR mode")

    def test_gate_failure_cannot_be_approved_away(self):
        self.candidate("src/auth/**,.harness/gate/**")
        self.sha = self.commit(self.a, ".harness/gate/code.sh", "#!/bin/sh\nexit 1\n")
        self.git(self.a, "push", "-q", "origin", self.branch)
        self.checkout_reviewer()
        self.approve()
        self.denied(self.done(False), "gate failed")
        self.assertEqual(self.remote_main(), self.base)

    def test_withdrawal_after_final_board_read_defeats_atomic_push(self):
        self.candidate()
        self.approve()
        original = core.push_main_and_board
        count = []
        def race(*args):
            count.append(1)
            if len(count) == 1:
                self.fx.board("bob", "withdraw", self.task)
            return original(*args)
        with patch.object(core, "push_main_and_board", race), self.assertRaisesRegex(core.BoardError, "Independent approval"):
            self.invoke_done()
        self.assertEqual(self.remote_main(), self.base)
        self.assertEqual(count, [1])

    def test_concurrent_ancestor_push_requires_exact_expected_main(self):
        self.candidate()
        middle = self.sha
        self.sha = self.commit(self.a, "src/auth/login.py", "second change\n")
        self.git(self.a, "push", "-q", "origin", self.branch)
        self.checkout_reviewer()
        self.approve()
        original = core.push_main_and_board
        attempts = []
        def race(*args):
            if not attempts:
                self.git(self.b, "push", "-q", "origin", middle + ":refs/heads/main")
            result = original(*args)
            attempts.append(result)
            return result
        with patch.object(core, "push_main_and_board", race), self.assertRaisesRegex(core.BoardError, "Independent approval"):
            self.invoke_done()
        self.assertEqual(attempts, [False], "old-base push must be rejected even though main is an ancestor")
        self.assertEqual(self.remote_main(), middle)

    def test_board_contention_retries_gate_and_approval(self):
        self.candidate()
        self.approve()
        self.fx.reject_next_push_to("refs/heads/board")
        result = self.fx.data(self.done())
        self.assertEqual(len(result["log"]), 2)
        self.assertEqual(self.remote_main(), self.sha)

    def test_rollback_keeps_provenance_and_approval_history(self):
        self.candidate()
        approval = self.fx.data(self.approve())
        self.done()
        self.fx.board("alice", "revert-failed", self.base, self.sha, "--reason", "test recovery")
        task = self.task_state()
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["implementation"]["owners"], ["alice"])
        self.assertEqual(task["approvals"], [approval])
        self.fx.board("bob", "take", self.task)
        self.assertEqual(self.task_state()["implementation"]["owners"], ["alice", "bob"])

    def test_lease_change_after_final_board_read_defeats_atomic_push(self):
        self.candidate()
        self.approve()
        original = core.push_main_and_board
        def race(*args):
            def change(b):
                claim = b.claim(self.task)
                claim["lease"] = "b" * 32
                b.save_claim(claim)
            core.Board(core.Repo(self.b)).txn("lease changed", change)
            return original(*args)
        with patch.object(core, "push_main_and_board", race), self.assertRaisesRegex(core.BoardError, "Claim changed"):
            self.invoke_done()
        self.assertEqual(self.remote_main(), self.base)

    def test_exact_ref_guard_cannot_publish_non_fast_forward(self):
        self.candidate()
        self.advance_main()
        expected = self.remote_main()
        self.git(self.a, "fetch", "-q", "origin", "main")
        board = core.Board(core.Repo(self.a))
        board.sync()
        board.event("alice", "test", "prepare a conflicting publication")
        with self.assertRaisesRegex(core.BoardError, "non-fast-forward"):
            core.push_main_and_board(core.Repo(self.a), board, "cannot rewrite main", expected)
        self.assertEqual(self.remote_main(), expected)

    def test_handoff_during_gate_invalidates_live_owner(self):
        self.candidate()
        self.approve()
        original = core.run_gate
        def gate(*args):
            result = original(*args)
            self.fx.board("alice", "handoff", "bob")
            return result
        with patch.object(core, "run_gate", gate), self.assertRaises(core.BoardError):
            self.invoke_done()
        self.assertEqual(self.remote_main(), self.base)

    def test_malformed_persisted_approval_fails_closed(self):
        self.candidate()
        self.approve()
        self.edit_task(lambda t: t["approvals"][0].update(withdrawal=False))
        self.denied(self.done(False), "Malformed approval withdrawal")
        self.assertEqual(self.remote_main(), self.base)

    def test_landing_rechecks_reviewer_independence_from_record(self):
        self.candidate()
        self.approve()
        self.edit_task(lambda t: t["approvals"][0].update(reviewer="alice"))
        self.denied(self.done(False), "independent")
        self.assertEqual(self.remote_main(), self.base)

    def test_legacy_claim_without_lease_cannot_be_approved(self):
        self.candidate()
        def legacy(b):
            claim = b.claim(self.task)
            claim.pop("lease")
            b.save_claim(claim)
        core.Board(core.Repo(self.b)).txn("legacy lease", legacy)
        self.fx.board("bob", "show", self.task)
        self.denied(self.approve(check=False), "live modern lease")

    def test_malformed_target_policy_cannot_be_hidden_by_candidate(self):
        self.configure(require_review="false")
        # Repair only the candidate checkout so local config validation passes.
        cfg = json.loads(Path(self.a, ".harness/config.json").read_text())
        cfg["require_review"] = False
        self.commit(self.a, ".harness/config.json", json.dumps(cfg))
        with self.assertRaisesRegex(core.BoardError, "require_review must be a boolean"):
            review.fetch_target(core.Repo(self.a))


class ReviewValidationTests(unittest.TestCase):
    def test_new_metadata_is_strictly_validated(self):
        approval = {"id": "approval", "reviewer": "bob", "timestamp": core.iso(core.now()), "commit": "a" * 40,
                    "base": "b" * 40, "evidence": "tested", "claim": {"owner": "alice", "lease": "a" * 32,
                    "branch": "claim/AL-1-task", "globs": ["src/**"], "hot": False}}
        for key, value in (("commit", "abc"), ("base", 1), ("timestamp", "today"), ("evidence", " "),
                           ("reviewer", None), ("withdrawal", False), ("withdrawal", {"reviewer": "eve", "timestamp": core.iso(core.now())}),
                           ("claim", {})):
            record = copy.deepcopy(approval)
            record[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(core.BoardError):
                review.validate_task_review({"approvals": [record]})
        for key, value in (("hot", "false"), ("hot", 0), ("lease", None), ("globs", []), ("branch", "claim/../other")):
            record = copy.deepcopy(approval)
            record["claim"][key] = value
            with self.subTest(key=key), self.assertRaises(core.BoardError):
                review.validate_task_review({"approvals": [record]})
        for provenance in (False, {}, {"version": True, "owners": []}, {"version": 1, "owners": "alice"}):
            with self.subTest(provenance=provenance), self.assertRaises(core.BoardError):
                review.validate_task_review({"implementation": provenance})
        for value in (0, 1, "false", None, [], {}):
            cfg = dict(core.DEFAULTS, require_review=value)
            with self.subTest(value=value), self.assertRaises(core.BoardError):
                core.validate_config(cfg)

    def test_mcp_mappings_and_argument_validation(self):
        with patch.object(mcp, "board", return_value=("ok", False)) as invoke:
            mcp.call("board_approve", {"task_id": "AL-1", "commit": "a" * 40, "base": "b" * 40, "evidence": "tests passed"})
            invoke.assert_called_with(["approve", "AL-1", "--commit", "a" * 40, "--base", "b" * 40, "--evidence", "tests passed"])
            mcp.call("board_withdraw", {"task_id": "AL-1", "approval": "record"})
            invoke.assert_called_with(["withdraw", "AL-1", "--approval", "record"])
            mcp.call("board_withdraw", {"task_id": "AL-1"})
            invoke.assert_called_with(["withdraw", "AL-1"])
            invoke.reset_mock()
            self.assertTrue(mcp.call("board_approve", {"task_id": "AL-1"})[1])
            self.assertTrue(mcp.call("board_withdraw", {"task_id": False})[1])
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
