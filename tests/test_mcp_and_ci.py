"""The MCP wrapper over stdio, and the --board-dir mode the workflows use."""
import json
import os
import subprocess
import sys
import unittest

from tests.test_board import BOARD, ROOT, Fixture, sh

MCP = os.path.join(ROOT, "tools", "board-mcp")


def mcp_session(cwd, env, requests):
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    full = dict(os.environ)
    full.update(env)
    full["BOARD_REPO_ROOT"] = cwd
    p = subprocess.run([sys.executable, MCP], cwd=cwd, env=full, input=payload, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise AssertionError("MCP server failed: " + p.stderr)
    return [json.loads(line) for line in p.stdout.split("\n") if line.strip()]


class McpWrapperTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.a = self.fx.clone("alice")
        self.fx.board("alice", "init")
        self.env = {"BOARD_MEMBER": "alice", "BOARD_AGENT": "claude-code"}

    def tearDown(self):
        self.fx.cleanup()

    def test_initialize_list_and_call(self):
        replies = mcp_session(self.a, self.env, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "board_join", "arguments": {"name": "alice", "agent": "claude-code"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "board_take", "arguments": {"title": "Login page", "globs": ["src/auth/**"]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "board_status", "arguments": {}}},
        ])
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "agentlane")
        names = sorted(t["name"] for t in by_id[2]["result"]["tools"])
        self.assertIn("board_take", names)
        self.assertIn("board_done", names)
        self.assertFalse(by_id[3]["result"]["isError"])
        self.assertFalse(by_id[4]["result"]["isError"], by_id[4])
        status = json.loads(by_id[5]["result"]["content"][0]["text"])
        self.assertEqual(status["my_claims"][0]["title"], "Login page")

    def test_conflict_is_reported_as_tool_error_not_crash(self):
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**")
        b = self.fx.clone("bob")
        replies = mcp_session(b, {"BOARD_MEMBER": "bob", "BOARD_AGENT": "cursor"}, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "board_take", "arguments": {"title": "Login tests", "globs": ["src/auth/tests/**"]}}},
        ])
        r = replies[0]["result"]
        self.assertTrue(r["isError"])
        self.assertIn("alice", r["content"][0]["text"])


class BoardDirModeTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.a = self.fx.clone("alice")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "claude-code")
        self.fx.board("alice", "take", "--new", "Login page", "--globs", "src/auth/**", "--ttl", "0")
        self.ci = os.path.join(self.fx.tmp, "ci")
        sh(["git", "clone", "-q", self.fx.origin, self.ci], self.fx.tmp)
        self.data_dir = os.path.join(self.ci, "board-data")
        sh(["git", "clone", "-q", "-b", "board", self.fx.origin, self.data_dir], self.fx.tmp)

    def tearDown(self):
        self.fx.cleanup()

    def test_expire_narrate_render_against_a_checkout(self):
        env = {"BOARD_MEMBER": "board"}
        p = sh([sys.executable, BOARD, "--json", "--board-dir", self.data_dir, "expire"], self.ci, env=env)
        self.assertEqual(json.loads(p.stdout)[0]["owner"], "alice")
        p = sh([sys.executable, BOARD, "--json", "--board-dir", self.data_dir, "narrate"], self.ci, env=env)
        lines = json.loads(p.stdout.strip().split("\n")[-1])["lines"]
        self.assertTrue(any(l.startswith("alice joined") for l in lines), lines)
        self.assertTrue(any(l.startswith("Claim expired") for l in lines), lines)
        p = sh([sys.executable, BOARD, "--json", "--board-dir", self.data_dir, "narrate"], self.ci, env=env)
        self.assertEqual(json.loads(p.stdout.strip().split("\n")[-1])["posted"], 0, "narrator must not repeat")
        sh([sys.executable, BOARD, "--board-dir", self.data_dir, "render"], self.ci, env=env)
        board_md = open(os.path.join(self.data_dir, "BOARD.md")).read()
        self.assertIn("Login page", board_md)
        self.assertIn("## Available", board_md)


if __name__ == "__main__":
    unittest.main()
