"""Board behavior against independent real Git repositories."""
import json
import os
from pathlib import Path
import time
import unittest

from tests.support import BOARD, ROOT, Fixture, TestCase, load_board_module, sh


class GlobOverlapTests(TestCase):
    def setUp(self):
        super().setUp()
        self.m = load_board_module()

    def test_directory_vs_file(self):
        self.assertTrue(self.m.globs_overlap("src/auth/**", "src/auth/login.py"))
        self.assertTrue(self.m.globs_overlap("src/auth/login.py", "src/auth/**"))

    def test_nested_directories(self):
        self.assertTrue(self.m.globs_overlap("src/**", "src/auth/**"))
        self.assertTrue(self.m.globs_overlap("src/auth/**", "src/**"))

    def test_siblings_do_not_overlap(self):
        self.assertFalse(self.m.globs_overlap("src/auth/**", "src/billing/**"))
        self.assertFalse(self.m.globs_overlap("docs/pitch/**", "src/**"))
        self.assertFalse(self.m.globs_overlap("src/auth/login.py", "src/auth/logout.py"))

    def test_prefix_is_not_a_directory(self):
        self.assertFalse(self.m.globs_overlap("src/auth/**", "src/authz/**"))

    def test_wildcard_file_pattern(self):
        self.assertTrue(self.m.globs_overlap("src/*.py", "src/main.py"))
        self.assertTrue(self.m.globs_overlap("src/**", "src/*.py"))

    def test_paths_outside(self):
        self.assertEqual(self.m.paths_outside(["src/auth/a.py", "docs/x.md"], ["src/auth/**"]), ["docs/x.md"])


class BoardFlowTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "cursor")

    def test_init_creates_board_branch_with_board_md(self):
        p = sh(["git", "ls-remote", "--heads", self.fx.origin, "board"], self.fx.tmp)
        self.assertIn("refs/heads/board", p.stdout)
        p = sh(["git", "show", "origin/board:BOARD.md"], self.a)
        self.assertIn("# AgentLane Board", p.stdout)

    def test_overlapping_claim_is_refused_and_disjoint_claim_allowed(self):
        p = self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.assertEqual(self.fx.data(p)["claim"]["owner"], "alice")
        p = self.fx.board("bob", "take", "--new", "Login tests", "--globs", "src/auth/test_login.py", check=False)
        self.assertEqual(p.returncode, 1)
        err = self.fx.data(p)
        self.assertTrue(err["conflict"])
        self.assertIn("alice", err["error"])
        p = self.fx.board("bob", "take", "--new", "Billing", "--globs", "src/billing/**")
        self.assertTrue(self.fx.data(p)["claim"]["branch"].startswith("claim/"))
        st = self.fx.data(self.fx.board("bob", "status"))
        self.assertEqual(len(st["in_progress"]), 2)
        self.assertEqual(len(st["open"]), 0, "a refused take must not leave a task behind")

    def test_second_claim_requires_stacking_on_own_branch(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        p = self.fx.board("alice", "take", "--new", "Billing", "--globs", "src/billing/**", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("stack", self.fx.data(p)["error"])
        st = self.fx.data(self.fx.board("alice", "status"))
        branch = st["my_claims"][0]["branch"]
        p = self.fx.board("alice", "take", "--new", "Billing", "--globs", "src/billing/**", "--base", branch)
        self.assertEqual(self.fx.data(p)["claim"]["base"], branch)
        p = self.fx.board("bob", "take", "--new", "Other", "--globs", "src/other/**", "--base", branch, check=False)
        self.assertEqual(p.returncode, 1)

    def test_hot_path_needs_hot_claim(self):
        p = self.fx.board("alice", "take", "--new", "Deps", "--globs", "package.json", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("hot", self.fx.data(p)["error"])
        p = self.fx.board("alice", "take", "--new", "Deps", "--globs", "package.json", "--hot")
        self.assertEqual(self.fx.data(p)["claim"]["ttl_minutes"], 10)

    def test_expire_releases_timed_out_claim(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**", "--ttl", "0")
        res = self.fx.data(self.fx.board("bob", "expire"))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["owner"], "alice")
        st = self.fx.data(self.fx.board("bob", "status"))
        self.assertEqual(len(st["open"]), 1)
        self.assertEqual(st["in_progress"], [])

    def test_extend_is_capped(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.fx.board("alice", "extend")
        self.fx.board("alice", "extend")
        p = self.fx.board("alice", "extend", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no extensions left", self.fx.data(p)["error"])

    def test_claim_survives_a_lost_push_race(self):
        marker = self.fx.reject_next_push_to("refs/heads/board")
        p = self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.assertEqual(self.fx.data(p)["claim"]["owner"], "alice")
        self.assertTrue(os.path.exists(marker), "the simulated rejection never fired")

    def test_notes_are_shared_and_per_member_files(self):
        self.fx.board("alice", "note", "auth API returns 401 on empty token", "--kind", "finding")
        self.fx.board("bob", "note", "use sqlite for the demo", "--kind", "decision")
        st = self.fx.data(self.fx.board("alice", "status"))
        kinds = sorted(n["kind"] for n in st["recent_notes"])
        self.assertEqual(kinds, ["decision", "finding"])
        p = sh(["git", "ls-tree", "--name-only", "origin/board", "notes/"], self.a)
        self.assertIn("notes/alice.jsonl", p.stdout)
        self.assertIn("notes/bob.jsonl", p.stdout)

    def test_stuck_and_handoff(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.fx.board("alice", "stuck", "OAuth redirect loops")
        st = self.fx.data(self.fx.board("bob", "status"))
        self.assertEqual(st["stuck"][0]["blocker"], "OAuth redirect loops")
        self.fx.board("alice", "handoff", "bob")
        st = self.fx.data(self.fx.board("bob", "status"))
        self.assertEqual(st["my_claims"][0]["owner"], "bob")
        self.assertEqual(st["stuck"], [])


class LandingTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "codex")
        self.fx.board("alice", "install", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "install", "--name", "bob", "--agent", "codex")

    def commit(self, clone, rel, content, msg="work"):
        path = os.path.join(clone, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        sh(["git", "add", "-A"], clone)
        sh(["git", "commit", "-q", "-m", msg], clone)

    def test_done_lands_on_main_and_releases_claim(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/auth/login.py", "def login():\n    return 'ok'\n")
        res = self.fx.data(self.fx.board("alice", "done"))
        self.assertEqual(res["task"]["status"], "done")
        self.assertEqual(res["landed"]["tier"], "code")
        sh(["git", "fetch", "-q", "origin"], self.b)
        p = sh(["git", "show", "origin/main:src/auth/login.py"], self.b)
        self.assertIn("'ok'", p.stdout)
        st = self.fx.data(self.fx.board("bob", "status"))
        self.assertEqual(st["in_progress"], [])
        self.assertEqual(len(st["done"]), 1)

    def test_done_refuses_paths_outside_claim(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/billing/x.py", "x = 1\n")
        p = self.fx.board("alice", "done", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("outside your claim", self.fx.data(p)["error"])
        st = self.fx.data(self.fx.board("alice", "status"))
        self.assertEqual(len(st["my_claims"]), 1, "a refused landing keeps the claim")

    def test_done_retries_when_someone_lands_first(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/auth/login.py", "def login():\n    return 'alice'\n")
        marker = self.fx.reject_next_push_to("refs/heads/main")
        res = self.fx.data(self.fx.board("alice", "done"))
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(res["task"]["status"], "done")
        self.assertGreaterEqual(len(res["log"]), 2)

    def test_docs_only_change_uses_docs_gate(self):
        self.fx.board("bob", "take", "--new", "Pitch outline", "--globs", "docs/**")
        self.commit(self.b, "docs/pitch.md", "# pitch\n")
        res = self.fx.data(self.fx.board("bob", "done"))
        self.assertEqual(res["landed"]["tier"], "docs")

    def test_docs_gate_catches_merge_markers(self):
        self.fx.board("bob", "take", "--new", "Pitch outline", "--globs", "docs/**")
        self.commit(self.b, "docs/pitch.md", "# pitch\n<<<<<<< HEAD\nx\n")
        p = self.fx.board("bob", "done", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("gate failed", self.fx.data(p)["error"])

    def test_pre_push_hook_blocks_direct_push_to_main(self):
        self.commit(self.a, "README.md", "# changed\n")
        p = sh(["git", "push", "-q", "origin", "HEAD:main"], self.a, check=False, env={"BOARD_MEMBER": "alice"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("agentlane done", p.stderr)

    def test_pre_push_hook_blocks_paths_outside_claim_on_claim_branch(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/billing/x.py", "x = 1\n")
        p = sh(["git", "push", "-q", "-u", "origin", "HEAD"], self.a, check=False, env={"BOARD_MEMBER": "alice"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("outside your claim", p.stderr)

    def test_revert_failed_reopens_task(self):
        claim = self.fx.data(self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**"))["claim"]
        self.commit(self.a, "src/auth/login.py", "def login():\n    return 'broken'\n")
        landed = self.fx.data(self.fx.board("alice", "done"))["landed"]
        res = self.fx.data(self.fx.board("bob", "revert-failed", landed["before"], landed["after"], "--reason", "smoke red"))
        self.assertEqual(res["task"]["status"], "open")
        self.assertEqual(res["task"]["failure"], "smoke red")
        sh(["git", "fetch", "-q", "origin"], self.b)
        p = sh(["git", "show", "origin/main:src/auth/login.py"], self.b)
        self.assertNotIn("broken", p.stdout)
        self.fx.board("alice", "take", claim["id"])
        recovered = sh(["git", "show", "HEAD:src/auth/login.py"], self.a)
        self.assertIn("broken", recovered.stdout, "The original branch must survive landing for repair after rollback")


if __name__ == "__main__":
    unittest.main()


class RegressionTests(TestCase):
    """Defects found by tools/simulate on 2026-09-15."""

    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "codex")

    def commit(self, clone, rel, content):
        path = os.path.join(clone, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        sh(["git", "add", "-A"], clone)
        sh(["git", "commit", "-q", "-m", "work"], clone)

    def board_commit_count(self):
        return int(sh(["git", "rev-list", "--count", "origin/board"], self.a).stdout.strip())

    def test_heartbeats_inside_the_window_do_not_write_to_the_board(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        sh(["git", "fetch", "-q", "origin"], self.a)
        before = self.board_commit_count()
        first = self.fx.data(self.fx.board("alice", "heartbeat"))
        second = self.fx.data(self.fx.board("alice", "heartbeat"))
        sh(["git", "fetch", "-q", "origin"], self.a)
        self.assertEqual(first["written"], 0, "a claim taken seconds ago is already fresh")
        self.assertEqual(second["written"], 0)
        self.assertEqual(self.board_commit_count(), before)
        forced = self.fx.data(self.fx.board("alice", "heartbeat", "--force"))
        self.assertEqual(forced["written"], 1)

    def test_heartbeat_window_is_clamped_below_the_stall_window(self):
        cfg_path = os.path.join(self.a, ".harness", "config.json")
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        cfg["stall_release_minutes"] = 0.05
        Path(cfg_path).write_text(json.dumps(cfg), encoding="utf-8")
        sh(["git", "add", "-A"], self.a)
        sh(["git", "commit", "-q", "-m", "short stall window"], self.a)
        sh(["git", "push", "-q", "origin", "main"], self.a)
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        time.sleep(1.2)
        res = self.fx.data(self.fx.board("alice", "heartbeat"))
        self.assertEqual(res["written"], 1, "with a 3 second stall window a heartbeat after 1.2s must be written")

    def test_done_explains_an_expired_claim_and_how_to_recover(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**", "--ttl", "0")
        self.commit(self.a, "src/auth/login.py", "def login():\n    return 'late'\n")
        self.fx.board("bob", "expire")
        p = self.fx.board("alice", "done", check=False)
        self.assertEqual(p.returncode, 1)
        err = self.fx.data(p)["error"]
        self.assertIn("expired", err)
        self.assertIn("agentlane take", err)

    def test_revert_is_skipped_when_main_was_already_red_at_base(self):
        self.fx.board("alice", "take", "--new", "Gate", "--globs", ".harness/gate/**")
        gate = os.path.join(self.a, ".harness", "gate", "code.sh")
        with open(gate, "w") as f:
            f.write("#!/usr/bin/env bash\ncd \"$(git rev-parse --show-toplevel)\"\n"
                    "if [ \"${SIM_CI:-}\" = 1 ] && grep -rq BROKEN src; then exit 1; fi\nexit 0\n")
        sh(["git", "add", "-A"], self.a)
        sh(["git", "commit", "-q", "-m", "sim gate"], self.a)
        self.fx.board("alice", "done")
        self.fx.board("alice", "take", "--new", "Bad", "--globs", "src/auth/**")
        self.commit(self.a, "src/auth/login.py", "BROKEN\n")
        bad = self.fx.data(self.fx.board("alice", "done"))["landed"]
        sh(["git", "fetch", "-q", "origin"], self.b)
        sh(["git", "checkout", "-q", "-B", "main", "origin/main"], self.b)
        self.fx.board("bob", "take", "--new", "Good", "--globs", "src/billing/**")
        self.commit(self.b, "src/billing/x.py", "x = 1\n")
        good = self.fx.data(self.fx.board("bob", "done"))["landed"]
        res = self.fx.data(self.fx.board("bob", "revert-failed", good["before"], good["after"], "--check-base",
                                         extra_env={"SIM_CI": "1"}))
        self.assertEqual(res.get("skipped"), "base already red")
        sh(["git", "fetch", "-q", "origin"], self.b)
        self.assertIn("x = 1", sh(["git", "show", "origin/main:src/billing/x.py"], self.b).stdout)
        res = self.fx.data(self.fx.board("bob", "revert-failed", bad["before"], bad["after"], "--check-base",
                                         extra_env={"SIM_CI": "1"}))
        self.assertEqual(res["task"]["status"], "open")
        sh(["git", "fetch", "-q", "origin"], self.b)
        self.assertNotIn("BROKEN", sh(["git", "show", "origin/main:src/auth/login.py"], self.b).stdout)
