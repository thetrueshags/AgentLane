"""Evidence integrity and bounded Git process use against committed board history."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from agentlane import board as core, evidence
from tests.support import TestCase, Fixture


class EvidenceTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.root = self.fx.clone("alice")
        self.fx.board("alice", "init")
        self.fx.board("alice", "join", "--name", "alice", "--agent", "test")

    def test_export_does_not_reread_unchanged_backlog_for_every_note(self):
        board = core.Board(core.Repo(self.root))
        title = "Café 🌲"

        def backlog(b):
            for number in range(1, 21):
                task = {"id": "AL-%d" % number, "title": title, "status": "open"}
                # Include actual multibyte UTF-8 in the committed blob.
                Path(b.path("tasks", task["id"] + ".json")).write_bytes(
                    json.dumps(task, ensure_ascii=False).encode("utf-8"))

        board.txn("test: backlog", backlog)
        for number in range(12):
            board.txn("test: activity", lambda b, n=number: b.note("alice", "finding", "Note %d" % n))
        destination = Path(self.fx.tmp, "evidence")
        args = core.build_parser().parse_args(["--json", "export", "--out", str(destination)])
        real_popen = subprocess.Popen
        commands = []

        def launch(command, *positional, **kwargs):
            commands.append(command)
            return real_popen(command, *positional, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=launch), contextlib.redirect_stdout(io.StringIO()):
            evidence.cmd_export(args, core.Repo(self.root))
        tasks = [json.loads(line)["record"] for line in (destination / "tasks.jsonl").read_text().splitlines()]
        notes = [json.loads(line)["record"] for line in (destination / "notes.jsonl").read_text().splitlines()]
        self.assertEqual(len(tasks), 20)
        self.assertTrue(all(task["title"] == title for task in tasks))
        self.assertEqual({note["text"] for note in notes}, {"Note %d" % n for n in range(12)})
        git_processes = [command for command in commands if command[0] == "git"]
        self.assertLess(len(git_processes), 60, "Export process count must scale with history, not backlog × history")

    def test_export_preserves_note_history_after_log_rewrite(self):
        board = core.Board(core.Repo(self.root))
        first_commits = {}
        for text in ("First note", "Second note"):
            board.txn("test: note", lambda b, value=text: b.note("alice", "finding", value))
            first_commits[text] = core.git(["rev-parse", "HEAD"], cwd=board.dir).stdout.strip()

        def rewrite(b):
            notes = core.read_jsonl(b.path("notes", "alice.jsonl"))
            Path(b.path("notes", "alice.jsonl")).write_bytes((json.dumps(notes[-1]) + "\n").encode("utf-8"))

        board.txn("test: rewritten log", rewrite)
        board.txn("test: append after rewrite", lambda b: b.note("alice", "finding", "Third note"))
        first_commits["Third note"] = core.git(["rev-parse", "HEAD"], cwd=board.dir).stdout.strip()
        output = Path(self.fx.tmp, "evidence")
        self.fx.board("alice", "export", "--out", str(output))
        rows = [json.loads(line) for line in (output / "notes.jsonl").read_text().splitlines()]
        self.assertEqual({row["record"]["text"]: row["board_commit"] for row in rows}, first_commits)

    def test_bounded_export_context_cannot_descend_from_selected_commit(self):
        board = core.Board(core.Repo(self.root))
        commits = []
        for time, text in (("10:00", "Context"), ("10:30", "Selected"), ("10:20", "Clock moved back")):
            timestamp = "2030-01-01T%s:00+00:00" % time
            with patch.dict(os.environ, {"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}):
                board.txn("test: timestamped note", lambda b, value=text: b.note("alice", "finding", value))
            commits.append(core.git(["rev-parse", "HEAD"], cwd=board.dir).stdout.strip())
        output = Path(self.fx.tmp, "bounded-evidence")
        result = self.fx.data(self.fx.board("alice", "export", "--since", "2030-01-01T10:25:00Z",
                                           "--out", str(output)))
        self.assertEqual(result["manifest"]["context_commit"], commits[0])
        rows = [json.loads(line) for line in (output / "notes.jsonl").read_text().splitlines()]
        selected = next(row for row in rows if row["record"]["text"] == "Selected")
        self.assertEqual(selected["board_commit"], commits[1])
        self.assertFalse(selected["context"])
        self.assertNotIn("Clock moved back", [row["record"]["text"] for row in rows])

    def test_export_includes_transient_records_on_merged_board_branch(self):
        board = core.Board(core.Repo(self.root))
        board.sync()
        git = lambda *args: core.git(list(args), cwd=board.dir).stdout.strip()
        base = git("rev-parse", "HEAD")
        git("checkout", "-qb", "evidence-side")
        task = {"id": "AL-1", "title": "Transient branch task", "status": "open"}
        core.write_json(board.path("tasks", "AL-1.json"), task)
        git("add", "tasks/AL-1.json")
        git("commit", "-qm", "Side branch task")
        introduced = git("rev-parse", "HEAD")
        git("rm", "-q", "tasks/AL-1.json")
        git("commit", "-qm", "Remove side branch task")
        git("checkout", "-q", "--detach", base)
        git("merge", "--no-ff", "-qm", "Merge board branch", "evidence-side")
        git("push", "-q", board.remote, "HEAD:refs/heads/" + board.branch)
        output = Path(self.fx.tmp, "merged-evidence")
        self.fx.board("alice", "export", "--out", str(output))
        rows = [json.loads(line) for line in (output / "tasks.jsonl").read_text().splitlines()]
        self.assertEqual(rows, [{"board_commit": introduced, "context": False, "record": task}])
