"""Read-only localhost dashboard: escaping, loopback binding, bounded logs and each view."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from agentlane import board as core, ui, worker
from tests.support import TestCase

HOSTILE = "<script>alert('xss')</script> \x1b[31mred\x1b[0m & <img src=x onerror=alert(1)>"
RUN_ID = "a" * 32
OLD_RUN = "b" * 32


def iso(minutes):
    return core.iso(core.now() + core.dt.timedelta(minutes=minutes))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def receipt(directory, run_id, common, **overrides):
    directory.mkdir(parents=True, exist_ok=True)
    data = {"run_id": run_id, "name": "engineer", "clone": str(directory), "task": "AL-1",
            "git_common_dir": str(common), "task_verified": False, "command": ["codex", HOSTILE],
            "started": "2026-09-18T10:00:00+00:00", "finished": "2026-09-18T10:20:30+00:00",
            "supervisor_pid": 42, "child_pid": 43, "exit_code": 0, "status": "exited",
            "stdout_log": str(directory / "stdout.log"), "stderr_log": str(directory / "stderr.log")}
    data.update(overrides)
    write(directory / "receipt.json", data)
    for stream in ("stdout.log", "stderr.log"):
        if not (directory / stream).exists():
            (directory / stream).write_text("ready\n", encoding="utf-8")
    return data


class UITests(TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="agentlane-ui-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.board_dir = self.tmp / "board"
        self.common = self.tmp / "common"
        self.registry = self.common / "agentlane-workers"
        self.common.mkdir(parents=True)

    def seed(self):
        write(self.board_dir / "tasks/AL-1.json", {
            "id": "AL-1", "title": "Harden " + HOSTILE, "status": "claimed", "owner": "alice",
            "kind": "code", "globs": ["src/auth/**"], "description": "Danger: " + HOSTILE,
            "created": iso(-600), "updated": iso(-30), "last_branch": "claim/AL-1-harden",
            "implementation": {"owners": ["alice"], "version": 1},
            "approvals": [{"id": "c" * 32, "reviewer": "bob", "timestamp": iso(-60),
                           "commit": "d" * 40, "base": "e" * 40, "evidence": "Gate green " + HOSTILE,
                           "claim": {"owner": "alice", "lease": "f" * 32, "branch": "claim/AL-1-harden",
                                     "globs": ["src/auth/**"], "hot": False}}],
            "landed": [{"before": "e" * 40, "after": "d" * 40, "paths": ["src/auth/**"], "tier": "code"}]})
        write(self.board_dir / "tasks/AL-2.json", {
            "id": "AL-2", "title": "Quiet task", "status": "open", "globs": [], "created": iso(-20)})
        write(self.board_dir / "claims/AL-1.json", {
            "id": "AL-1", "owner": "alice", "agent": "codex", "branch": "claim/AL-1-harden",
            "base": "main", "globs": ["src/auth/**"], "hot": False, "lease": "f" * 32,
            "created": iso(-400), "expires": iso(-100), "last_heartbeat": iso(-300),
            "ttl_minutes": 45, "extensions": 1})
        notes = self.board_dir / "notes/alice.jsonl"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("".join(json.dumps(r) + "\n" for r in [
            {"ts": iso(-300), "member": "alice", "kind": "finding", "task": "AL-1", "text": "First " + HOSTILE},
            {"ts": iso(-120), "member": "alice", "kind": "decision", "task": "AL-1", "text": "Second finding"},
        ]), encoding="utf-8")
        events = self.board_dir / "events/alice.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text("".join(json.dumps(r) + "\n" for r in [
            {"id": "1" * 32, "ts": iso(-400), "member": "alice", "agent": "codex", "type": "take",
             "task": "AL-1", "text": "alice took 'Harden'"},
            {"id": "2" * 32, "ts": iso(-60), "member": "bob", "agent": "codex", "type": "approved",
             "task": "AL-1", "text": "bob approved AL-1"},
            {"id": "3" * 32, "ts": iso(-50), "member": "alice", "agent": "codex", "type": "done",
             "task": "AL-1", "text": "alice landed AL-1"},
        ]), encoding="utf-8")
        receipt(self.registry / OLD_RUN, OLD_RUN, self.common)

    def reader(self):
        return ui.Reader(str(self.board_dir), str(self.common))

    def get(self, path, reader=None):
        server = ui.make_server(reader or self.reader(), "127.0.0.1", 0, 5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d%s" % (server.server_address[1], path)
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read().decode("utf-8")

    def test_overview_groups_tasks_and_marks_stale_claims(self):
        self.seed()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("claimed", body)
        self.assertIn("AL-1", body)
        self.assertIn("AL-2", body)
        self.assertIn("alice", body)
        self.assertIn("stale", body.lower())
        self.assertIn('http-equiv="refresh"', body)

    def test_agent_authored_text_renders_inert_everywhere(self):
        self.seed()
        for path in ("/", "/task?id=AL-1", "/activity", "/sessions"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("<script", body)
            self.assertNotIn("<img", body)
            self.assertNotIn("\x1b", body)
            self.assertNotIn(HOSTILE, body)
        self.assertIn("&lt;script&gt;", self.get("/task?id=AL-1")[1])

    def test_task_detail_shows_claim_notes_approvals_and_runs(self):
        self.seed()
        status, body = self.get("/task?id=AL-1")
        self.assertEqual(status, 200)
        for expected in ("claim/AL-1-harden", "src/auth/**", "f" * 32, "Second finding",
                         "bob", "d" * 40, OLD_RUN[:12], "stale"):
            self.assertIn(expected, body.lower() if expected == "stale" else body)
        self.assertLess(body.index("First "), body.index("Second finding"))

    def test_task_without_claim_notes_or_runs_renders(self):
        self.seed()
        status, body = self.get("/task?id=AL-2")
        self.assertEqual(status, 200)
        self.assertIn("Quiet task", body)
        self.assertIn("No notes", body)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/task?id=AL-404")
        self.assertEqual(caught.exception.code, 404)

    def test_missing_and_empty_boards_are_handled(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Board checkout missing", body)
        self.assertIn(ui.escape(str(self.board_dir)), body)
        self.assertNotIn("No tasks", body)
        self.assertEqual(200, self.get("/activity")[0])
        self.assertEqual(200, self.get("/sessions")[0])
        self.board_dir.mkdir()
        for path in ("/", "/activity"):
            body = self.get(path)[1]
            self.assertIn("No tasks directory", body)
            self.assertIn("--board-dir", body)
        for name in ("tasks", "claims", "notes", "events"):
            (self.board_dir / name).mkdir(parents=True)
        self.assertIn("No tasks", self.get("/")[1])
        self.assertNotIn("checkout missing", self.get("/")[1])
        self.assertIn("No notes or events", self.get("/activity")[1])

    def test_unknown_task_has_snapshot_refresh_guidance(self):
        self.seed()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/task?id=AL-404")
        with caught.exception as response:
            self.assertEqual(response.code, 404)
            body = response.read().decode("utf-8")
        for expected in ("Unknown task", "snapshot", "agentlane status", "reload",
                         ui.escape(str(self.board_dir))):
            self.assertIn(expected, body)

    def test_activity_paginates_all_events_and_notes(self):
        self.seed()
        records = [{"id": "%032x" % n, "ts": iso(n - 1000), "member": "alice",
                    "type": "note", "kind": "finding", "text": "ENTRY_%04d_END" % n}
                   for n in range(605)]
        events = self.board_dir / "events/alice.jsonl"
        events.write_text("".join(json.dumps(r) + "\n" for r in records[:500]), encoding="utf-8")
        # Overlapping notes also have corresponding events and must not be counted twice.
        notes = self.board_dir / "notes/alice.jsonl"
        notes.write_text("".join(json.dumps(r) + "\n" for r in records[490:]), encoding="utf-8")
        seen = []
        for number, count in ((1, 300), (2, 300), (3, 5)):
            status, body = self.get("/activity?page=%d" % number)
            self.assertEqual(status, 200)
            entries = re.findall(r"ENTRY_(\d+)_END", body)
            self.assertEqual(len(entries), count)
            self.assertIn("of 605", body)
            self.assertIn("Page %d of 3" % number, body)
            if number < 3:
                self.assertIn('href="/activity?page=%d"' % (number + 1), body)
                self.assertIn("Older", body)
            else:
                self.assertNotIn("Older", body)
            if number > 1:
                self.assertIn('href="/activity?page=%d"' % (number - 1), body)
                self.assertIn("Newer", body)
            seen.extend(int(n) for n in entries)
        self.assertEqual(seen, list(reversed(range(605))))

    def test_activity_rejects_invalid_pages_and_links_back_from_missing_page(self):
        self.seed()
        for value in ("0", "-1", "abc", "1.5", "9" * 5000):
            with self.subTest(page=value):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get("/activity?page=" + value)
                with caught.exception as response:
                    self.assertEqual(response.code, 400)
                    self.assertIn("positive integer", response.read().decode("utf-8"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/activity?page=2")
        with caught.exception as response:
            self.assertEqual(response.code, 404)
            self.assertIn('href="/activity"', response.read().decode("utf-8"))

    def test_activity_merges_notes_claims_approvals_and_landings(self):
        self.seed()
        status, body = self.get("/activity")
        self.assertEqual(status, 200)
        for expected in ("take", "approved", "done", "Second finding"):
            self.assertIn(expected, body)
        self.assertLess(body.index("alice landed AL-1"), body.index("alice took"))

    def test_sessions_list_runs_and_tail_the_running_one(self):
        self.seed()
        running = self.registry / RUN_ID
        receipt(running, RUN_ID, self.common, status="running", finished=None, exit_code=None)
        (running / "stdout.log").write_text("live output line\n", encoding="utf-8")
        write(self.common / "agentlane-worker.json", {"run_id": RUN_ID, "receipt": str(running / "receipt.json")})
        with worker.clone_lock(self.common, create=True):
            status, body = self.get("/sessions")
        self.assertEqual(status, 200)
        self.assertIn("running", body)
        self.assertIn("live output line", body)
        self.assertIn("AL-1", body)
        self.assertIn(OLD_RUN[:12], body)

    def test_ui_reads_receipts_without_worker_lock_or_liveness_probe(self):
        self.seed()
        receipt(self.registry / RUN_ID, RUN_ID, self.common,
                status="running", finished=None, exit_code=None)
        with patch.object(worker, "clone_lock", side_effect=AssertionError("UI took worker lock")), \
                patch.object(worker, "read_marker", side_effect=AssertionError("UI probed clone")):
            self.assertEqual(self.reader().runs()[0]["status"], "running")
            for path in ("/sessions", "/task?id=AL-1"):
                status, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn("running (unverified)", body)
        self.assertFalse((self.common / "agentlane-worker.lock").exists())

    def test_large_log_is_tailed_not_loaded(self):
        self.seed()
        log = self.registry / OLD_RUN / "stdout.log"
        with log.open("w", encoding="utf-8") as stream:
            stream.write("HEAD_OF_LOG\n")
            stream.write("filler line that repeats\n" * 120000)
            stream.write("TAIL_OF_LOG\n")
        self.assertGreater(log.stat().st_size, 2 * 1024 * 1024)
        text, truncated, size = ui.tail(str(log))
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode("utf-8")), ui.LOG_TAIL_BYTES + 64)
        self.assertEqual(size, log.stat().st_size)
        status, body = self.get("/sessions?run=" + OLD_RUN)
        self.assertEqual(status, 200)
        self.assertIn("TAIL_OF_LOG", body)
        self.assertNotIn("HEAD_OF_LOG", body)
        self.assertIn("truncated", body.lower())
        self.assertLess(len(body), 4 * ui.LOG_TAIL_BYTES)

    def test_log_path_outside_the_receipt_directory_is_refused(self):
        self.seed()
        (self.tmp / "outside.txt").write_text("SECRET_FILE_CONTENT\n", encoding="utf-8")
        for planted in (str(self.tmp / "outside.txt"),
                        str(self.registry / OLD_RUN / ".." / ".." / ".." / "outside.txt")):
            receipt(self.registry / OLD_RUN, OLD_RUN, self.common, stdout_log=planted)
            status, body = self.get("/sessions?run=" + OLD_RUN)
            self.assertEqual(status, 200)
            self.assertNotIn("SECRET_FILE_CONTENT", body)
            self.assertIn("outside this run&#x27;s receipt directory", body)

    def test_log_checks_and_reads_the_resolved_contained_path(self):
        self.seed()
        directory = self.registry / OLD_RUN
        (directory / "subdir").mkdir()
        original = str(directory / "subdir" / ".." / "stdout.log")
        run = receipt(directory, OLD_RUN, self.common, stdout_log=original)
        resolved = os.path.realpath(original)
        self.assertNotEqual(original, resolved)
        with patch.object(ui.os.path, "isfile", wraps=os.path.isfile) as isfile, \
                patch.object(ui, "tail", wraps=ui.tail) as read_tail:
            body = ui.log_block(self.reader(), run, "stdout")
        self.assertIn("ready", body)
        isfile.assert_called_once_with(resolved)
        read_tail.assert_called_once_with(resolved)

    def test_unreadable_receipt_does_not_break_the_sessions_view(self):
        self.seed()
        broken = self.registry / ("c" * 32)
        broken.mkdir(parents=True)
        (broken / "receipt.json").write_text("{not json", encoding="utf-8")
        status, body = self.get("/sessions")
        self.assertEqual(status, 200)
        self.assertIn("unreadable", body.lower())

    def test_non_loopback_bind_is_refused(self):
        for host in ("0.0.0.0", "192.0.2.10", "::"):
            with self.assertRaises(core.BoardError):
                ui.make_server(self.reader(), host, 0, 5)
        for host in ("127.0.0.1", "localhost", "::1"):
            try:
                server = ui.make_server(self.reader(), host, 0, 5)
            except OSError as error:  # a host with IPv6 disabled, which is not a refusal
                self.assertEqual(host, "::1", error)
                continue
            server.server_close()

    def test_ipv4_mapped_ipv6_is_rejected_before_binding(self):
        for address in ("::ffff:127.0.0.1", "::ffff:192.0.2.10"):
            infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
            with self.subTest(address=address), \
                    patch.object(ui.socket, "getaddrinfo", return_value=infos), \
                    patch.object(ui.ThreadingHTTPServer, "server_bind") as bind:
                with self.assertRaisesRegex(core.BoardError, "IPv4-mapped IPv6"):
                    ui.make_server(self.reader(), address, 0, 5)
                bind.assert_not_called()

    def test_server_serves_no_mutating_methods(self):
        self.seed()
        server = ui.make_server(self.reader(), "127.0.0.1", 0, 5)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/task?id=AL-1" % server.server_address[1], data=b"x", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=30)
            self.assertIn(caught.exception.code, (405, 501))
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(sorted(os.listdir(self.board_dir)), ["claims", "events", "notes", "tasks"])

    def test_cli_registers_ui_with_loopback_defaults(self):
        args = core.build_parser().parse_args(["--board-dir", "b", "ui"])
        self.assertEqual(args.cmd, "ui")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.board_dir, "b")
        self.assertIsInstance(args.port, int)
        self.assertEqual(core.build_parser().parse_args(["ui", "--port", "8123"]).port, 8123)

    def test_serve_prints_its_url_and_stops_cleanly_on_interrupt(self):
        self.seed()
        args = core.build_parser().parse_args(["--board-dir", str(self.board_dir), "ui", "--port", "0"])
        repo = types.SimpleNamespace(cfg=dict(core.DEFAULTS), git_common=str(self.common))
        printed = io.StringIO()
        with patch.object(ui.ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(printed):
            self.assertEqual(ui.serve(args, repo), 0)
        self.assertIn("http://127.0.0.1:", printed.getvalue())
        self.assertIn("Stopped.", printed.getvalue())

    def test_escape_neutralizes_markup_and_control_characters(self):
        rendered = ui.escape(HOSTILE)
        self.assertNotIn("<", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertEqual(ui.escape(None), "")


if __name__ == "__main__":
    unittest.main()
