"""Additive installation and gate contracts in fresh projects."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from agentlane import board as core, setup
from tests.support import TestCase, sh


class SetupUpgradeTests(TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="agentlane-project-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        sh(["git", "init", "-q"], str(self.root))

    def install(self, *extra):
        args = core.build_parser().parse_args(["--json", "install", "--project", "--agent", "claude-code", *extra])
        setup.install(args, core.Repo(str(self.root)))

    def gate(self, functions):
        return sh([core.find_bash(), "-c", functions + '\nbash .harness/gate/code.sh'], str(self.root), check=False)

    def test_phoenix_and_ruby_detection_and_override(self):
        self.install("--no-claude-hooks")
        Path(self.root, "mix.exs").touch()
        result = self.gate('mix() { echo "mix:$*"; }; export -f mix')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mix:test", result.stdout)
        Path(self.root, "mix.exs").unlink()
        Path(self.root, "Gemfile").touch()
        result = self.gate('bundle() { echo "bundle:$*"; }; export -f bundle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bundle:exec rspec", result.stdout)
        Path(self.root, "bin").mkdir()
        runner = Path(self.root, "bin/selftest")
        runner.write_text('#!/usr/bin/env bash\necho selftest\n')
        runner.chmod(0o755)
        result = self.gate('bundle() { return 9; }; export -f bundle')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selftest", result.stdout)
        result = self.gate("export AGENTLANE_GATE_COMMAND='echo custom-gate; exit 7'")
        self.assertEqual(result.returncode, 7)
        self.assertIn("custom-gate", result.stdout)
        self.assertNotIn("selftest", result.stdout)

    def test_hook_merge_preserves_settings_and_is_idempotent(self):
        path = Path(self.root, ".claude/settings.json")
        path.parent.mkdir()
        custom = {"hooks": [{"type": "command", "command": "echo custom"}]}
        path.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [custom]}}))
        self.install()
        first = path.read_bytes()
        self.install()
        self.assertEqual(first, path.read_bytes())
        config = json.loads(first)
        self.assertEqual(config["hooks"]["Stop"][0], custom)
        self.assertEqual(config["permissions"], {"allow": ["Read"]})
        self.assertEqual(len(config["hooks"]["Stop"]), 2)
        for name in ("claude-session-start.sh", "claude-stop.sh"):
            self.assertTrue(Path(self.root, ".harness/hooks", name).is_file())
        self.assertIn("agentlane heartbeat", Path(self.root, ".harness/hooks/claude-stop.sh").read_text())

    def test_no_hooks_flag_and_malformed_settings_are_preserved(self):
        self.install("--no-claude-hooks")
        path = Path(self.root, ".claude/settings.json")
        self.assertFalse(path.exists())
        path.parent.mkdir()
        path.write_text('{"hooks": {"Stop": "custom"}}')
        before = path.read_bytes()
        with self.assertRaisesRegex(core.BoardError, "preserved"):
            self.install()
        self.assertEqual(path.read_bytes(), before)

    def test_installed_hooks_keep_lf_and_execute_with_windows_default_newlines(self):
        real_open = Path.open
        hook_names = ("claude-session-start.sh", "claude-stop.sh")

        def windows_open(path, mode="r", *args, **kwargs):
            if mode == "w" and path.name in hook_names and kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", windows_open):
            self.install()
        calls = self.root / "hook-calls.txt"
        for name in hook_names:
            script = Path(self.root, ".harness/hooks", name)
            self.assertNotIn(b"\r\n", script.read_bytes())
            result = sh([core.find_bash(), "-c",
                         'agentlane() { printf "%s\\n" "$*" >> "$HOOK_CALLS"; }; export -f agentlane; bash "$1"',
                         "hooks", ".harness/hooks/" + name], str(self.root),
                        env={"CLAUDE_PROJECT_DIR": self.root.as_posix(), "HOOK_CALLS": calls.as_posix()})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["status --brief", "heartbeat --quiet"])
