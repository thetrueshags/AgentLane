"""Exercise isolation, cleanup, timeout diagnostics and complete test reporting."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.runner import shard, test_cases
from tests.support import ROOT, Fixture, TestCase, sh


class HarnessTests(TestCase):
    def test_shards_are_disjoint_complete_and_independent_of_discovery_order(self):
        class Examples(unittest.TestCase):
            def test_a(self):
                pass

            def test_b(self):
                pass

            def test_c(self):
                pass

            def test_d(self):
                pass

        cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(Examples))
        groups = [[case.id() for case in test_cases(shard(unittest.TestSuite(cases), index, 3))]
                  for index in range(3)]
        self.assertEqual(sorted(sum(groups, [])), sorted(case.id() for case in cases))
        self.assertEqual(len(sum(groups, [])), len(set(sum(groups, []))))
        for index, expected in enumerate(groups):
            actual = shard(unittest.TestSuite(reversed(cases)), index, 3)
            self.assertEqual([case.id() for case in test_cases(actual)], expected)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            shard(unittest.TestSuite([cases[0], cases[0]]), 0, 1)

    def test_runner_reports_real_failures_subtests_errors_skips_and_timings(self):
        with tempfile.TemporaryDirectory(prefix="agentlane-runner-probe-") as root:
            Path(root, "probe.py").write_text(
                "import unittest\n"
                "class Probe(unittest.TestCase):\n"
                " def test_pass(self): pass\n"
                " def test_fail(self): self.fail('assertion evidence')\n"
                " def test_error(self): raise RuntimeError('error evidence')\n"
                " def test_skip(self): self.skipTest('platform reason')\n"
                " def test_subtests(self):\n"
                "  with self.subTest(value=1): self.assertEqual(1, 2)\n"
                " @unittest.expectedFailure\n"
                " def test_expected(self): self.fail('known')\n"
                " @unittest.expectedFailure\n"
                " def test_unexpected(self): pass\n"
                "class ClassSkip(unittest.TestCase):\n"
                " @classmethod\n"
                " def setUpClass(cls): raise unittest.SkipTest('class skip reason')\n"
                " def test_never(self): pass\n", encoding="utf-8")
            report = Path(root, "report.jsonl")
            result = sh([sys.executable, str(Path(ROOT, "tools/test")), "probe", "--report", str(report)],
                        root, env={"PYTHONPATH": root}, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            events = [json.loads(line) for line in report.read_text().splitlines()]
            finished = {event["test"].split(".")[-1]: event for event in events if event["event"] == "finish"}
            expected = {"test_pass": "passed", "test_fail": "failed", "test_error": "error",
                        "test_skip": "skipped", "test_subtests": "failed",
                        "test_expected": "expected_failure", "test_unexpected": "unexpected_success"}
            self.assertEqual({name: event["outcome"] for name, event in finished.items()}, expected)
            self.assertTrue(all(event["seconds"] >= 0 for event in finished.values()))
            self.assertEqual(len([event for event in events if event["event"] == "start"]), 7)
            self.assertEqual(events[-1]["event"], "summary")
            self.assertFalse(events[-1]["successful"])
            self.assertIn("assertion evidence", report.read_text())
            self.assertIn("platform reason", report.read_text())
            self.assertIn("Slowest tests", result.stderr)

    def test_report_retains_active_test_when_process_exits_abruptly(self):
        with tempfile.TemporaryDirectory(prefix="agentlane-crash-probe-") as root:
            Path(root, "crash_probe.py").write_text(
                "import os, unittest\nclass Crash(unittest.TestCase):\n"
                " def test_crash(self): os._exit(7)\n", encoding="utf-8")
            report = Path(root, "report.jsonl")
            result = sh([sys.executable, str(Path(ROOT, "tools/test")), "crash_probe", "--report", str(report)],
                        root, env={"PYTHONPATH": root}, check=False)
            self.assertEqual(result.returncode, 7)
            events = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual(events, [{"event": "start", "test": "crash_probe.Crash.test_crash"}])

    def test_runner_rejects_missing_tests_and_invalid_or_empty_shards(self):
        command = [sys.executable, str(Path(ROOT, "tools/test"))]
        for args, code in ((["does_not_exist", "--shard-index", "1", "--shard-count", "3"], 1),
                           (["--shard-count", "0"], 2),
                           (["tests.test_v01.ValidationTests.test_path_normalization_rejects_escape_and_git_metadata",
                             "--shard-index", "1", "--shard-count", "2"], 2)):
            with self.subTest(args=args):
                result = sh(command + args, ROOT, check=False)
                self.assertEqual(result.returncode, code, result.stderr)

    def test_fixture_copies_do_not_share_refs_objects_config_or_working_files(self):
        first, second = Fixture(), Fixture()
        self.addCleanup(first.cleanup)
        self.addCleanup(second.cleanup)
        first_seed, second_seed = Path(first.tmp, "seed"), Path(second.tmp, "seed")
        original = sh(["git", "rev-parse", "HEAD"], second_seed).stdout.strip()
        Path(first_seed, "README.md").write_text("changed\n")
        sh(["git", "add", "README.md"], first_seed)
        sh(["git", "commit", "-qm", "private change"], first_seed)
        sh(["git", "push", "origin", "main"], first_seed)
        self.assertEqual(sh(["git", "rev-parse", "main"], second.origin).stdout.strip(), original)
        self.assertEqual(Path(second_seed, "README.md").read_text(), "# project\n")
        for fixture, seed in ((first, first_seed), (second, second_seed)):
            self.assertEqual(sh(["git", "remote", "get-url", "origin"], seed).stdout.strip(), fixture.origin)
        relative_object = Path("objects", original[:2], original[2:])
        self.assertFalse(os.path.samefile(Path(first.origin, relative_object), Path(second.origin, relative_object)))
        third = Fixture()
        self.addCleanup(third.cleanup)
        self.assertEqual(sh(["git", "rev-parse", "main"], third.origin).stdout.strip(), original)

    def test_fixture_cleanup_removes_read_only_git_objects_even_after_setup_failure(self):
        paths = []

        class BrokenSetup(TestCase):
            def setUp(self):
                super().setUp()
                fixture = Fixture()
                self.addCleanup(fixture.cleanup)
                paths.append(Path(fixture.tmp))
                fixture.clone("alice")
                raise RuntimeError("setup failed")

            def runTest(self):
                pass

        result = unittest.TextTestRunner(stream=io.StringIO()).run(BrokenSetup())
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(paths[0].exists())

    def test_test_environment_ignores_developer_git_routing_config_and_identity(self):
        observed = {}

        class Probe(TestCase):
            def runTest(self):
                observed.update(os.environ)
                self.assertNotIn("GIT_DIR", os.environ)
                self.assertNotIn("BOARD_MEMBER", os.environ)
                self.assertNotIn("AGENTLANE_LOCK_HELD", os.environ)
                self.assertNotIn("SLACK_WEBHOOK_URL", os.environ)
                self.assertEqual(sh(["git", "config", "--global", "maintenance.auto"], ROOT).stdout.strip(), "false")

        with patch.dict(os.environ, {"GIT_DIR": "/missing", "BOARD_MEMBER": "someone-else",
                                     "AGENTLANE_LOCK_HELD": "1", "GIT_CONFIG_GLOBAL": "/missing",
                                     "SLACK_WEBHOOK_URL": "https://example.invalid/test-webhook"}):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(Probe())
            self.assertEqual(os.environ["BOARD_MEMBER"], "someone-else")
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertNotEqual(observed["GIT_CONFIG_GLOBAL"], "/missing")

    def test_command_timeout_keeps_output_and_stops_descendants(self):
        with tempfile.TemporaryDirectory(prefix="agentlane-timeout-probe-") as root:
            pulse = Path(root, "pulse")
            child = ("import time; from pathlib import Path; p=Path(%r); "
                     "print('descendant ready',flush=True); "
                     "exec('while True:\\n p.write_text(str(time.time_ns()))\\n time.sleep(.02)')" % str(pulse))
            parent = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); "
                      "print('stderr evidence',file=sys.stderr,flush=True); time.sleep(60)" % child)
            with self.assertRaises(AssertionError) as error:
                sh([sys.executable, "-c", parent], root, timeout=3)
            self.assertIn("Timed out", str(error.exception))
            self.assertIn("descendant ready", str(error.exception))
            self.assertIn("stderr evidence", str(error.exception))
            before = pulse.read_bytes()
            time.sleep(0.2)
            self.assertEqual(pulse.read_bytes(), before)

    def test_default_code_gate_executes_project_tests_and_failed_tests_block_landing(self):
        fixture = Fixture()
        self.addCleanup(fixture.cleanup)
        clone = fixture.clone("alice")
        fixture.board("alice", "init")
        fixture.board("alice", "join", "--name", "alice", "--agent", "test")
        task = fixture.data(fixture.board("alice", "take", "--new", "Health", "--paths", "src/auth/**"))["task"]
        before = sh(["git", "rev-parse", "main"], fixture.origin).stdout
        health = Path(clone, "src/auth/health.txt")
        health.write_text("broken\n")
        sh(["git", "add", "src/auth/health.txt"], clone)
        sh(["git", "commit", "-qm", "break project test"], clone)
        rejected = fixture.board("alice", "done", task["id"], check=False)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("gate failed", fixture.data(rejected)["error"])
        self.assertIn("FAILED", fixture.data(rejected)["error"])
        self.assertEqual(sh(["git", "rev-parse", "main"], fixture.origin).stdout, before)
        self.assertEqual(fixture.data(fixture.board("alice", "show", task["id"]))["status"], "claimed")
        health.write_text("healthy\n")
        Path(clone, "src/auth/new.txt").write_text("fixed work\n")
        sh(["git", "add", "src/auth"], clone)
        sh(["git", "commit", "-qm", "repair project test"], clone)
        landed = fixture.data(fixture.board("alice", "done", task["id"]))
        self.assertEqual(landed["task"]["status"], "done")
