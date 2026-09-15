"""Tests for bin/board against real git repos in a temp dir: a bare origin and two member clones."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOARD = os.path.join(ROOT, "bin", "board")


def load_board_module():
    loader = importlib.machinery.SourceFileLoader("boardmod", BOARD)
    spec = importlib.util.spec_from_loader("boardmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def sh(cmd, cwd, env=None, check=True, input_text=None):
    full = dict(os.environ)
    full.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"})
    if env:
        full.update(env)
    p = subprocess.run(cmd, cwd=cwd, env=full, text=True, input=input_text,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise AssertionError("failed: %s\nstdout:%s\nstderr:%s" % (" ".join(cmd), p.stdout, p.stderr))
    return p


class Fixture:
    """One bare origin with a main branch carrying AgentLane files, plus named member clones."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="board-test-")
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.origin], self.tmp)
        seed = os.path.join(self.tmp, "seed")
        sh(["git", "clone", "-q", self.origin, seed], self.tmp)
        for rel in ("bin/board", ".harness/config.json", ".harness/.gitignore", ".harness/hooks/pre-push",
                    ".harness/gate/code.sh", ".harness/gate/docs.sh"):
            dst = os.path.join(seed, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), dst)
        os.makedirs(os.path.join(seed, "src", "auth"))
        os.makedirs(os.path.join(seed, "docs"))
        open(os.path.join(seed, "src", "auth", "login.py"), "w").write("def login():\n    return True\n")
        open(os.path.join(seed, "docs", "README.md"), "w").write("# project\n")
        open(os.path.join(seed, "README.md"), "w").write("# project\n")
        cfg = json.load(open(os.path.join(seed, ".harness", "config.json")))
        cfg["hot_paths"] = ["package.json"]
        json.dump(cfg, open(os.path.join(seed, ".harness", "config.json"), "w"))
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "seed"], seed)
        sh(["git", "push", "-q", "origin", "main"], seed)
        self.clones = {}

    def clone(self, member):
        path = os.path.join(self.tmp, member)
        sh(["git", "clone", "-q", self.origin, path], self.tmp)
        sh(["git", "config", "user.name", member], path)
        sh(["git", "config", "user.email", member + "@t"], path)
        self.clones[member] = path
        return path

    def board(self, member, *args, check=True, input_text=None, extra_env=None):
        path = self.clones[member]
        env = {"BOARD_MEMBER": member, "BOARD_AGENT": "test-agent"}
        if extra_env:
            env.update(extra_env)
        return sh(["python3", BOARD, "--json"] + list(args), path, env=env, check=check, input_text=input_text)

    def data(self, p):
        return json.loads(p.stdout)

    def reject_next_push_to(self, ref):
        """Installs an origin pre-receive hook that rejects the first push to `ref`, then allows all."""
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        marker = os.path.join(self.tmp, "rejected-" + ref.replace("/", "_"))
        with open(hook, "w") as f:
            f.write("#!/usr/bin/env bash\nwhile read old new ref; do\n"
                    "  if [ \"$ref\" = \"%s\" ] && [ ! -f \"%s\" ]; then touch \"%s\"; echo 'simulated race' >&2; exit 1; fi\n"
                    "done\nexit 0\n" % (ref, marker, marker))
        os.chmod(hook, stat.S_IRWXU)
        return marker

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class GlobOverlapTests(unittest.TestCase):
    def setUp(self):
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


class BoardFlowTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "cursor")

    def tearDown(self):
        self.fx.cleanup()

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


class LandingTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "codex")
        self.fx.board("alice", "install", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "install", "--name", "bob", "--agent", "codex")

    def tearDown(self):
        self.fx.cleanup()

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
        self.assertIn("board done", p.stderr)

    def test_pre_push_hook_blocks_paths_outside_claim_on_claim_branch(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/billing/x.py", "x = 1\n")
        p = sh(["git", "push", "-q", "-u", "origin", "HEAD"], self.a, check=False, env={"BOARD_MEMBER": "alice"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("outside your claim", p.stderr)

    def test_revert_failed_reopens_task(self):
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        self.commit(self.a, "src/auth/login.py", "def login():\n    return 'broken'\n")
        landed = self.fx.data(self.fx.board("alice", "done"))["landed"]
        res = self.fx.data(self.fx.board("bob", "revert-failed", landed["before"], landed["after"], "--reason", "smoke red"))
        self.assertEqual(res["task"]["status"], "open")
        self.assertEqual(res["task"]["failure"], "smoke red")
        sh(["git", "fetch", "-q", "origin"], self.b)
        p = sh(["git", "show", "origin/main:src/auth/login.py"], self.b)
        self.assertNotIn("broken", p.stdout)


if __name__ == "__main__":
    unittest.main()


class RegressionTests(unittest.TestCase):
    """Defects found by tools/simulate on 2026-09-15."""

    def setUp(self):
        self.fx = Fixture()
        self.a = self.fx.clone("alice")
        self.b = self.fx.clone("bob")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("bob", "join", "--name", "bob", "--agent", "codex")

    def tearDown(self):
        self.fx.cleanup()

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
        cfg = json.load(open(cfg_path))
        cfg["stall_release_minutes"] = 0.05
        json.dump(cfg, open(cfg_path, "w"))
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
        self.assertIn("board take", err)

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
