"""Copilot setup preservation and restricted MCP calls over real stdio."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from agentlane import mcp
from agentlane.setup import install_agent
from tests.support import BOARD, ROOT, Fixture, TestCase, sh


class CopilotSetupTests(TestCase):
    def test_cloud_generates_restricted_admin_configuration_and_preserves_vscode(self):
        with tempfile.TemporaryDirectory() as root:
            repo = SimpleNamespace(root=root)
            install_agent(repo, "copilot", read_only=True)
            vscode = Path(root, ".vscode/mcp.json").read_bytes()
            instructions = Path(root, ".github/copilot-instructions.md").read_bytes()
            message = install_agent(repo, "copilot-cloud")
            cloud = Path(root, ".harness/integrations/copilot-cloud.json")
            self.assertEqual(json.loads(cloud.read_text()), {"mcpServers": {"board": {
                "type": "stdio", "command": "agentlane", "args": ["mcp", "--read-only"],
                "tools": ["board_status", "board_list", "board_show"]}}})
            self.assertIn("not loaded automatically", message)
            self.assertEqual(Path(root, ".vscode/mcp.json").read_bytes(), vscode)
            self.assertEqual(Path(root, ".github/copilot-instructions.md").read_bytes(), instructions)
            cloud.write_bytes(b'{"mcpServers":{"company":{"command":"approved"}}}\r\n')
            original = cloud.read_bytes()
            message = install_agent(repo, "copilot-cloud")
            self.assertEqual(cloud.read_bytes(), original)
            self.assertIn("read-only mode was not applied", message)

    def test_copilot_and_vscode_create_config_and_instructions(self):
        for agent in ("copilot", "vscode"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as root:
                message = install_agent(SimpleNamespace(root=root), agent, read_only=True)
                config = json.loads(Path(root, ".vscode/mcp.json").read_text())
                self.assertEqual(config["servers"]["board"], {
                    "type": "stdio", "command": "agentlane", "args": ["mcp", "--read-only"],
                    "cwd": "${workspaceFolder}"})
                instructions = Path(root, ".github/copilot-instructions.md").read_text()
                self.assertIn("AGENTS.md", instructions)
                self.assertIn("agentlane take", instructions)
                self.assertIn("Created .github/copilot-instructions.md", message)

    def test_preserves_existing_files_and_reports_restriction_not_applied(self):
        with tempfile.TemporaryDirectory() as root:
            repo = SimpleNamespace(root=root)
            config = Path(root, ".vscode/mcp.json")
            config.parent.mkdir()
            original = b'{"servers":{"corporate":{"command":"approved-server"}}}\n'
            config.write_bytes(original)
            message = install_agent(repo, "copilot", read_only=True)
            self.assertEqual(config.read_bytes(), original)
            self.assertIn("read-only mode was not applied", message)
            instructions = Path(root, ".github/copilot-instructions.md")
            instructions.write_bytes(b"Company rules\r\n")
            message = install_agent(repo, "copilot")
            self.assertEqual(instructions.read_bytes(), b"Company rules\r\n")
            self.assertIn("Preserved existing instructions", message)

    def test_restricted_configuration_for_other_clients_and_default_compatibility(self):
        for agent, relative in (("codex", ".codex/config.toml"), ("cursor", ".cursor/mcp.json"),
                                ("opencode", "opencode.json"), ("copilot", ".vscode/mcp.json")):
            for restricted in (False, True):
                with self.subTest(agent=agent, restricted=restricted), tempfile.TemporaryDirectory() as root:
                    install_agent(SimpleNamespace(root=root), agent, read_only=restricted)
                    self.assertEqual("--read-only" in Path(root, relative).read_text(), restricted)


class RestrictedMcpTests(TestCase):
    def test_compatibility_launcher_honors_restriction_and_rejects_unknown_flags(self):
        launcher = str(Path(ROOT, "tools/board-mcp"))
        with tempfile.TemporaryDirectory() as root:
            reply = sh([sys.executable, launcher, "--read-only"], root,
                       input_text='{"id":1,"method":"tools/list"}\n')
            names = {t["name"] for t in json.loads(reply.stdout)["result"]["tools"]}
            self.assertEqual(names, mcp.READ_ONLY_TOOLS)
            result = sh([sys.executable, launcher, "--read-onyl"], root, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_every_unapproved_tool_refused_before_dispatch(self):
        denied = [t["name"] for t in mcp.TOOLS if t["name"] not in mcp.READ_ONLY_TOOLS]
        denied.extend(["board_future_write", None, []])
        with patch.object(mcp, "board", side_effect=AssertionError("must not dispatch")) as dispatch:
            for name in denied:
                with self.subTest(name=name):
                    result, failed = mcp.call(name, {}, read_only=True)
                    self.assertTrue(failed)
                    self.assertIn("read-only", json.loads(result)["error"])
            dispatch.assert_not_called()

    def test_cli_install_and_restricted_protocol_preserve_remote_and_checkout(self):
        fixture = Fixture()
        self.addCleanup(fixture.cleanup)
        root = fixture.clone("copilot-pilot")
        fixture.board("copilot-pilot", "init")
        result = fixture.board("copilot-pilot", "install", "--project", "--agent", "copilot", "--mcp-read-only")
        self.assertIn(".github/copilot-instructions.md", result.stdout)
        self.assertEqual(json.loads(Path(root, ".vscode/mcp.json").read_text())["servers"]["board"]["args"],
                         ["mcp", "--read-only"])
        before = sh(["git", "ls-remote", "origin"], root).stdout
        head = sh(["git", "rev-parse", "HEAD"], root).stdout
        requests = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "board_status"}},
            {"id": 4, "method": "tools/call", "params": {"name": "board_take", "arguments": {
                "title": "Unauthorized", "globs": ["src/**"]}}},
            {"id": 5, "method": "tools/call", "params": {"name": "board_done"}},
            {"id": 6, "method": "tools/call", "params": {"name": "board_list"}},
            {"id": 7, "method": "tools/call", "params": {"name": "board_worker_list"}},
        ]
        process = sh([sys.executable, BOARD, "mcp", "--read-only"], root,
                     env={"BOARD_REPO_ROOT": root, "BOARD_MEMBER": "copilot-pilot"},
                     input_text="\n".join(json.dumps(r) for r in requests) + "\n")
        replies = {r["id"]: r for r in map(json.loads, process.stdout.splitlines())}
        self.assertEqual({t["name"] for t in replies[2]["result"]["tools"]}, {
            "board_doctor", "board_list", "board_show", "board_status", "board_worker_list", "board_worker_show"})
        for identifier in (3, 6, 7):
            self.assertFalse(replies[identifier]["result"]["isError"], replies[identifier])
        for identifier in (4, 5):
            self.assertTrue(replies[identifier]["result"]["isError"])
        self.assertEqual(sh(["git", "ls-remote", "origin"], root).stdout, before)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], root).stdout, head)
        self.assertEqual(json.loads(replies[6]["result"]["content"][0]["text"])["tasks"], [])
