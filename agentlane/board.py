#!/usr/bin/env python3
"""AgentLane: shared work for people and agents, coordinated through Git boards.

Everything lives on a `board` branch of the shared repo. Claims are atomic because a git ref
update is compare-and-swap: we fetch, check, commit, push; a rejected push means someone else
got there first and we retry. Standard library only, Python 3.9+.

Humans and agents use the same CLI. Run `agentlane --help` for the commands.
"""
import argparse
import contextlib
import tempfile
import math
from agentlane import __version__
import datetime as dt
import fnmatch
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import random
import sys
import time
import urllib.request
import uuid

DEFAULTS = {
    "remote": "origin",
    "main_branch": "main",
    "board_branch": "board",
    "ttl_minutes": 45,
    "max_extensions": 2,
    "hot_ttl_minutes": 10,
    "stall_minutes": 10,
    "stall_release_minutes": 15,
    "hot_paths": ["package.json", "package-lock.json", "pyproject.toml", "mix.exs", "go.mod",
                  "src/App.*", "src/main.*", "src/router.*", "src/routes.*"],
    "docs_globs": ["docs/**", "pitch/**", "*.md", "**/*.md"],
    "warn_lines": 300,
    "refuse_lines": 800,
    "land_retries": 4,
    "push_retries": 8,
    "heartbeat_min_seconds": 60,
    "landing_mode": "direct",
    "require_review": False,
}

TASK_KINDS = ["code", "docs", "test", "research", "design", "ops", "other"]

RETRYABLE_PUSH_MARKERS = ("non-fast-forward", "fetch first", "failed to push", "rejected",
                          "cannot lock ref", "stale info")


class BoardError(Exception):
    """A user-facing error. Message is printed, exit code 1."""


class Conflict(BoardError):
    """The requested claim overlaps a live claim."""


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise BoardError("Invalid task/member identifier; use the ID shown by agentlane list.")


def normalize_paths(paths):
    result = []
    for path in paths:
        if not isinstance(path, str):
            raise BoardError("Claim paths must be strings")
        path = path.strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ":" in path or any(p in ("", ".", "..", ".git") for p in path.split("/")) or any(ord(c) < 32 for c in path):
            raise BoardError("Invalid claim path %r. Use repository-relative paths such as src/auth/**." % path)
        if path not in result:
            result.append(path)
    return result


def validate_config(cfg):
    if type(cfg["require_review"]) is not bool:
        raise BoardError("Configuration require_review must be a boolean")
    for key in ("ttl_minutes", "hot_ttl_minutes", "stall_minutes", "stall_release_minutes", "push_retries", "land_retries"):
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise BoardError("Configuration %s must be a positive number" % key)
    for key in ("max_extensions", "heartbeat_min_seconds", "warn_lines", "refuse_lines"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], (int, float)) or not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise BoardError("Configuration %s must be a non-negative number" % key)
    for key in ("remote", "main_branch", "board_branch"):
        value = cfg[key]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) or ".." in value:
            raise BoardError("Invalid %s in .harness/config.json" % key)
    if cfg["main_branch"] == cfg["board_branch"]:
        raise BoardError("main_branch and board_branch must be different")
    for key in ("hot_paths", "docs_globs"):
        if not isinstance(cfg[key], list):
            raise BoardError("Configuration %s must be a list of paths" % key)
        cfg[key] = normalize_paths(cfg[key])


def validate_record(kind, record):
    required = {"tasks": ("id", "title", "status"),
                "claims": ("id", "owner", "branch", "created", "expires"),
                "members": ("name",)}[kind]
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k] for k in required):
        raise BoardError("Malformed %s record: required fields are %s. Inspect board history." % (kind, ", ".join(required)))
    validate_id(record.get("id", record.get("name")))
    if kind == "tasks" and record["status"] not in ("open", "claimed", "blocked", "review", "done"):
        raise BoardError("Unknown task state %r" % record["status"])
    if kind == "tasks":
        from agentlane.review import validate_task_review
        validate_task_review(record)
    if kind in ("tasks", "claims"):
        paths = record.get("globs", [])
        if not isinstance(paths, list) or (kind == "claims" and not paths):
            raise BoardError("Malformed paths in %s" % record["id"])
        normalize_paths(paths)
    if kind == "claims":
        for key in ("created", "expires", "last_heartbeat"):
            if record.get(key):
                try:
                    value = parse_iso(record[key])
                    if value.tzinfo is None:
                        raise ValueError("timezone required")
                except (ValueError, TypeError, AttributeError):
                    raise BoardError("Invalid %s timestamp in claim %s" % (key, record["id"]))
        for key in ("extensions", "ttl_minutes"):
            if isinstance(record.get(key), bool) or not isinstance(record.get(key), (int, float)) or not math.isfinite(record[key]) or record[key] < 0:
                raise BoardError("Invalid %s in claim %s" % (key, record["id"]))


def claim_stale(claim, cfg):
    at = now()
    return at >= parse_iso(claim["expires"]) or (at - parse_iso(claim.get("last_heartbeat") or claim["created"])).total_seconds() >= cfg["stall_release_minutes"] * 60


def require_live(claim, cfg):
    if claim_stale(claim, cfg):
        raise BoardError("Claim %s expired. Your branch is safe; run agentlane take %s to recover ownership." % (claim["id"], claim["id"]))


def find_bash():
    if os.name == "nt" and shutil.which("git"):
        candidate = os.path.join(os.path.dirname(os.path.dirname(shutil.which("git"))), "bin", "bash.exe")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("bash")


def hook_path(repo):
    path = repo.git(["rev-parse", "--git-path", "hooks/pre-push"]).stdout.strip()
    return path if os.path.isabs(path) else os.path.join(repo.root, path)


@contextlib.contextmanager
def checkout_lock(repo):
    """OS lock protects the disposable board worktree; process exit releases it."""
    key = os.path.realpath(repo.git_common)
    if os.environ.get("AGENTLANE_LOCK_HELD") == key:
        yield  # a hook invoked synchronously by this command
        return
    with open(os.path.join(repo.git_common, "agentlane.lock"), "a+b") as f:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BoardError("Another AgentLane command is running in this checkout. Retry when it finishes; use separate clones for concurrent workers.")
        previous = os.environ.get("AGENTLANE_LOCK_HELD")
        os.environ["AGENTLANE_LOCK_HELD"] = key
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("AGENTLANE_LOCK_HELD", None)
            else:
                os.environ["AGENTLANE_LOCK_HELD"] = previous



def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def run(cmd, cwd=None, check=True, env=None, input_text=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    p = subprocess.run(cmd, cwd=cwd, env=full_env, input=input_text, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise BoardError("command failed: %s\n%s%s" % (" ".join(cmd), p.stdout, p.stderr))
    return p


def git(args, cwd=None, check=True, env=None):
    return run(["git"] + list(args), cwd=cwd, check=check, env=env)


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "task"


def short_id():
    return uuid.uuid4().hex[:6]


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as e:
        raise BoardError("Invalid JSON in %s: %s. Restore this file from a known-good Git commit." % (path, e))


def write_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agentlane-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
            if any(not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ("ts", "member", "text")) for r in records):
                raise ValueError("each event/note needs ts, member and text strings")
            return records
    except FileNotFoundError:
        return []
    except (ValueError, UnicodeError) as e:
        raise BoardError("Invalid JSONL in %s: %s. Restore it from Git history." % (path, e))


def literal_prefix(glob):
    """The part of a glob before the first wildcard, as a path prefix."""
    m = re.search(r"[*?\[]", glob)
    return glob if m is None else glob[:m.start()]


def glob_matches_path(glob, path):
    """fnmatch with `**` meaning any depth, and a directory glob matching its children."""
    if glob.endswith("/**"):
        base = glob[:-3]
        return path == base or path.startswith(base + "/")
    if "**" in glob:
        pattern = glob.replace("**", "*")
        return fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path, glob)


def globs_overlap(a, b):
    """Conservative: two globs overlap if either could match a path the other covers.

    Uses each glob's literal prefix as a witness path. Fails closed on doubt.
    """
    a = a.casefold()
    b = b.casefold()
    if a == b:
        return True
    pa, pb = literal_prefix(a).rstrip("/"), literal_prefix(b).rstrip("/")
    if glob_matches_path(a, pb) or glob_matches_path(b, pa):
        return True
    if pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")):
        return True
    if not pa or not pb:
        return True
    if (literal_prefix(a) != a and pb.startswith(literal_prefix(a))) or (literal_prefix(b) != b and pa.startswith(literal_prefix(b))):
        return True
    return False


def any_overlap(globs_a, globs_b):
    for ga in globs_a:
        for gb in globs_b:
            if globs_overlap(ga, gb):
                return (ga, gb)
    return None


def paths_outside(paths, globs):
    return [p for p in paths if not any(glob_matches_path(g, p) for g in globs)]


def paths_matching(paths, globs):
    return [p for p in paths if any(glob_matches_path(g, p) for g in globs)]



class Repo:
    def __init__(self, cwd=None):
        p = git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
        if p.returncode != 0:
            raise BoardError("not inside a git repository")
        self.root = p.stdout.strip()
        self.git_common = git(["rev-parse", "--git-common-dir"], cwd=self.root).stdout.strip()
        if not os.path.isabs(self.git_common):
            self.git_common = os.path.join(self.root, self.git_common)
        self.cfg = dict(DEFAULTS)
        config = read_json(os.path.join(self.root, ".harness", "config.json"), {})
        if not isinstance(config, dict):
            raise BoardError(".harness/config.json must contain a JSON object")
        self.cfg.update(config)
        validate_config(self.cfg)
        if self.cfg["landing_mode"] not in ("direct", "pr"):
            raise BoardError("landing_mode must be 'direct' or 'pr'")
        self.local = read_json(os.path.join(self.root, ".harness", "member.json"), {}) or {}
        if not isinstance(self.local, dict):
            raise BoardError(".harness/member.json must contain a JSON object; rejoin with agentlane join")

    def git(self, args, check=True, env=None):
        return git(args, cwd=self.root, check=check, env=env)

    @property
    def remote(self):
        return self.cfg["remote"]

    @property
    def main(self):
        return self.cfg["main_branch"]

    @property
    def member(self):
        name = os.environ.get("BOARD_MEMBER") or self.local.get("name")
        if not name:
            name = self.git(["config", "user.name"], check=False).stdout.strip() or getpass.getuser()
        return slug(name)

    @property
    def agent(self):
        return os.environ.get("BOARD_AGENT") or self.local.get("agent") or "unknown-agent"

    def remote_main(self):
        return "%s/%s" % (self.remote, self.main)



class Board:
    """A checkout of the board branch with fetch / mutate / commit / push semantics."""

    def __init__(self, repo, board_dir=None):
        self.repo = repo
        self.cfg = repo.cfg
        self.branch = self.cfg["board_branch"]
        self.remote = repo.remote
        self.external = board_dir is not None
        self.dir = os.path.abspath(board_dir or os.path.join(repo.git_common, "board-wt"))

    def exists_on_remote(self):
        p = self.repo.git(["ls-remote", "--exit-code", "--heads", self.remote, self.branch], check=False)
        return p.returncode == 0

    def ensure(self):
        if self.external:
            if not os.path.isdir(os.path.join(self.dir, ".git")) and not os.path.isfile(os.path.join(self.dir, ".git")):
                raise BoardError("board dir %s is not a git checkout" % self.dir)
            return
        if not os.path.isdir(self.dir):
            if not self.exists_on_remote():
                raise BoardError("no `%s` branch on %s yet. Run: agentlane init" % (self.branch, self.remote))
            self.repo.git(["fetch", "-q", self.remote, self.branch])
            self.repo.git(["worktree", "add", "-q", "--detach", self.dir, "FETCH_HEAD"])

    def sync(self):
        self.ensure()
        git(["fetch", "-q", self.remote, self.branch], cwd=self.dir)
        git(["reset", "-q", "--hard", "FETCH_HEAD"], cwd=self.dir)
        git(["clean", "-qfd"], cwd=self.dir)
        self.validate()

    def commit_and_push(self, message):
        git(["add", "-A"], cwd=self.dir)
        if git(["status", "--porcelain"], cwd=self.dir).stdout.strip() == "":
            return True
        git(["-c", "user.name=board", "-c", "user.email=board@agentlane.local",
             "commit", "-q", "-m", message], cwd=self.dir)
        p = git(["push", "-q", self.remote, "HEAD:refs/heads/%s" % self.branch], cwd=self.dir, check=False)
        if p.returncode == 0:
            return True
        err = (p.stderr + p.stdout).lower()
        if any(m in err for m in RETRYABLE_PUSH_MARKERS):
            return False
        raise BoardError("push of board failed:\n" + p.stderr)

    def txn(self, message, mutate):
        """Fetch, mutate, commit, push; retry on a lost race. `mutate(board)` returns a result."""
        retries = int(self.cfg["push_retries"])
        for attempt in range(retries):
            self.sync()
            result = mutate(self)
            if git(["status", "--porcelain"], cwd=self.dir).stdout.strip() == "":
                return result
            self.render()
            if self.commit_and_push(message):
                return result
            time.sleep(min(8.0, 0.2 * (2 ** attempt)) * random.uniform(0.5, 1.5))
        raise BoardError("lost the race to update the board %d times; try again" % retries)

    def init(self):
        if self.exists_on_remote():
            raise BoardError("`%s` already exists on %s" % (self.branch, self.remote))
        if os.path.isdir(self.dir):
            raise BoardError("stale board worktree at %s; remove it first" % self.dir)
        self.repo.git(["worktree", "add", "-q", "--detach", self.dir])
        git(["checkout", "-q", "--orphan", self.branch], cwd=self.dir)
        git(["rm", "-rfq", "--cached", "."], cwd=self.dir, check=False)
        for entry in os.listdir(self.dir):
            if entry == ".git":
                continue
            path = os.path.join(self.dir, entry)
            # The disposable board worktree is the only tree cleared during init.
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        for d in ("tasks", "claims", "notes", "events", "members"):
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
            open(os.path.join(self.dir, d, ".keep"), "w").close()
        write_json(os.path.join(self.dir, "narrator.json"), {"posted_through": None})
        self.render()
        git(["add", "-A"], cwd=self.dir)
        git(["-c", "user.name=board", "-c", "user.email=board@agentlane.local",
             "commit", "-q", "-m", "board: init"], cwd=self.dir)
        git(["push", "-q", self.remote, "HEAD:refs/heads/%s" % self.branch], cwd=self.dir)
        git(["checkout", "-q", "--detach"], cwd=self.dir)

    def path(self, *parts):
        path = os.path.abspath(os.path.join(self.dir, *parts))
        root = os.path.realpath(self.dir)
        if os.path.normcase(os.path.commonpath([root, os.path.realpath(path)])) != os.path.normcase(root):
            raise BoardError("Board path must stay inside the board checkout")
        return path

    def validate(self):
        for kind in ("tasks", "claims", "members"):
            for record in self._list(kind):
                validate_record(kind, record)
        tasks = {t["id"]: t for t in self.tasks()}
        claims = self.claims()
        for c in claims:
            t = tasks.get(c["id"])
            if not t or t["status"] not in ("claimed", "blocked") or t.get("owner") != c["owner"]:
                raise BoardError("Inconsistent claim %s; inspect board history before repairing it" % c["id"])
        for i, c in enumerate(claims):
            for other in claims[i + 1:]:
                if any_overlap(c["globs"], other["globs"]):
                    raise BoardError("Board contains overlapping claims %s and %s; resolve ownership explicitly" % (c["id"], other["id"]))
        for t in tasks.values():
            if t["status"] == "claimed" and not any(c["id"] == t["id"] for c in claims):
                raise BoardError("Task %s is claimed but its claim is missing; inspect board history" % t["id"])
        for record in self.all_events() + self.all_notes():
            if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ("ts", "member", "text")):
                raise BoardError("Invalid event/note record; restore it from board history")

    def next_id(self):
        numbers = [int(t["id"][3:]) for t in self.tasks() if re.fullmatch(r"AL-\d+", t["id"])]
        return "AL-%d" % (max(numbers, default=0) + 1)

    def _list(self, kind):
        d = self.path(kind)
        out = []
        if not os.path.isdir(d):
            return out
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                record = read_json(self.path(kind, name))
                validate_record(kind, record)
                if record.get("id", record.get("name")) != name[:-5]:
                    raise BoardError("Board filename does not match its record ID: " + name)
                out.append(record)
        return out

    def tasks(self):
        return self._list("tasks")

    def claims(self):
        return self._list("claims")

    def members(self):
        return self._list("members")

    def task(self, task_id):
        validate_id(task_id)
        t = read_json(self.path("tasks", task_id + ".json"))
        if t is None:
            raise BoardError("No task %s. Run agentlane list to see task IDs." % task_id)
        validate_record("tasks", t)
        return t

    def claim(self, task_id):
        validate_id(task_id)
        return read_json(self.path("claims", task_id + ".json"))

    def save_task(self, t):
        t["updated"] = iso(now())
        write_json(self.path("tasks", t["id"] + ".json"), t)

    def save_claim(self, c):
        validate_record("claims", c)
        write_json(self.path("claims", c["id"] + ".json"), c)

    def drop_claim(self, task_id):
        p = self.path("claims", task_id + ".json")
        if os.path.exists(p):
            os.remove(p)

    def claims_of(self, member):
        return [c for c in self.claims() if c["owner"] == member]

    def note(self, member, kind, text, task=None, agent=None):
        rec = {"ts": iso(now()), "member": member, "kind": kind, "text": text, "task": task}
        append_jsonl(self.path("notes", member + ".jsonl"), rec)
        self.event(member, "note", text, task=task, agent=agent, kind=kind)

    def event(self, member, etype, text, task=None, agent=None, **extra):
        rec = {"id": uuid.uuid4().hex, "ts": iso(now()), "member": member, "agent": agent, "type": etype, "task": task, "text": text}
        rec.update(extra)
        append_jsonl(self.path("events", member + ".jsonl"), rec)

    def all_notes(self):
        out = []
        d = self.path("notes")
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".jsonl"):
                    records = read_jsonl(self.path("notes", name))
                    if any(not isinstance(r.get("kind"), str) for r in records):
                        raise BoardError("Invalid note kind in " + name)
                    out.extend(records)
        return sorted(out, key=lambda r: (r.get("ts", ""), r.get("member", ""), r.get("id", "")))

    def all_events(self):
        out = []
        d = self.path("events")
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".jsonl"):
                    out.extend(read_jsonl(self.path("events", name)))
        return sorted(out, key=lambda r: (r.get("ts", ""), r.get("member", ""), r.get("id", "")))

    def expire(self, at=None):
        at = at or now()
        released = []
        for c in self.claims():
            expires = parse_iso(c["expires"])
            last = parse_iso(c.get("last_heartbeat") or c["created"])
            stalled_for = (at - last).total_seconds() / 60
            reason = None
            if at >= expires:
                reason = "time is up (%d min TTL, %d extensions used)" % (c["ttl_minutes"], c["extensions"])
            elif stalled_for >= float(self.cfg["stall_release_minutes"]):
                reason = "no heartbeat for %d minutes" % int(stalled_for)
            if reason:
                t = self.task(c["id"])
                t["status"] = "open"
                t["owner"] = None
                t["last_branch"] = c["branch"]
                self.save_task(t)
                self.drop_claim(c["id"])
                self.event(c["owner"], "expired", "claim on '%s' released: %s. Branch %s still has the work." %
                           (t["title"], reason, c["branch"]), task=c["id"], agent=c.get("agent"))
                released.append((c, reason))
        return released

    def render(self):
        at = now()
        claims = {c["id"]: c for c in self.claims()}
        tasks = self.tasks()
        lines = ["# AgentLane Board", "", "Updated %s. Generated by AgentLane; do not edit by hand." % iso(at), ""]

        def minutes_left(c):
            return int((parse_iso(c["expires"]) - at).total_seconds() // 60)

        lines.append("## In Progress")
        lines.append("")
        live = [t for t in tasks if t["status"] == "claimed" and t["id"] in claims]
        if not live:
            lines.append("- nothing claimed right now")
        for t in live:
            c = claims[t["id"]]
            hb = parse_iso(c.get("last_heartbeat") or c["created"])
            stalled = (at - hb).total_seconds() / 60 >= float(self.cfg["stall_minutes"])
            flag = " STALE - recover with agentlane take" if claim_stale(c, self.cfg) else " STALLED?" if stalled else ""
            lines.append("- **%s** by %s (%s), %d min left, paths `%s`, branch `%s`%s" %
                         (t["id"] + " - " + t["title"], c["owner"], c.get("agent", "?"), minutes_left(c),
                          ", ".join(c["globs"]), c["branch"], flag))
        lines.append("")
        lines.append("## Ready to test")
        lines.append("")
        review = [t for t in tasks if t["status"] == "review"]
        if not review:
            lines.append("- nothing waiting for a tester")
        for t in review:
            lines.append("- **%s** by %s. Branch `%s`. Say: \"help me test %s\"" %
                         (t["title"], t.get("owner") or "?", t.get("last_branch", "?"), t["id"]))
            if t.get("pr"):
                lines.append("  PR: %s" % t["pr"])
        lines.append("")
        lines.append("## Blocked")
        lines.append("")
        blocked = [t for t in tasks if t["status"] == "blocked"]
        if not blocked:
            lines.append("- nobody is stuck")
        for t in blocked:
            lines.append("- **%s** (%s): %s" % (t["title"], t.get("owner") or "?", t.get("blocker", "")))
        lines.append("")
        lines.append("## Available")
        lines.append("")
        open_tasks = [t for t in tasks if t["status"] == "open"]
        if not open_tasks:
            lines.append("- backlog is empty. Propose one: \"add a task ...\"")
        for t in open_tasks:
            lines.append("- `%s` **%s** (%s) paths `%s`" % (t["id"], t["title"], t.get("kind", "code"), ", ".join(t.get("globs", []))))
            if t.get("description"):
                lines.extend("  " + line for line in t["description"].splitlines())
        lines.append("")
        lines.append("## Done Recently")
        lines.append("")
        done = [t for t in tasks if t["status"] == "done"]
        if not done:
            lines.append("- nothing landed yet")
        for t in sorted(done, key=lambda t: t.get("updated", t.get("created", "")))[-15:]:
            lines.append("- %s by %s" % (t["title"], t.get("owner") or "?"))
        lines.append("")
        lines.append("## Recent notes")
        lines.append("")
        notes = self.all_notes()[-15:]
        if not notes:
            lines.append("- no notes yet")
        for n in notes:
            lines.append("- %s **%s** [%s] %s" % (n["ts"][11:16], n["member"], n["kind"], n["text"]))
        lines.append("")
        lines.append("## Team")
        lines.append("")
        for m in self.members():
            lines.append("- %s (%s)" % (m["name"], m.get("agent", "?")))
        lines.extend(["", "## Recent Activity", ""])
        for event in self.all_events()[-12:]:
            lines.append("- %s %s / %s [%s]: %s" % (event["ts"], event.get("agent") or "worker", event["member"], event.get("task") or "board", event["text"]))
        with open(self.path("BOARD.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")



def out(args, human, data):
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(human)


def cmd_init(args, repo):
    board = Board(repo, args.board_dir)
    if not repo.git(["remote", "get-url", repo.remote], check=False).returncode == 0:
        raise BoardError("No remote configured. Add one with git remote add %s <repository-url>." % repo.remote)
    if repo.git(["rev-parse", "--verify", "HEAD"], check=False).returncode:
        raise BoardError("Create an initial Git commit and push %s before agentlane init." % repo.main)
    if repo.git(["ls-remote", "--exit-code", "--heads", repo.remote, repo.main], check=False).returncode:
        raise BoardError("Remote main branch missing or unreachable. Push %s and check Git credentials." % repo.main)
    board.init()
    out(args, "board branch `%s` created on %s. Next: agentlane join --name <you> --agent <agent>" %
        (board.branch, board.remote), {"ok": True, "branch": board.branch})


def cmd_join(args, repo):
    name = slug(args.name or repo.member)
    agent = args.agent or repo.agent
    local = os.path.join(repo.root, ".harness", "member.json")
    board = Board(repo, args.board_dir)

    def mutate(b):
        write_json(b.path("members", name + ".json"), {"name": name, "agent": agent, "joined": iso(now())})
        b.event(name, "joined", "%s joined with %s" % (name, agent), agent=agent)
        return {"name": name, "agent": agent}

    res = board.txn("board: %s joined" % name, mutate)
    write_json(local, {"name": name, "agent": agent})
    out(args, "welcome %s. Your agent is recorded as %s. Try: agentlane status" % (name, agent), res)


def cmd_add(args, repo):
    title = args.title.strip()
    if not title:
        raise BoardError("a task needs a non-empty title")
    globs = normalize_paths([g.strip() for g in (args.globs or "").split(",") if g.strip()])
    board = Board(repo, args.board_dir)

    def mutate(b):
        t = {"id": b.next_id(), "title": title, "description": args.description or "",
             "kind": args.kind or "other", "globs": globs, "status": "open", "owner": None,
             "created_by": repo.member, "created": iso(now()),
             "implementation": {"version": 1, "owners": []}}
        b.save_task(t)
        b.event(repo.member, "add", "%s added '%s' to the backlog" % (repo.member, title),
                task=t["id"], agent=repo.agent)
        return t

    task = board.txn("board: %s adds task" % repo.member, mutate)
    out(args, "added '%s' [%s]. No paths reserved; take it when ready to work." %
        (title, task["id"]), task)


def status_data(board, repo):
    at = now()
    claims = {c["id"]: c for c in board.claims()}
    tasks = board.tasks()
    me = repo.member

    def view(t):
        c = claims.get(t["id"])
        v = {"id": t["id"], "title": t["title"], "kind": t.get("kind"), "status": t["status"],
             "owner": t.get("owner"), "globs": t.get("globs", []),
             "description": t.get("description", ""), "pr": t.get("pr"),
             "created": t.get("created"), "updated": t.get("updated", t.get("created"))}
        if c:
            v["minutes_left"] = int((parse_iso(c["expires"]) - at).total_seconds() // 60)
            v["branch"] = c["branch"]
            v["agent"] = c.get("agent")
            hb = parse_iso(c.get("last_heartbeat") or c["created"])
            v["stalled"] = (at - hb).total_seconds() / 60 >= float(board.cfg["stall_minutes"])
            v["stale"] = bool(claim_stale(c, board.cfg))
            v["extensions_left"] = int(board.cfg["max_extensions"]) - c["extensions"]
        if t["status"] == "review":
            v["branch"] = t.get("last_branch")
        if t["status"] == "blocked":
            v["blocker"] = t.get("blocker")
        return v

    return {
        "me": me,
        "landing_mode": repo.cfg["landing_mode"],
        "my_claims": [view(t) for t in tasks if t["id"] in claims and claims[t["id"]]["owner"] == me],
        "in_progress": [view(t) for t in tasks if t["status"] == "claimed"],
        "ready_to_test": [view(t) for t in tasks if t["status"] == "review"],
        "stuck": [view(t) for t in tasks if t["status"] == "blocked"],
        "open": [view(t) for t in tasks if t["status"] == "open"],
        "done": [view(t) for t in tasks if t["status"] == "done"],
        "recent_notes": board.all_notes()[-10:],
        "members": [m["name"] for m in board.members()],
    }


def format_status(d, brief=False):
    lines = []
    if d["my_claims"]:
        for v in d["my_claims"]:
            lines.append("You hold: %s (%d min left, branch %s, paths %s)" %
                         (v["title"], v["minutes_left"], v["branch"], ", ".join(v["globs"])))
    else:
        lines.append("You hold no claim. Take a task before editing.")
    lines.append("In progress:")
    for v in d["in_progress"]:
        lines.append("  %s: %s (%s), %d min left%s" % (v["owner"], v["title"], v.get("agent") or "?",
                                                      v["minutes_left"], " STALLED?" if v.get("stalled") else ""))
    if not d["in_progress"]:
        lines.append("  nothing")
    lines.append("Ready to test: " + (", ".join("%s [%s]" % (v["title"], v["id"]) for v in d["ready_to_test"]) or "nothing"))
    lines.append("Stuck: " + (", ".join("%s (%s): %s" % (v["title"], v["owner"], v.get("blocker")) for v in d["stuck"]) or "nobody"))
    lines.append("Open tasks:")
    for v in d["open"][: (5 if brief else 50)]:
        lines.append("  %s  %s (%s) paths %s" % (v["id"], v["title"], v["kind"], ", ".join(v["globs"]) or "-"))
        if v.get("description") and not brief:
            lines.append("    " + v["description"].replace("\n", "\n    "))
    if not d["open"]:
        lines.append("  none. Plan work with: board add \"title\"")
    if not brief:
        lines.append("Recent notes:")
        for n in d["recent_notes"]:
            lines.append("  %s %s [%s] %s" % (n["ts"][11:16], n["member"], n["kind"], n["text"]))
    lines.append("Done: %d tasks landed on main" % len(d["done"]))
    return "\n".join(lines)


def cmd_status(args, repo):
    board = Board(repo, args.board_dir)
    board.sync()
    d = status_data(board, repo)
    out(args, format_status(d, brief=args.brief), d)


def check_hot(repo, globs, hot):
    hot_paths = repo.cfg["hot_paths"]
    if hot:
        return
    for g in globs:
        for h in hot_paths:
            if globs_overlap(g, h):
                raise BoardError("path `%s` covers hot path `%s`. Hot paths are edited only through a "
                                 "10 minute micro-claim: agentlane take --hot --new \"...\" --globs %s" % (g, h, h))


def cmd_take(args, repo):
    board = Board(repo, args.board_dir)
    me, agent = repo.member, repo.agent
    if not args.task and not args.new:
        from agentlane.inspect import cmd_list
        args.available, args.mine = True, False
        return cmd_list(args, repo)
    repo.git(["fetch", "-q", repo.remote, repo.main])
    if repo.git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise BoardError("your worktree has uncommitted changes. Commit them to your current claim branch first")
    ttl = int(repo.cfg["hot_ttl_minutes"] if args.hot else (args.ttl if args.ttl is not None else repo.cfg["ttl_minutes"]))

    def mutate(b):
        b.expire()
        mine = b.claims_of(me)
        if args.new:
            globs = normalize_paths([g.strip() for g in (args.globs or "").split(",") if g.strip()])
            if not globs:
                raise BoardError("a new task needs --globs, the paths it will touch (for a docs task: docs/pitch/**)")
            tid = b.next_id()
            t = {"id": tid, "title": args.new, "kind": args.kind or ("docs" if not paths_outside(globs, repo.cfg["docs_globs"]) else "code"),
                 "globs": globs, "status": "open", "owner": None, "created_by": me, "created": iso(now()),
                 "implementation": {"version": 1, "owners": []}}
            b.save_task(t)
        else:
            t = b.task(args.task)
            globs = normalize_paths([g.strip() for g in args.globs.split(",")]) if args.globs else t.get("globs", [])
            if not globs:
                raise BoardError("task %s has no paths. Pass --globs" % t["id"])
        if b.claim(t["id"]) or t["status"] not in ("open", "blocked"):
            raise BoardError("task %s is %s (owner %s), not open" % (t["id"], t["status"], t.get("owner")))
        if args.hot:
            hot_only = paths_outside(globs, repo.cfg["hot_paths"])
            if hot_only:
                raise BoardError("a hot claim may only cover hot paths; these are not: %s" % ", ".join(hot_only))
        else:
            check_hot(repo, globs, False)
        base = args.base or repo.main
        if mine:
            own_branches = [c["branch"] for c in mine]
            if len(mine) >= 2:
                raise BoardError("you already hold two claims. Finish one (agentlane done) before taking more")
            if base not in own_branches:
                raise BoardError("you already hold '%s'. Finish it (agentlane done), hand it off, or stack on it "
                                 "with --base %s" % (b.task(mine[0]["id"])["title"], own_branches[0]))
        elif base != repo.main:
            raise BoardError("--base must be one of your own claim branches; you hold none")
        for c in b.claims():
            hit = any_overlap(globs, c["globs"])
            if hit:
                other = b.task(c["id"])
                raise Conflict("Cannot claim %s. Path `%s` overlaps an active claim:\n\n"
                               "  %s - %s\n  owner: %s / %s\n  claimed path: %s (%d min left)\n\n"
                               "Choose another task or ask the owner to release or hand off the claim." %
                               (t["id"], hit[0], c["id"], other["title"], c["owner"], c.get("agent", "unknown"), hit[1],
                                int((parse_iso(c["expires"]) - now()).total_seconds() // 60)))
        for other in b.claims():
            if other["owner"] != me and other["branch"] == base:
                raise BoardError("cannot stack on another member's branch %s" % base)
        branch = "claim/%s-%s" % (t["id"], slug(t["title"]))
        created = now()
        c = {"id": t["id"], "owner": me, "agent": agent, "globs": globs, "branch": branch, "base": base,
             "created": iso(created), "last_heartbeat": iso(created), "ttl_minutes": ttl,
             "expires": iso(created + dt.timedelta(minutes=ttl)), "extensions": 0, "hot": bool(args.hot), "lease": uuid.uuid4().hex}
        b.save_claim(c)
        t["status"] = "claimed"
        t["owner"] = me
        from agentlane.review import record_owner
        record_owner(t, me)
        t["globs"] = globs
        b.save_task(t)
        b.event(me, "take", "%s took '%s' (%s) for %d min" % (me, t["title"], ", ".join(globs), ttl),
                task=t["id"], agent=agent)
        return {"task": t, "claim": c}

    res = board.txn("board: %s takes" % me, mutate)
    c, t = res["claim"], res["task"]
    repo.git(["fetch", "-q", repo.remote, repo.main])
    start = repo.remote_main() if c["base"] == repo.main else c["base"]
    try:
        exists = repo.git(["show-ref", "--verify", "--quiet", "refs/heads/" + c["branch"]], check=False).returncode == 0
        if exists:
            repo.git(["checkout", "-q", c["branch"]])
        else:
            if repo.git(["ls-remote", "--exit-code", "--heads", repo.remote, c["branch"]], check=False).returncode == 0:
                repo.git(["fetch", "-q", repo.remote, c["branch"]])
                start = "FETCH_HEAD"
            repo.git(["checkout", "-q", "-b", c["branch"], start])
    except BoardError:
        def undo(b):
            current = b.claim(c["id"])
            if current and current.get("lease") == c.get("lease"):
                task = b.task(c["id"])
                task.update(status="open", owner=None, last_branch=c["branch"])
                b.save_task(task)
                b.drop_claim(c["id"])
                b.event(me, "release", "Claim released because branch checkout failed", task=c["id"], agent=agent)
        board.txn("board: undo failed checkout", undo)
        raise BoardError("Could not switch to the claim branch; claim released. Preserve local changes, inspect git status, then retry.")
    out(args, "claimed '%s' for %d minutes. You are on branch %s (from %s). Edit only under: %s. "
        "Commit small and often; run `agentlane done` to land." %
        (t["title"], c["ttl_minutes"], c["branch"], start, ", ".join(c["globs"])), res)


def my_claim(board, repo, task_id=None):
    mine = board.claims_of(repo.member)
    if not mine:
        raise BoardError("you hold no claim")
    if task_id:
        for c in mine:
            if c["id"] == task_id:
                require_live(c, board.cfg)
                return c
        raise BoardError("you do not hold %s" % task_id)
    if len(mine) == 1:
        require_live(mine[0], board.cfg)
        return mine[0]
    cur = repo.git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    for c in mine:
        if c["branch"] == cur:
            require_live(c, board.cfg)
            return c
    raise BoardError("you hold two claims; pass --task <id>")


def cmd_heartbeat(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member

    def mutate(b):
        mine = b.claims_of(me)
        if not mine:
            return {"claims": 0, "written": 0}
        written = 0
        window = min(float(repo.cfg["heartbeat_min_seconds"]), float(repo.cfg["stall_release_minutes"]) * 60 / 3)
        for c in mine:
            age = (now() - parse_iso(c.get("last_heartbeat") or c["created"])).total_seconds()
            if age < window and not args.force:
                continue
            if claim_stale(c, b.cfg):
                raise BoardError("Claim %s expired; re-take it before continuing." % c["id"])
            c["last_heartbeat"] = iso(now())
            if args.text:
                c["last_activity"] = args.text[:200]
            b.save_claim(c)
            written += 1
        return {"claims": len(mine), "written": written}

    res = board.txn("board: heartbeat %s" % me, mutate)
    if not args.quiet:
        out(args, "heartbeat recorded for %d claim(s)" % res["claims"], res)


def cmd_extend(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member

    def mutate(b):
        c = my_claim(b, repo, args.task)
        if c.get("hot"):
            raise BoardError("Hot claims cannot be extended; finish or release this claim.")
        if c["extensions"] >= int(repo.cfg["max_extensions"]):
            raise BoardError("no extensions left on '%s'. Land what you have (agentlane done), or split the rest "
                             "into a new task" % b.task(c["id"])["title"])
        c["extensions"] += 1
        c["expires"] = iso(parse_iso(c["expires"]) + dt.timedelta(minutes=int(c["ttl_minutes"])))
        c["last_heartbeat"] = iso(now())
        b.save_claim(c)
        b.event(me, "extend", "%s extended '%s' (%d/%d)" % (me, b.task(c["id"])["title"], c["extensions"],
                                                            repo.cfg["max_extensions"]), task=c["id"], agent=repo.agent)
        return c

    c = board.txn("board: %s extends" % me, mutate)
    out(args, "extended to %s (%d of %d extensions used)" % (c["expires"], c["extensions"], repo.cfg["max_extensions"]), c)


def cmd_note(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member

    def mutate(b):
        task = args.task
        if not task:
            mine = b.claims_of(me)
            task = mine[0]["id"] if len(mine) == 1 else None
        if task:
            b.task(task)
        b.note(me, args.kind, args.text, task=task, agent=repo.agent)
        if task:
            b.save_task(b.task(task))
        return {"member": me, "kind": args.kind, "task": task}

    res = board.txn("board: note from %s" % me, mutate)
    out(args, "noted (%s)" % args.kind, res)


def cmd_stuck(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member

    def mutate(b):
        c = my_claim(b, repo, args.task)
        t = b.task(c["id"])
        t["status"] = "blocked"
        t["blocker"] = args.text
        b.save_task(t)
        b.note(me, "blocker", args.text, task=t["id"], agent=repo.agent)
        b.event(me, "stuck", "%s is stuck on '%s': %s" % (me, t["title"], args.text), task=t["id"], agent=repo.agent)
        return t

    t = board.txn("board: %s is stuck" % me, mutate)
    out(args, "flagged as stuck. Others will see it on the board. Keep the claim, or release it with board release", t)


def cmd_handoff(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member
    to = slug(args.to)
    board.sync()
    current = my_claim(board, repo, args.task)
    if repo.git(["branch", "--show-current"]).stdout.strip() != current["branch"]:
        raise BoardError("Check out your claim branch before handing it off.")
    repo.git(["push", "-q", "-u", repo.remote, "HEAD"])

    def mutate(b):
        if not any(m["name"] == to for m in b.members()):
            raise BoardError("no member named %s. Members: %s" % (to, ", ".join(m["name"] for m in b.members())))
        c = my_claim(b, repo, args.task)
        c["owner"] = to
        c["lease"] = uuid.uuid4().hex
        c["agent"] = None
        c["last_heartbeat"] = iso(now())
        b.save_claim(c)
        t = b.task(c["id"])
        t["owner"] = to
        from agentlane.review import record_owner
        record_owner(t, me)
        record_owner(t, to)
        if t["status"] == "blocked":
            t["status"] = "claimed"
        b.save_task(t)
        b.event(me, "handoff", "%s handed '%s' to %s (branch %s)" % (me, t["title"], to, c["branch"]),
                task=t["id"], agent=repo.agent)
        return c

    c = board.txn("board: handoff %s -> %s" % (me, to), mutate)
    out(args, "handed to %s. They should run: git fetch && git checkout %s" % (to, c["branch"]), c)


def cmd_release(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member

    def mutate(b):
        if args.force:
            if not args.task:
                raise BoardError("Specify the task ID to recover: agentlane release --force TASK_ID")
            c = b.claim(args.task)
            if not c:
                raise BoardError("Task has no claim to recover")
            if not claim_stale(c, b.cfg):
                raise BoardError("Cannot recover a live claim held by %s. Ask them to release or hand it off." % c["owner"])
        else:
            mine = [c for c in b.claims_of(me) if not args.task or c["id"] == args.task]
            if len(mine) != 1:
                raise BoardError("Specify one of your claims with agentlane release TASK_ID")
            c = mine[0]
        t = b.task(c["id"])
        t["status"] = "open"
        t["owner"] = None
        t["last_branch"] = c["branch"]
        b.save_task(t)
        b.drop_claim(c["id"])
        b.event(me, "release", "%s released '%s' (branch %s keeps the work)" % (me, t["title"], c["branch"]),
                task=t["id"], agent=repo.agent)
        return t

    t = board.txn("board: %s releases" % me, mutate)
    out(args, "released. Task '%s' is open again" % t["title"], t)



def classify(repo, paths):
    docs = repo.cfg["docs_globs"]
    hot = paths_matching(paths, repo.cfg["hot_paths"])
    code = paths_outside(paths, docs)
    tier = "docs" if not code else "code"
    return tier, hot


def run_gate(repo, tier, paths):
    gate_dir = os.path.join(repo.root, ".harness", "gate")
    scripts = [os.path.join(gate_dir, tier + ".sh")]
    smoke = os.path.join(gate_dir, "smoke.sh")
    if tier == "code" and os.path.exists(smoke):
        scripts.append(smoke)
    results = []
    for s in scripts:
        if not os.path.isfile(s):
            results.append({"script": os.path.relpath(s, repo.root), "exit": 1,
                            "output": "Missing gate. Restore or create it before landing."})
            return False, results
        bash = find_bash()
        if not bash:
            raise BoardError("Quality gates need Bash; install Bash (Git for Windows on Windows).")
        p = run([bash, s], cwd=repo.root, check=False, env={"BOARD_PATHS": "\n".join(paths), "AGENTLANE_PYTHON": sys.executable})
        results.append({"script": os.path.relpath(s, repo.root), "exit": p.returncode,
                        "output": (p.stdout + p.stderr)[-4000:]})
        if p.returncode != 0:
            return False, results
    return True, results


def changed_paths(repo, base, head="HEAD"):
    p = repo.git(["diff", "--no-renames", "--name-only", "-z", "%s...%s" % (base, head)], check=False)
    if p.returncode != 0:
        p = repo.git(["diff", "--no-renames", "--name-only", "-z", base, head])
    return [x for x in p.stdout.split("\0") if x]


def cmd_gate(args, repo):
    if args.all:
        paths = repo.git(["ls-files"]).stdout.split("\n")
        tier = "code"
    else:
        repo.git(["fetch", "-q", repo.remote, repo.main], check=False)
        paths = changed_paths(repo, repo.remote_main())
        tier, _ = classify(repo, paths)
    ok, results = run_gate(repo, tier, [p for p in paths if p])
    out(args, "gate %s (%s tier)\n%s" % ("passed" if ok else "FAILED", tier,
                                        "\n".join("%s exit %d\n%s" % (r["script"], r["exit"], r["output"]) for r in results)),
        {"ok": ok, "tier": tier, "results": results})
    if not ok:
        sys.exit(1)


def submit_review(args, repo, board, claim):
    board.sync()
    current = my_claim(board, repo, claim["id"])
    if current.get("lease", current["created"]) != claim.get("lease", claim["created"]):
        raise BoardError("Claim changed during the gate; inspect agentlane show before retrying.")
    repo.git(["push", "-q", "-u", repo.remote, "HEAD"])
    try:
        p = run(["gh", "pr", "create", "--fill", "--draft", "--base", repo.main,
                 "--head", claim["branch"]], cwd=repo.root, check=False)
    except OSError as e:
        raise BoardError("could not start GitHub CLI; claim kept. Install gh for PR review: %s" % e)
    url = p.stdout.strip()
    if p.returncode != 0 or not re.fullmatch(r"https://[^\s]+/pull/\d+", url):
        raise BoardError("PR creation failed; claim kept. Resolve the error and retry: %s" %
                         (p.stderr.strip() or url or "no PR URL returned"))

    def mutate(b):
        current = b.claim(claim["id"])
        if not current or current["owner"] != repo.member or current.get("lease", current["created"]) != claim.get("lease", claim["created"]):
            raise BoardError("claim changed while opening %s; inspect the board before continuing" % url)
        require_live(current, repo.cfg)
        t = b.task(claim["id"])
        t.update(status="review", last_branch=claim["branch"], pr=url)
        b.save_task(t)
        b.drop_claim(claim["id"])
        b.event(repo.member, "review", "%s opened a PR for '%s' %s" % (repo.member, t["title"], url),
                task=t["id"], agent=repo.agent)
        return t

    task = board.txn("board: %s opened PR" % repo.member, mutate)
    out(args, "PR opened at %s. Claim released; task is ready for review and testing." % url, task)


def cmd_sync_reviews(args, repo):
    board = Board(repo, args.board_dir)
    board.sync()
    updates = []
    for task in board.tasks():
        if task["status"] != "review" or not task.get("pr"):
            continue
        try:
            p = run(["gh", "pr", "view", task["pr"], "--json", "state,mergeCommit,baseRefName"],
                    cwd=repo.root, check=False)
        except OSError as e:
            raise BoardError("review sync needs GitHub CLI: %s" % e)
        if p.returncode:
            raise BoardError("could not read PR %s: %s" % (task["pr"], p.stderr.strip()))
        try:
            pr = json.loads(p.stdout)
            state = pr["state"]
        except (ValueError, KeyError, TypeError):
            raise BoardError("invalid PR response for %s" % task["pr"])
        if state == "MERGED":
            sha = (pr.get("mergeCommit") or {}).get("oid")
            if pr.get("baseRefName") != repo.main or not sha:
                raise BoardError("PR %s was not merged into %s" % (task["pr"], repo.main))
            repo.git(["fetch", "-q", repo.remote, repo.main])
            if repo.git(["merge-base", "--is-ancestor", sha, repo.remote_main()], check=False).returncode:
                raise BoardError("PR merge %s is not on %s; task kept in review" % (sha, repo.main))
            updates.append((task, "done", sha))
        elif state == "CLOSED":
            updates.append((task, "open", None))

    def mutate(b):
        changed = []
        for original, status, sha in updates:
            t = b.task(original["id"])
            if t["status"] != "review" or t.get("pr") != original["pr"]:
                continue
            t["status"] = status
            if sha:
                t["landed"] = (t.get("landed") or []) + [{"after": sha, "pr": t["pr"]}]
            else:
                t["owner"] = None
                t["last_pr"] = t.pop("pr")
            b.save_task(t)
            b.event(repo.member, "review_merged" if sha else "review_closed",
                    "'%s': PR merged" % t["title"] if sha else "'%s': PR closed; task reopened" % t["title"],
                    task=t["id"], agent=repo.agent)
            changed.append(t)
        return changed

    changed = board.txn("board: sync reviews", mutate)
    out(args, "updated %d reviewed task(s)" % len(changed), {"updated": changed})


def push_with_main_guard(repo, board_branch, board_head, before, head):
    """Require an actual main update in Git's advertised pre-push input.

    Git skips force-with-lease for up-to-date refs. Without this check an atomic
    push could publish only board after another writer has already published HEAD.
    Preserve the effective user hook, including its interpreter, arguments, input
    and failure status. The override and its files last for this push only.
    """
    original_hook = hook_path(repo)
    # Git propagates -c options to hooks through GIT_CONFIG_PARAMETERS. Restore
    # the incoming parameters for the user's hook and any Git commands it runs.
    parameters = os.environ.get("GIT_CONFIG_PARAMETERS")
    restore_parameters = ("unset GIT_CONFIG_PARAMETERS" if parameters is None else
                          "export GIT_CONFIG_PARAMETERS=" + shlex.quote(parameters))
    main_ref = "refs/heads/" + repo.main
    with tempfile.TemporaryDirectory(prefix="agentlane-push-", dir=repo.git_common) as directory:
        def quote(value):
            return shlex.quote(value.replace("\\", "/") if os.name == "nt" else value)

        hook_status = os.path.join(directory, "hook-status")
        script = """#!/bin/sh
input=%s
cat > "$input" || exit 1
original_hook=%s
if [ -x "$original_hook" ]; then
    (
        %s
        "$original_hook" "$@" < "$input"
    )
    status=$?
    if [ "$status" -ne 0 ]; then
        printf '%%s\\n' "$status" > %s
        exit "$status"
    fi
fi
found=0
while read -r local_ref local_sha remote_ref remote_sha extra; do
    if [ "$remote_ref" = %s ]; then
        if [ "$local_sha" != %s ] || [ "$remote_sha" != %s ] ||
           [ "$local_sha" = "$remote_sha" ] || [ -n "$extra" ]; then
            echo 'AgentLane refused publication: main update does not match expected base/candidate' >&2
            exit 1
        fi
        found=$((found + 1))
    fi
done < "$input"
if [ "$found" -ne 1 ]; then
    echo 'AgentLane refused publication: main is missing from push updates (possibly already at candidate); board kept' >&2
    exit 1
fi
""" % (quote(os.path.join(directory, "input")), quote(original_hook), restore_parameters, quote(hook_status),
       quote(main_ref), quote(head), quote(before))
        path = os.path.join(directory, "pre-push")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        os.chmod(path, 0o755)
        hooks_directory = directory.replace("\\", "/") if os.name == "nt" else directory
        result = repo.git(["-c", "core.hooksPath=" + hooks_directory,
                           "push", "--atomic", "-q", "--force-with-lease=%s:%s" % (main_ref, before), repo.remote,
                           head + ":" + main_ref, board_head + ":refs/heads/" + board_branch],
                          check=False, env={"BOARD_LAND": "1"})
        if os.path.isfile(hook_status):
            with open(hook_status, encoding="utf-8") as f:
                status = f.read().strip()
            raise BoardError("Existing pre-push hook rejected publication (exit %s); this push did not publish main or board.\n%s" %
                             (status, result.stdout + result.stderr))
        return result


def push_main_and_board(repo, board, message, before, head=None, claim=None, claim_cfg=None):
    """Publish both prepared commits or neither; callers replay on contention."""
    board.render()
    git(["add", "-A"], cwd=board.dir)
    git(["-c", "user.name=board", "-c", "user.email=board@agentlane.local",
         "commit", "-q", "-m", message], cwd=board.dir)
    board_head = git(["rev-parse", "HEAD"], cwd=board.dir).stdout.strip()
    current_head = repo.git(["rev-parse", "HEAD"]).stdout.strip()
    if head is not None and current_head != head:
        raise BoardError("HEAD changed after the gate; run agentlane done again")
    head = current_head
    # The lease is only an exact old-ref guard. Prove FF separately: never rewrite history.
    if repo.git(["merge-base", "--is-ancestor", before, head], check=False).returncode:
        raise BoardError("Refusing non-fast-forward main update")
    if claim is not None:
        require_live(claim, claim_cfg or repo.cfg)
    p = push_with_main_guard(repo, board.branch, board_head, before, head)
    if p.returncode == 0:
        return True
    if "does not support --atomic" in p.stderr:
        raise BoardError("Remote does not support atomic Git pushes. No changes landed; use PR mode or a remote supporting atomic pushes.")
    if not any(m in (p.stderr + p.stdout).lower() for m in RETRYABLE_PUSH_MARKERS):
        raise BoardError("Atomic push did not confirm success. Inspect remote main and board before retrying; check connectivity and permissions.\n" + p.stderr)
    return False


def cmd_done(args, repo):
    from agentlane.review import fetch_target, require_approval
    board = Board(repo, args.board_dir)
    me, agent = repo.member, repo.agent
    if repo.git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise BoardError("uncommitted changes. Commit them first (small commits are fine)")
    board.sync()
    cur = repo.git(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if not board.claims_of(me):
        for t0 in board.tasks():
            if t0.get("last_branch") == cur and t0["status"] == "open":
                raise BoardError("your claim on '%s' expired or was released, so it went back to open. Your work is "
                                 "safe on %s. Take it again (agentlane take %s), then run agentlane done." % (t0["title"], cur, t0["id"]))
    c = my_claim(board, repo, args.task)
    t = board.task(c["id"])
    if cur != c["branch"]:
        raise BoardError("you are on %s but your claim branch is %s. Check it out first" % (cur, c["branch"]))

    landed = None
    attempts = int(repo.cfg["land_retries"])
    log = []
    for attempt in range(1, attempts + 1):
        before, target_cfg = fetch_target(repo)
        if target_cfg["require_review"] and (args.pr or repo.cfg["landing_mode"] == "pr" or target_cfg["landing_mode"] == "pr"):
            raise BoardError("require_review is incompatible with PR mode/done --pr; host merges are not enforced")
        p = repo.git(["rebase", "-q", before], check=False)
        if p.returncode != 0:
            repo.git(["rebase", "--abort"], check=False)
            conflict_files = re.findall(r"CONFLICT.*?: (.*)", p.stdout + p.stderr)
            msg = ("rebase onto %s hit a conflict%s. Resolve it in your worktree (git rebase %s), then run "
                   "agentlane done again. Your claim is kept." %
                   (repo.main, (" in " + ", ".join(conflict_files)) if conflict_files else "", repo.remote_main()))
            board.txn("board: %s land conflict" % me,
                      lambda b: b.event(me, "conflict", "%s hit a rebase conflict landing '%s'" % (me, t["title"]),
                                        task=t["id"], agent=agent))
            raise BoardError(msg)
        paths = changed_paths(repo, before)
        if not paths:
            raise BoardError("nothing to land: no changes relative to %s. Claim kept; this command did not publish board completion." % repo.main)
        outside = paths_outside(paths, c["globs"])
        if not c.get("hot"):
            outside_hot = paths_matching(paths, repo.cfg["hot_paths"])
            outside = sorted(set(outside) | set(outside_hot))
        if outside:
            raise BoardError("these changed paths are outside your claim (%s): %s. Split them into another "
                             "claim, or take a --hot micro-claim for hot paths." % (", ".join(c["globs"]), ", ".join(outside)))
        tier, hot = classify(repo, paths)
        if hot and c.get("hot"):
            tier = "code"
        gated_head = repo.git(["rev-parse", "HEAD"]).stdout.strip()
        ok, results = run_gate(repo, tier, paths)
        if repo.git(["rev-parse", "HEAD"]).stdout.strip() != gated_head or repo.git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip():
            raise BoardError("The gate changed tracked files or HEAD. Review and commit the changes, then run agentlane done again.")
        log.append({"attempt": attempt, "tier": tier, "gate": ok, "results": results})
        if not ok:
            board.txn("board: %s gate failed" % me,
                      lambda b: b.event(me, "gate_failed", "%s: gate failed on '%s'" % (me, t["title"]),
                                        task=t["id"], agent=agent))
            raise BoardError("gate failed (%s tier). Fix and run agentlane done again.\n%s" %
                             (tier, "\n".join("%s exit %d\n%s" % (r["script"], r["exit"], r["output"]) for r in results)))
        if args.pr or repo.cfg["landing_mode"] == "pr":
            # PR creation is not an atomic main update. Refresh policy after the gate
            # rather than submitting under a policy superseded while tests ran.
            _, latest_cfg = fetch_target(repo)
            if latest_cfg["require_review"]:
                raise BoardError("require_review is incompatible with PR mode/done --pr; host merges are not enforced")
            submit_review(args, repo, board, c)
            return
        head = repo.git(["rev-parse", "HEAD"]).stdout.strip()
        board.sync()
        current = my_claim(board, repo, c["id"])
        if current["branch"] != c["branch"] or current["globs"] != c["globs"] or current.get("lease", current["created"]) != c.get("lease", c["created"]):
            raise BoardError("Claim changed while running the gate; inspect agentlane show before retrying.")
        landed = {"before": before, "after": head, "paths": paths, "tier": tier}
        t2 = board.task(c["id"])
        if target_cfg["require_review"]:
            approval = require_approval(board, t2, current, head, before, target_cfg)
            landed["approval"] = approval["id"]
        t2["status"] = "done"
        t2["landed"] = (t2.get("landed") or []) + [landed]
        t2["last_branch"] = c["branch"]
        board.save_task(t2)
        board.drop_claim(c["id"])
        board.event(me, "done", "%s landed '%s' on %s (%s, %d files)" %
                    (me, t2["title"], repo.main, head[:8], len(paths)), task=c["id"], agent=agent, sha=head)
        if push_main_and_board(repo, board, "board: %s landed %s" % (me, c["id"]), before,
                               head, current, target_cfg if target_cfg["require_review"] else repo.cfg):
            break
        landed = None
    if not landed:
        raise BoardError("Competing pushes prevented landing after %d attempts; claim kept. Run agentlane done again." % attempts)

    repo.git(["checkout", "-q", "--detach", repo.remote_main()], check=False)
    out(args, "landed '%s' on %s as %s. Claim released. You are on %s; take the next task." %
        (t2["title"], repo.main, landed["after"][:8], repo.remote_main()), {"task": t2, "landed": landed, "log": log})


def cmd_expire(args, repo):
    board = Board(repo, args.board_dir)

    def mutate(b):
        return [{"task": c["id"], "owner": c["owner"], "reason": r} for c, r in b.expire()]

    res = board.txn("board: expire sweep", mutate)
    out(args, "released %d claim(s)" % len(res) if res else "nothing to expire", res)


def cmd_event(args, repo):
    board = Board(repo, args.board_dir)
    me = repo.member
    board.txn("board: event %s" % args.type,
              lambda b: b.event(me, args.type, args.text or "", task=args.task, agent=repo.agent))
    if not args.quiet:
        out(args, "event recorded", {"ok": True})


def cmd_render(args, repo):
    board = Board(repo, args.board_dir)
    if args.board_dir:
        board.render()
    else:
        board.txn("board: render", lambda b: b.render())
    out(args, "BOARD.md rendered", {"ok": True})


def narrate_line(e):
    who = e.get("member", "someone")
    agent = e.get("agent")
    who_a = "%s's %s" % (who, agent) if agent else who
    t = e.get("type")
    text = e.get("text") or ""
    if t == "take":
        return "%s / %s claimed %s: %s" % (agent or "worker", who, e.get("task") or "task", text.split(" took ", 1)[-1])
    if t == "done":
        return "%s / %s completed %s: %s" % (agent or "worker", who, e.get("task") or "task", text.split(" landed ", 1)[-1])
    if t == "stuck":
        return "Needs help: %s" % text
    if t == "note":
        return "%s noted [%s]: %s" % (who, e.get("kind", "note"), text)
    if t == "expired":
        return "Claim expired: %s" % text
    if t == "joined":
        return "%s joined the team with %s" % (who, agent or "an agent")
    if t == "handoff":
        return "Handoff: %s" % text
    if t == "review":
        return "Ready to test: %s" % text
    if t in ("add", "release", "extend", "review_merged", "review_closed"):
        return text
    if t == "reverted":
        return "Reverted from main: %s" % text
    if t in ("gate_failed", "conflict"):
        return "Blocked at landing: %s" % text
    return None


def post_slack(url, text):
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except Exception:
        raise BoardError("Slack delivery failed. Check the webhook and network; the secret URL is not shown.") from None


def cmd_narrate(args, repo):
    board = Board(repo, args.board_dir)
    if not args.board_dir:
        board.sync()
    marker = read_json(board.path("narrator.json"), {"posted_through": None}) or {}
    counts = dict(marker.get("posted_counts", {}))
    pending = []
    for filename in sorted(os.listdir(board.path("events"))):
        if not filename.endswith(".jsonl"):
            continue
        records = read_jsonl(board.path("events", filename))
        previous = counts.get(filename)
        if previous is None:
            previous = sum(1 for e in records if marker.get("posted_through") and e["ts"] <= marker["posted_through"])
        pending.extend(records[previous:])
        counts[filename] = len(records)
    lines = [line for line in (narrate_line(e) for e in sorted(pending, key=lambda e: (e["ts"], e["member"]))) if line]
    url = args.slack_url or os.environ.get("SLACK_WEBHOOK_URL")
    for line in lines:
        if url:
            post_slack(url, line)
        elif not args.json:
            print(line)

    def record_cursor(b):
        current = read_json(b.path("narrator.json"), {}) or {}
        merged = current.get("posted_counts", {})
        for filename, count in counts.items():
            merged[filename] = max(merged.get(filename, 0), count)
        current["posted_counts"] = merged
        write_json(b.path("narrator.json"), current)
    if pending or marker.get("posted_counts") != counts:
        if args.board_dir:
            record_cursor(board)
        else:
            board.txn("board: narrated %d events" % len(lines), record_cursor)
    if args.json:
        print(json.dumps({"posted": len(lines), "lines": lines}))


def cmd_revert_failed(args, repo):
    """Used by the main-gate workflow: revert the range that broke main and reopen its task."""
    board = Board(repo, args.board_dir)
    before, after = args.before, args.after
    if repo.git(["status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise BoardError("Automatic recovery needs a clean CI checkout. Commit or preserve local changes first.")
    repo.git(["fetch", "-q", repo.remote, repo.main])
    if args.check_base:
        repo.git(["checkout", "-q", "--detach", before])
        base_ok, _ = run_gate(repo, "code", repo.git(["ls-files"]).stdout.split("\n"))
        if not base_ok:
            out(args, "main was already failing at %s; leaving the revert to the run that broke it" % before[:8],
                {"reverted": None, "skipped": "base already red", "base": before})
            return
    def mutate(b):
        hit = None
        for t in b.tasks():
            for l in t.get("landed") or []:
                if l.get("after") == after:
                    hit = t
        if hit:
            if hit["status"] != "done":
                raise BoardError("Task %s is no longer done; inspect its current ownership before reverting." % hit["id"])
            hit["status"] = "open"
            hit["owner"] = None
            hit["failure"] = args.reason or "gate failed on main"
            b.save_task(hit)
            b.event(hit.get("owner") or "board", "reverted",
                    "'%s' was reverted from main: %s. Branch %s still has the work; take it again to fix." %
                    (hit["title"], hit["failure"], hit.get("last_branch")), task=hit["id"])
        else:
            b.event("board", "reverted", "commits %s..%s reverted from main: %s" % (before[:8], after[:8], args.reason or "gate failed"))
        return hit

    for attempt in range(int(repo.cfg["land_retries"])):
        repo.git(["fetch", "-q", repo.remote, repo.main])
        repo.git(["checkout", "-q", "--detach", repo.remote_main()])
        expected_main = repo.git(["rev-parse", "HEAD"]).stdout.strip()
        board.sync()
        hit = mutate(board)
        p = repo.git(["-c", "user.name=board", "-c", "user.email=board@agentlane.local",
                      "revert", "--no-edit", "%s..%s" % (before, after)], check=False)
        if p.returncode != 0:
            repo.git(["revert", "--abort"], check=False)
            raise BoardError("Automatic revert failed; remote unchanged. Inspect main and resolve the conflict:\n" + p.stderr)
        if push_main_and_board(repo, board, "board: reverted %s" % after[:8], expected_main):
            break
    else:
        raise BoardError("Competing pushes prevented recovery; no recovery was published. Retry revert-failed from a fresh checkout.")
    out(args, "reverted %s..%s and reopened %s" % (before[:8], after[:8], hit["id"] if hit else "no matching task"),
        {"reverted": [before, after], "task": hit})


def cmd_check_push(args, repo):
    """git pre-push hook body. Reads `<local ref> <local sha> <remote ref> <remote sha>` lines on stdin."""
    lines = sys.stdin.read().split("\n")
    main_ref = "refs/heads/%s" % repo.main
    board_ref = "refs/heads/%s" % repo.cfg["board_branch"]
    for line in lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref == main_ref:
            if os.environ.get("BOARD_LAND") != "1":
                raise BoardError("direct pushes to %s are refused. Land through: agentlane done" % repo.main)
            continue
        if remote_ref == board_ref or local_sha == "0" * 40:
            continue
        base = remote_sha if remote_sha != "0" * 40 else repo.remote_main()
        try:
            paths = changed_paths(repo, base, local_sha)
        except BoardError:
            continue
        stat = repo.git(["diff", "--shortstat", base, local_sha], check=False).stdout
        nums = [int(x) for x in re.findall(r"(\d+) (?:insertion|deletion)", stat)]
        total = sum(nums)
        if total >= int(repo.cfg["refuse_lines"]) and not os.environ.get("BOARD_SCAFFOLD"):
            raise BoardError("push has %d changed lines; over %d. Split the claim, or set BOARD_SCAFFOLD=1 "
                             "for a genuine scaffold" % (total, repo.cfg["refuse_lines"]))
        if total >= int(repo.cfg["warn_lines"]):
            sys.stderr.write("board: %d changed lines is large for one claim; consider landing sooner\n" % total)
        board = Board(repo, args.board_dir)
        pushed_branch = remote_ref.replace("refs/heads/", "")
        try:
            board.sync()
        except BoardError:
            if pushed_branch.startswith("claim/"):
                raise
            continue
        mine = [c for c in board.claims_of(repo.member) if c["branch"] == pushed_branch]
        if not mine:
            if pushed_branch.startswith("claim/"):
                raise BoardError("No active claim owned by %s permits pushing %s. Inspect agentlane status or recover the task first." % (repo.member, pushed_branch))
            sys.stderr.write("board: pushing %s without a matching claim; the board will not track it\n" % pushed_branch)
            continue
        c = mine[0]
        require_live(c, repo.cfg)
        outside = paths_outside(paths, c["globs"])
        if not c.get("hot"):
            outside = sorted(set(outside) | set(paths_matching(paths, repo.cfg["hot_paths"])))
        if outside:
            raise BoardError("paths outside your claim (%s): %s" % (", ".join(c["globs"]), ", ".join(outside)))
    print("board: push ok")


def cmd_install(args, repo):
    from agentlane.setup import install
    return install(args, repo)


def cmd_whoami(args, repo):
    out(args, "%s (%s)" % (repo.member, repo.agent), {"member": repo.member, "agent": repo.agent})



def build_parser():
    from agentlane.inspect import cmd_list, cmd_show, cmd_sync
    p = argparse.ArgumentParser(prog="agentlane", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="AgentLane " + __version__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--board-dir", help="operate on an existing checkout of the board branch (CI use)")
    sub = p.add_subparsers(dest="cmd", required=True)
    from agentlane.worker import add_parser as add_worker_parser
    add_worker_parser(sub)

    s = sub.add_parser("doctor", help="diagnose setup without changing files or Git refs")
    s.set_defaults(fn=None)
    s = sub.add_parser("mcp", help="serve the shared agent tools over stdio")
    s.set_defaults(fn=None)
    s = sub.add_parser("list", help="list tasks, ownership and paths")
    s.add_argument("--available", action="store_true", help="open tasks without live path conflicts")
    s.add_argument("--mine", action="store_true", help="tasks owned by this worker")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show", help="show a task, its claim and durable notes")
    s.add_argument("task")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("sync", help="fetch main and board without modifying your work branch")
    s.add_argument("--reviews", action="store_true", help="also reconcile GitHub PR outcomes")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("init", help="create the board branch on the remote (once per team)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("join", help="register yourself and your agent")
    s.add_argument("--name")
    s.add_argument("--agent", help="e.g. claude-code, cursor, codex, gemini, copilot")
    s.set_defaults(fn=cmd_join)

    s = sub.add_parser("status", help="who is doing what, what is ready to test, what is stuck")
    s.add_argument("--brief", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("add", help="plan a task without claiming paths or switching branches")
    s.add_argument("title")
    s.add_argument("--description", help="context, expected outcome and acceptance criteria")
    s.add_argument("--paths", nargs="+", help="repository-relative paths (quote wildcards)")
    s.add_argument("--globs", help="optional comma-separated paths; required when taking the task")
    s.add_argument("--kind", choices=TASK_KINDS)
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("sync-reviews", help="mark merged PR tasks done and reopen closed PR tasks")
    s.set_defaults(fn=cmd_sync_reviews)

    s = sub.add_parser("take", help="claim a task (or propose a new one) and switch to its branch")
    s.add_argument("task", nargs="?")
    s.add_argument("--new", help="title of a new task")
    s.add_argument("--paths", nargs="+", help="repository-relative paths (quote wildcards)")
    s.add_argument("--globs", help="comma-separated paths the task will touch, e.g. src/auth/**,docs/auth.md")
    s.add_argument("--kind", choices=TASK_KINDS)
    s.add_argument("--hot", action="store_true", help="10 minute micro-claim on hot paths only")
    s.add_argument("--base", help="stack on one of your own claim branches")
    s.add_argument("--ttl", type=int, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_take)

    s = sub.add_parser("heartbeat", help="tell the board you are still working (hooks call this)")
    s.add_argument("--text")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_heartbeat)

    s = sub.add_parser("extend", help="extend your claim once (limited)")
    s.add_argument("task_id", nargs="?", help="task ID; defaults to your current claim")
    s.add_argument("--task", help="legacy task ID option")
    s.set_defaults(fn=cmd_extend)

    s = sub.add_parser("note", help="record a finding, decision, blocker or test result for everyone")
    s.add_argument("text", help="note text, or TASK_ID followed by note text")
    s.add_argument("message", nargs="?", help="note text when TASK_ID was provided")
    s.add_argument("--kind", default="finding", choices=["finding", "decision", "blocker", "test", "idea"])
    s.add_argument("--task", help="legacy task ID option")
    s.set_defaults(fn=cmd_note)

    s = sub.add_parser("stuck", help="flag that you are blocked")
    s.add_argument("text")
    s.add_argument("--task", help="legacy task ID option")
    s.set_defaults(fn=cmd_stuck)

    s = sub.add_parser("handoff", help="pass your claim and branch to another member")
    s.add_argument("to")
    s.add_argument("--task", help="legacy task ID option")
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("release", help="give up your claim; the branch keeps the work")
    s.add_argument("task_id", nargs="?", help="task ID; defaults to your current claim")
    s.add_argument("--task", help="legacy task ID option")
    s.add_argument("--force", action="store_true", help="recover another worker's stale claim; never steals a live claim")
    s.set_defaults(fn=cmd_release)

    from agentlane.review import cmd_approve, cmd_withdraw
    s = sub.add_parser("approve", help="independently approve an exact pushed commit/base after testing in a clean reviewer checkout")
    s.add_argument("task")
    s.add_argument("--commit", required=True, help="full candidate commit SHA; must be checked out")
    s.add_argument("--base", required=True, help="full exact remote main SHA")
    s.add_argument("--evidence", required=True, help="tests performed and results; reviewer must test this exact commit")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("withdraw", help="withdraw your structured approval, preserving its history")
    s.add_argument("task")
    s.add_argument("--approval", help="approval ID; required if you have multiple active approvals")
    s.set_defaults(fn=cmd_withdraw)

    s = sub.add_parser("done", help="rebase, gate and land; target require_review needs exact independent approval",
                       description="Rebase and gate before landing. Target main require_review requires independent approval of the exact commit/base/lease and rejects PR mode.")
    s.add_argument("task_id", nargs="?", help="task ID; defaults to your current claim")
    s.add_argument("--task", help="legacy task ID option")
    s.add_argument("--pr", action="store_true", help="open a draft PR; incompatible with target require_review")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("gate", help="run the quality gate for your current changes")
    s.add_argument("--all", action="store_true", help="run the full code gate over the whole tree")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("expire", help="release claims that ran out of time or stopped heartbeating")
    s.set_defaults(fn=cmd_expire)

    s = sub.add_parser("event", help="record a raw event (hooks)")
    s.add_argument("type")
    s.add_argument("--text")
    s.add_argument("task_id", nargs="?", help="task ID; defaults to your current claim")
    s.add_argument("--task", help="legacy task ID option")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_event)

    s = sub.add_parser("render", help="regenerate BOARD.md")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("narrate", help="post plain-language lines for new events (CI)")
    s.add_argument("--slack-url")
    s.set_defaults(fn=cmd_narrate)

    s = sub.add_parser("revert-failed", help="revert a range that broke main and reopen its task (CI)")
    s.add_argument("before")
    s.add_argument("after")
    s.add_argument("--reason")
    s.add_argument("--check-base", action="store_true",
                   help="only revert if the gate passes at `before`; otherwise main was already red")
    s.set_defaults(fn=cmd_revert_failed)

    s = sub.add_parser("check-push", help="pre-push hook body")
    s.set_defaults(fn=cmd_check_push)

    s = sub.add_parser("install", help="install the pre-push hook and record who you are")
    s.add_argument("--name")
    s.add_argument("--agent")
    s.add_argument("--project", action="store_true", help="add missing project files and gates without overwriting existing ones")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("whoami", help="show the member and agent this checkout acts as")
    s.set_defaults(fn=cmd_whoami)
    for command_parser in sub.choices.values():
        command_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "worker":
            from agentlane.worker import dispatch
            return dispatch(args)
        if args.cmd == "doctor":
            from agentlane.inspect import doctor
            return doctor(args)
        if args.cmd == "mcp":
            from agentlane.mcp import main as mcp_main
            mcp_main()
            return 0
        if getattr(args, "paths", None):
            if args.globs:
                raise BoardError("Use either --paths or --globs, not both.")
            args.globs = ",".join(normalize_paths(args.paths))
        if getattr(args, "task_id", None):
            if args.task:
                raise BoardError("Provide the task ID once, either positionally or with --task.")
            args.task = args.task_id
        if args.cmd == "note" and args.message is not None:
            if args.task:
                raise BoardError("Provide the task ID once.")
            args.task, args.text = args.text, args.message
        repo = Repo()
        with checkout_lock(repo):
            args.fn(args, repo)
        return 0
    except (BoardError, OSError, ValueError) as e:
        message = str(e)
        if isinstance(e, FileNotFoundError):
            message = "Required file or executable missing: %s. Run agentlane doctor for setup guidance." % (e.filename or "unknown")
        if args.json:
            print(json.dumps({"ok": False, "error": message, "conflict": isinstance(e, Conflict)}))
        else:
            sys.stderr.write("agentlane: %s\n" % message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
