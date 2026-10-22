"""Read-only evidence bundles from committed board history."""
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from agentlane import board as core


CLASSES = ("tasks", "claims", "events", "notes", "members", "approvals", "landings", "gates", "recoveries")


def boundary(value):
    if value is None:
        return None
    try:
        parsed = core.parse_iso(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise core.BoardError("Export bounds must be ISO-8601 timestamps with timezone")


@contextlib.contextmanager
def blob_reader(directory):
    """Read byte-sized Git objects through one process, including multibyte JSON."""
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=directory, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors) as process:
        def read(oid):
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[0].decode("ascii") != oid or header[1] != b"blob":
                raise core.BoardError("Could not read committed board blob " + oid)
            size = int(header[2])
            content = process.stdout.read(size)
            if len(content) != size or process.stdout.read(1) != b"\n":
                raise core.BoardError("Incomplete committed board blob " + oid)
            return content

        try:
            yield read
            process.stdin.close()
            if process.wait():
                errors.seek(0)
                raise core.BoardError("Git evidence reader failed: " + errors.read().decode("utf-8", "replace"))
        finally:
            process.stdin.close()
            process.stdout.close()


def historical_records(directory, commits):
    """Visit changed blobs once and parse only appended portions of JSONL logs."""
    if not commits:
        return
    seen = set()
    prefixes = {}
    previous = None
    with blob_reader(directory) as read:
        for sha in commits:
            changes = []
            if previous is None:
                tree = core.git(["ls-tree", "-r", "-z", sha], cwd=directory).stdout
                for entry in tree.split("\0"):
                    if entry:
                        metadata, name = entry.split("\t", 1)
                        changes.append((name, metadata.split()[2]))
            else:
                diff = core.git(["diff-tree", "-r", "--no-commit-id", "--no-renames", "--no-abbrev", "--raw", "-z",
                                 previous, sha, "--"], cwd=directory).stdout.split("\0")
                for index in range(0, len(diff) - 1, 2):
                    fields = diff[index].split()
                    if fields[4] != "D":
                        changes.append((diff[index + 1], fields[3]))
            previous = sha
            for name, oid in changes:
                kind = name.split("/", 1)[0]
                if kind not in ("tasks", "claims", "events", "notes", "members") or not name.endswith((".json", ".jsonl")):
                    continue
                key = (kind, oid)
                if key in seen:
                    continue
                seen.add(key)
                content = read(oid)
                if name.endswith(".jsonl"):
                    prefix = prefixes.get(name, b"")
                    fragment = content[len(prefix):] if prefix.endswith(b"\n") and content.startswith(prefix) else content
                    prefixes[name] = content
                    for line in fragment.splitlines():
                        if line.strip():
                            yield kind, json.loads(line), sha
                else:
                    yield kind, json.loads(content), sha


def commit_window(directory, head, since, until):
    """Select by clock time, but order and choose context by commit ancestry."""
    history = core.git(["log", "--topo-order", "--reverse", "--format=%H %cI %P", head],
                       cwd=directory).stdout.splitlines()
    entries = []
    parents = {}
    for line in history:
        sha, timestamp, *ancestors = line.split()
        entries.append((sha, core.parse_iso(timestamp)))
        parents[sha] = ancestors
    selected = [sha for sha, at in entries if (since is None or at >= since) and (until is None or at <= until)]
    context = None
    if since:
        # A descendant with a skewed clock must not supply context for an earlier
        # selected commit. In merged histories, use an ancestor of the first one.
        pending = [selected[0] if selected else head]
        ancestors = set()
        while pending:
            sha = pending.pop()
            if sha not in ancestors:
                ancestors.add(sha)
                pending.extend(parents[sha])
        for sha, at in entries:
            if sha in ancestors and at < since and (until is None or at <= until):
                context = sha
    return ([context] if context else []) + selected, context


def cmd_export(args, repo):
    since, until = boundary(args.since), boundary(args.until)
    if since and until and since > until:
        raise core.BoardError("Export --since must not be after --until")
    board = core.Board(repo, args.board_dir)
    board.sync()
    head = core.git(["rev-parse", "HEAD"], cwd=board.dir).stdout.strip()
    commits, context = commit_window(board.dir, head, since, until)
    records = {kind: [] for kind in CLASSES}
    seen = {kind: set() for kind in CLASSES}

    def add(kind, record, sha):
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if encoded not in seen[kind]:
            seen[kind].add(encoded)
            records[kind].append({"board_commit": sha, "context": sha == context, "record": record})

    for kind, record, sha in historical_records(board.dir, commits):
        add(kind, record, sha)
        if kind == "tasks":
            for key, target in (("approvals", "approvals"), ("landed", "landings"), ("recoveries", "recoveries")):
                for entry in record.get(key, []):
                    add(target, dict(entry, task=record["id"]), sha)
            for entry in record.get("landed", []) + record.get("submissions", []):
                if "results" in entry:
                    add("gates", dict(entry, task=record["id"]), sha)
    destination = Path(args.out).resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise core.BoardError("Export destination must be new or empty; existing files are preserved")
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for kind in CLASSES:
        content = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in records[kind])
        files[kind + ".jsonl"] = content.encode("utf-8")
    summary = ["# AgentLane evidence export", "", "Generated: " + core.iso(core.now()),
               "Board head: " + head, "", "This bundle contains committed board records and their source commits.",
               "Rows marked context preserve a snapshot before the requested start, ancestral to the first selected commit.",
               "Historical versions are retained; repeated IDs describe changes, not additional tasks.", "",
               "Record counts:", ""]
    summary += ["- %s: %d" % (kind, len(records[kind])) for kind in CLASSES]
    summary += ["", "Landings (historical versions may repeat):", ""]
    for row in records["landings"]:
        item = row["record"]
        summary.append("- %s: %s using %s landed %s at %s; gate %s; approval %s; PR %s" %
                       (item["task"], item.get("member", "unknown (legacy record)"), item.get("agent", "unknown agent"), item.get("after", "unknown"),
                        item.get("timestamp", "unknown"), item.get("tier", "unknown"),
                        item.get("approval", "none recorded"), item.get("pr", "none recorded")))
    summary += ["", "Approvals:", ""]
    for row in records["approvals"]:
        item = row["record"]
        summary.append("- %s: %s reviewed %s at %s; %s%s" %
                       (item["task"], item["reviewer"], item["commit"], item["timestamp"], item["evidence"],
                        " (withdrawn)" if "withdrawal" in item else ""))
    summary += ["", "Gate output is bounded to 4,000 characters per script by AgentLane.",
                "Legacy records may lack identity, timestamps or gate output; missing evidence is not inferred.",
                "The manifest hashes every data file and this summary, excluding the manifest itself.", ""]
    files["SUMMARY.md"] = "\n".join(summary).encode("utf-8")
    bundle = hashlib.sha256()
    hashes = {}
    for name, content in sorted(files.items()):
        (destination / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
        bundle.update(name.encode("utf-8") + b"\0" + content)
    manifest = {"version": 1, "board_branch": board.branch, "board_head": head,
                "since": args.since, "until": args.until, "context_commit": context,
                "commit_range": {"first": commits[0] if commits else None, "last": commits[-1] if commits else None},
                "commits": commits, "files": hashes, "sha256": bundle.hexdigest(),
                "hash_algorithm": "SHA-256 over sorted UTF-8 filename + NUL + exact file bytes; manifest excluded"}
    core.write_json(str(destination / "manifest.json"), manifest)
    core.out(args, "Exported evidence to %s (%d board commits)" % (destination, len(commits)),
             {"out": str(destination), "manifest": manifest})
