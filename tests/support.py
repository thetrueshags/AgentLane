"""Isolated, bounded real-process fixtures shared by the test suite."""
import atexit
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = str(Path(__file__).resolve().parent.parent)
BOARD = os.path.join(ROOT, "bin", "board")
_seed = None


class TestCase(unittest.TestCase):
    """Keep developer Git configuration and agent identity out of each test."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="agentlane-test-env-")
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name, "gitconfig")
        config.write_text("[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n"
                          "[core]\n\tautocrlf = false\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "BOARD_", "AGENTLANE_")) and key != "SLACK_WEBHOOK_URL"}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(config),
                   GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        isolation = patch.dict(os.environ, env, clear=True)
        isolation.start()
        self.addCleanup(isolation.stop)


def load_board_module():
    loader = importlib.machinery.SourceFileLoader("boardmod", os.path.join(ROOT, "agentlane", "board.py"))
    spec = importlib.util.spec_from_loader("boardmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def stop_process_tree(process):
    """Release descendants and inherited pipes when a test command times out."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def sh(cmd, cwd, env=None, check=True, input_text=None, timeout=60):
    full = dict(os.environ)
    full.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"})
    if env:
        full.update(env)
    with subprocess.Popen(cmd, cwd=cwd, env=full, text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=os.name != "nt") as process:
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            stop_process_tree(process)
            stdout, stderr = process.communicate(timeout=10)
            raise AssertionError("Timed out after %ss in %s: %s\nstdout:%s\nstderr:%s" %
                                 (timeout, cwd, cmd, stdout, stderr)) from error
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise AssertionError("failed in %s: %s\nstdout:%s\nstderr:%s" % (cwd, cmd, stdout, stderr))
    return result


def seed_template():
    """Build one pristine Git seed per process; never share mutable refs between tests."""
    global _seed
    if _seed is not None:
        return _seed.name
    temporary = tempfile.TemporaryDirectory(prefix="agentlane-test-seed-")
    try:
        origin = os.path.join(temporary.name, "origin.git")
        seed = os.path.join(temporary.name, "seed")
        sh(["git", "init", "-q", "--bare", "-b", "main", origin], temporary.name)
        sh(["git", "clone", "-q", origin, seed], temporary.name)
        shutil.copytree(os.path.join(ROOT, "agentlane"), os.path.join(seed, "agentlane"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        for rel in (".gitattributes", ".gitignore", ".mcp.json", ".codex/config.toml", "bin/board",
                    ".harness/config.json", ".harness/.gitignore", ".harness/hooks/pre-push",
                    ".harness/gate/code.sh", ".harness/gate/docs.sh"):
            dst = Path(seed, rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(Path(ROOT, rel), dst)
        Path(seed, "src/auth").mkdir(parents=True)
        Path(seed, "docs").mkdir()
        Path(seed, "tests").mkdir()
        Path(seed, "src/auth/health.txt").write_text("healthy\n", encoding="utf-8")
        Path(seed, "tests/test_project.py").write_text(
            "from pathlib import Path\nimport unittest\n\n"
            "class ProjectTests(unittest.TestCase):\n"
            "    def test_health(self):\n"
            "        self.assertEqual(Path('src/auth/health.txt').read_text(), 'healthy\\n')\n",
            encoding="utf-8")
        Path(seed, "src/auth/login.py").write_text("def login():\n    return True\n", encoding="utf-8")
        for rel in ("docs/README.md", "README.md"):
            Path(seed, rel).write_text("# project\n", encoding="utf-8")
        config = Path(seed, ".harness/config.json")
        cfg = json.loads(config.read_text(encoding="utf-8"))
        cfg["hot_paths"] = ["package.json"]
        config.write_text(json.dumps(cfg), encoding="utf-8")
        sh(["git", "add", "-A"], seed)
        sh(["git", "commit", "-q", "-m", "seed"], seed)
        sh(["git", "push", "-q", "origin", "main"], seed)
    except BaseException:
        temporary.cleanup()
        raise
    _seed = temporary
    atexit.register(temporary.cleanup)
    return temporary.name


class Fixture:
    """A private bare origin and seed clone, with independently mutable member clones."""

    def __init__(self):
        template = seed_template()
        self._temporary = tempfile.TemporaryDirectory(prefix="board-test-")
        self.tmp = self._temporary.name
        self.origin = os.path.join(self.tmp, "origin.git")
        self.clones = {}
        try:
            # copy2 copies object contents, not hard links to the cached seed.
            shutil.copytree(template, self.tmp, dirs_exist_ok=True)
            sh(["git", "config", "remote.origin.url", self.origin], os.path.join(self.tmp, "seed"))
        except BaseException:
            self.cleanup()
            raise

    def clone(self, member):
        path = os.path.join(self.tmp, member)
        sh(["git", "clone", "-q", self.origin, path], self.tmp)
        sh(["git", "config", "user.name", member], path)
        sh(["git", "config", "user.email", member + "@t"], path)
        self.clones[member] = path
        return path

    def board(self, member, *args, check=True, input_text=None, extra_env=None):
        env = {"BOARD_MEMBER": member, "BOARD_AGENT": "test-agent"}
        if extra_env:
            env.update(extra_env)
        return sh([sys.executable, BOARD, "--json"] + list(args), self.clones[member],
                  env=env, check=check, input_text=input_text)

    def data(self, p):
        return json.loads(p.stdout)

    def reject_next_push_to(self, ref):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        marker = os.path.join(self.tmp, "rejected-" + ref.replace("/", "_"))
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nwhile read old new ref; do\n"
                    "  if [ \"$ref\" = \"%s\" ] && [ ! -f \"%s\" ]; then touch \"%s\"; echo 'simulated race' >&2; exit 1; fi\n"
                    "done\nexit 0\n" % (ref, marker.replace("\\", "/"), marker.replace("\\", "/")))
        os.chmod(hook, stat.S_IRWXU)
        return marker

    def cleanup(self):
        self._temporary.cleanup()
