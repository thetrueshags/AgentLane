"""Read-only localhost dashboard over an existing board checkout and local worker receipts.

Nothing here mutates the board, fetches from a remote or takes the checkout lock: it reuses the
readers in board.py, inspect.py and worker.py, so it shows the board exactly as the last
AgentLane command left it. Everything it renders was written by agents and is treated as hostile
text. Standard library only, Python 3.9+.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import ipaddress
import os
from pathlib import Path
import socket
import urllib.parse

from agentlane import board as core
from agentlane import inspect as views
from agentlane import worker as workers

LOG_TAIL_BYTES = 65536
ACTIVITY_LIMIT = 300
DEFAULT_PORT = 8787
DEFAULT_REFRESH = 10
STATES = ("claimed", "blocked", "review", "open", "done")
NAV = (("/", "Overview"), ("/activity", "Activity"), ("/sessions", "Sessions"))
STYLE = """
:root{color-scheme:light dark}
body{font:14px/1.5 ui-sans-serif,Segoe UI,system-ui,sans-serif;margin:0;background:Canvas;color:CanvasText}
header{display:flex;gap:1.5rem;align-items:baseline;padding:.6rem 1rem;border-bottom:1px solid #8884}
h1{font-size:1rem;margin:0;letter-spacing:.08em;text-transform:uppercase}
nav a{margin-right:1rem;text-decoration:none;color:inherit;opacity:.65}
nav a.on{opacity:1;font-weight:600;border-bottom:2px solid currentColor}
main{padding:1rem;max-width:72rem}
h2{font-size:.95rem;margin:1.4rem 0 .4rem;text-transform:uppercase;letter-spacing:.05em;opacity:.75}
table{border-collapse:collapse;width:100%;margin-bottom:.5rem}
th,td{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #8883;vertical-align:top}
th{font-size:.75rem;text-transform:uppercase;opacity:.6}
pre{white-space:pre-wrap;word-break:break-word;background:#8881;padding:.6rem;border-radius:4px;
margin:.3rem 0;font:12px/1.45 ui-monospace,Consolas,monospace}
.badge{display:inline-block;padding:0 .4rem;border-radius:999px;font-size:.72rem;border:1px solid #8886}
.bad{background:#e5484d33;border-color:#e5484d}.ok{background:#30a46c33;border-color:#30a46c}
.warn{background:#f5a62333;border-color:#f5a623}.num{white-space:nowrap}
.entry{border-bottom:1px solid #8883;padding:.4rem 0}
.meta{font-size:.78rem;opacity:.7}.empty{opacity:.7;font-style:italic}
dl{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .8rem;margin:.3rem 0}
dt{font-size:.78rem;text-transform:uppercase;opacity:.6}
dd{margin:0;word-break:break-word}
"""
# Control characters (including the ESC that starts a terminal escape sequence) never reach a page.
_CONTROL = {c: "�" for c in list(range(9)) + list(range(11, 32)) + [127] + list(range(0x80, 0xa0))}
_CONTROL[13] = None


def escape(value):
    """Render any agent-authored value as inert text."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True).translate(_CONTROL)


def tail(path, cap=LOG_TAIL_BYTES):
    """Read only the last `cap` bytes: worker logs reach tens of megabytes."""
    size = os.path.getsize(path)
    with open(path, "rb") as stream:
        if size > cap:
            stream.seek(size - cap)
        data = stream.read(cap)
    text = data.decode("utf-8", "replace")
    if size > cap:
        text = text.split("\n", 1)[-1]
    return text, size > cap, size


def moment(value):
    try:
        return core.parse_iso(value)
    except (ValueError, TypeError, AttributeError):
        return None


def span(delta):
    seconds = int(abs(delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    return "%dm %02ds" % (minutes, seconds)


def ago(value, at=None):
    when = moment(value)
    return span((at or core.now()) - when) + " ago" if when else "unknown"


class _ReadOnlyRepo:
    """The little of Repo that Board needs to read a checkout, without Git or the checkout lock."""

    def __init__(self, cfg, git_common):
        self.cfg = cfg
        self.remote = cfg["remote"]
        self.git_common = git_common


class Reader:
    """A snapshot source: an existing board checkout plus this coordinator's worker receipts."""

    def __init__(self, board_dir=None, git_common=None, cfg=None):
        self.cfg = dict(core.DEFAULTS)
        self.cfg.update(cfg or {})
        self.git_common = git_common or os.getcwd()
        self.board = core.Board(_ReadOnlyRepo(self.cfg, self.git_common), board_dir)
        self.registry = Path(self.git_common) / "agentlane-workers"

    def overview(self):
        claims = {c["id"]: c for c in self.board.claims()}
        rows = []
        for task in self.board.tasks():
            claim = claims.get(task["id"])
            rows.append({"task": task, "claim": claim,
                         "stale": bool(claim and core.claim_stale(claim, self.cfg))})
        return rows

    def task(self, task_id):
        return views.task_view(self.board, self.board.task(task_id))

    def runs(self, task=None):
        found = []
        for path in sorted(self.registry.glob("*/receipt.json")):
            try:
                found.append(workers.status(path))
            except (core.BoardError, OSError) as error:
                found.append({"run_id": path.parent.name, "name": "-", "status": "unreadable",
                              "task": None, "command": [], "started": None, "finished": None,
                              "exit_code": None, "error": str(error)})
        found.sort(key=lambda run: run.get("started") or "", reverse=True)
        return [r for r in found if task is None or r.get("task") == task]

    def activity(self, limit=ACTIVITY_LIMIT):
        """Claims, approvals, landings and notes from every worker, newest first."""
        merged = self.board.all_events()
        seen = {(r.get("ts"), r.get("member"), r.get("text")) for r in merged}
        for note in self.board.all_notes():
            if (note.get("ts"), note.get("member"), note.get("text")) not in seen:
                merged.append(dict(note, type="note"))
        merged.sort(key=lambda r: (r.get("ts") or "", r.get("member") or ""), reverse=True)
        return merged[:limit]


def badge(text, kind=""):
    return '<span class="badge %s">%s</span>' % (escape(kind), escape(text))


def link(href, text):
    return '<a href="%s">%s</a>' % (escape(href), escape(text))


def task_link(task_id):
    return link("/task?id=" + urllib.parse.quote(str(task_id)), task_id) if task_id else "-"


def table(headers, rows, empty="Nothing here yet."):
    if not rows:
        return '<p class="empty">%s</p>' % escape(empty)
    head = "".join("<th>%s</th>" % escape(name) for name in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in row) for row in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (head, body)


def fields(pairs):
    return "<dl>%s</dl>" % "".join("<dt>%s</dt><dd>%s</dd>" % (escape(name), value) for name, value in pairs)


def page(title, body, refresh, active):
    nav = "".join('<a class="%s" href="%s">%s</a>' % ("on" if href == active else "", href, escape(label))
                  for href, label in NAV)
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">%s<title>%s</title>'
            "<style>%s</style></head><body><header><h1>AgentLane</h1><nav>%s</nav></header>"
            "<main>%s</main></body></html>" % (meta, escape(title) + " - AgentLane", STYLE, nav, body))


def claim_cell(row, cfg, at):
    """One glance at lease health: stale, expiring, or quietly running."""
    claim = row["claim"]
    if not claim:
        return "-"
    if row["stale"]:
        return badge("stale", "bad") + " lease %s" % escape(claim["lease"][:8] if claim.get("lease") else "-")
    left = moment(claim["expires"]) - at
    beat = claim.get("last_heartbeat") or claim["created"]
    silent = (at - moment(beat)).total_seconds() / 60 >= float(cfg["stall_minutes"])
    marks = [badge("expires in " + span(left), "warn" if left.total_seconds() < 600 else "ok")]
    if silent:
        marks.append(badge("no heartbeat " + ago(beat, at), "warn"))
    return " ".join(marks)


def overview_section(reader):
    at = core.now()
    rows = reader.overview()
    if not rows:
        return ('<h2>Overview</h2><p class="empty">No tasks on this board yet, or no board checkout at %s. '
                "Run an AgentLane command such as agentlane status to create and refresh it.</p>"
                % escape(reader.board.dir))
    parts = ["<h2>Overview</h2><p class=\"meta\">%s tasks in %s</p>" % (len(rows), escape(reader.board.dir))]
    for state in STATES:
        group = [r for r in rows if r["task"]["status"] == state]
        if not group:
            continue
        group.sort(key=lambda r: r["task"].get("updated") or r["task"].get("created") or "", reverse=True)
        parts.append("<h2>%s (%d)</h2>" % (escape(state), len(group)))
        parts.append(table(("ID", "Title", "Owner", "Age", "Claim", "Paths"), [(
            task_link(r["task"]["id"]),
            escape(r["task"]["title"]),
            escape(r["task"].get("owner") or "-") +
            ((" / " + escape(r["claim"].get("agent"))) if r["claim"] and r["claim"].get("agent") else ""),
            '<span class="num">%s</span>' % escape(ago(r["task"].get("updated") or r["task"].get("created"), at)),
            claim_cell(r, reader.cfg, at),
            escape(", ".join(r["task"].get("globs", [])) or "-"),
        ) for r in group]))
    return "".join(parts)


def notes_block(notes):
    if not notes:
        return '<p class="empty">No notes on this task.</p>'
    return "".join('<div class="entry"><div class="meta">%s &middot; %s &middot; %s</div><div>%s</div></div>'
                   % (escape(note.get("ts")), escape(note.get("member")), escape(note.get("kind")),
                      escape(note.get("text"))) for note in notes)


def approvals_block(task):
    approvals = task.get("approvals") or []
    if not approvals:
        return '<p class="empty">No approvals recorded.</p>'
    return "".join('<div class="entry"><div class="meta">%s %s by %s at %s</div>'
                   "<div>commit %s on base %s</div><pre>%s</pre></div>"
                   % (escape(approval.get("id")), "withdrawn" if "withdrawal" in approval else "recorded",
                      escape(approval.get("reviewer")), escape(approval.get("timestamp")),
                      escape(approval.get("commit")), escape(approval.get("base")),
                      escape(approval.get("evidence"))) for approval in approvals)


def runs_table(runs, empty, columns=()):
    """One run table; the sessions view adds the task and the literal argv it was given."""
    extra = bool(columns)
    return table(("Run", "Worker", "Status") + columns + ("Started", "Duration", "Exit"), [tuple(
        [link("/sessions?run=" + urllib.parse.quote(run["run_id"]), run["run_id"][:12]),
         escape(run.get("name")), status_badge(run)] +
        ([task_link(run.get("task")), escape(" ".join(run.get("command") or []) or run.get("error") or "-")]
         if extra else []) +
        [escape(run.get("started")), escape(duration(run)),
         escape("-" if run.get("exit_code") is None else run["exit_code"])]) for run in runs], empty)


def task_section(reader, task):
    claim = task.get("claim")
    head = [("State", escape(task["status"]) + (" " + badge("stale claim", "bad") if task["stale"] else "")),
            ("Owner", escape(task.get("owner") or "unassigned")),
            ("Kind", escape(task.get("kind") or "-")),
            ("Branch", escape(task.get("branch") or "none")),
            ("Paths", escape(", ".join(task.get("globs", [])) or "not selected")),
            ("Created", escape(task.get("created"))),
            ("Updated", escape(task.get("updated")))]
    if task.get("pr"):
        head.append(("PR", escape(task["pr"])))
    if task.get("blocker"):
        head.append(("Blocker", escape(task["blocker"])))
    if task.get("landed"):
        head.append(("Landed", "<br>".join(escape("%s -> %s (%s)" % (entry.get("before"), entry.get("after"),
                     entry.get("tier"))) for entry in task["landed"])))
    parts = ["<h2>%s</h2><p>%s</p>" % (escape(task["id"]), escape(task["title"])), fields(head),
             "<h2>Description</h2><pre>%s</pre>" % escape(task.get("description") or "No description."),
             "<h2>Claim</h2>"]
    if claim:
        parts.append(fields([("Owner", escape(claim["owner"]) + " / " + escape(claim.get("agent") or "-")),
                             ("Lease", escape(claim.get("lease") or "legacy claim")),
                             ("Branch", escape(claim["branch"]) + " on " + escape(claim.get("base") or "-")),
                             ("Created", escape(claim["created"])),
                             ("Expires", escape(claim["expires"]) + " (" + escape(ago(claim["expires"])) + ")"),
                             ("Heartbeat", escape(ago(claim.get("last_heartbeat") or claim["created"]))),
                             ("Extensions", escape("%s of %s used" % (claim.get("extensions"),
                                                                      reader.cfg["max_extensions"]))),
                             ("Hot", escape("yes" if claim.get("hot") else "no"))]))
    else:
        parts.append('<p class="empty">No live claim; the task is unowned or already landed.</p>')
    parts.append("<h2>Notes (%d)</h2>%s" % (len(task["notes"]), notes_block(task["notes"])))
    parts.append("<h2>Approvals</h2>" + approvals_block(task))
    parts.append("<h2>Worker runs</h2>" + runs_table(reader.runs(task["id"]),
                                                     "No worker runs recorded for this task."))
    return "".join(parts)


def status_badge(run):
    kind = {"exited": "ok", "running": "ok", "failed": "bad", "unreadable": "bad"}.get(run.get("status"), "warn")
    return badge(run.get("status") or "unknown", kind)


def duration(run):
    started, finished = moment(run.get("started")), moment(run.get("finished"))
    if not started:
        return "-"
    return span((finished or core.now()) - started) + ("" if finished else " so far")


def log_block(run, stream):
    path = run.get(stream + "_log")
    if not path or not os.path.isfile(path):
        return "<h2>%s</h2><p class=\"empty\">No %s log on disk.</p>" % (escape(stream), escape(stream))
    text, truncated, size = tail(path)
    note = ("last %d of %d bytes; truncated" % (LOG_TAIL_BYTES, size)) if truncated else ("%d bytes" % size)
    return '<h2>%s</h2><p class="meta">%s &middot; %s</p><pre>%s</pre>' % (
        escape(stream), escape(path), escape(note), escape(text))


def sessions_section(reader, selected):
    runs = reader.runs()
    parts = ['<h2>Worker sessions</h2><p class="meta">%d runs in %s</p>' % (len(runs), escape(str(reader.registry)))]
    parts.append(runs_table(runs, "No worker runs recorded in this coordinator checkout.",
                            ("Task", "Command")))
    chosen = [r for r in runs if r["run_id"] == selected] or [r for r in runs if r.get("status") == "running"]
    for run in chosen[:3]:
        parts.append("<h2>Output of %s (%s)</h2>" % (escape(run["run_id"]), escape(run.get("name"))))
        for stream in ("stdout", "stderr"):
            parts.append(log_block(run, stream))
    return "".join(parts)


def activity_section(reader):
    records = reader.activity()
    if not records:
        return '<h2>Activity</h2><p class="empty">No notes or events on this board yet.</p>'
    parts = ['<h2>Activity</h2><p class="meta">%d most recent entries, newest first</p>' % len(records)]
    for record in records:
        parts.append('<div class="entry"><div class="meta">%s &middot; %s %s &middot; %s</div><div>%s</div></div>' % (
            escape(record.get("ts")), badge(record.get("type") or record.get("kind") or "event"),
            escape(record.get("member")), task_link(record.get("task")) if record.get("task") else "no task",
            escape(record.get("text"))))
    return "".join(parts)


def render(reader, path, query, refresh):
    if path in ("/", "/index.html"):
        return 200, page("Overview", overview_section(reader), refresh, "/")
    if path == "/task":
        identifier = (query.get("id") or [""])[0]
        try:
            core.validate_id(identifier)
            detail = reader.task(identifier)
        except core.BoardError as error:
            return 404, page("Unknown task", "<h2>Unknown task</h2><p>%s</p>" % escape(error), 0, "/")
        return 200, page(detail["id"], task_section(reader, detail), refresh, "/")
    if path == "/sessions":
        return 200, page("Sessions", sessions_section(reader, (query.get("run") or [""])[0]), refresh, "/sessions")
    if path == "/activity":
        return 200, page("Activity", activity_section(reader), refresh, "/activity")
    return 404, page("Not found", "<h2>Not found</h2><p>Try the overview.</p>", 0, "/")


class Handler(BaseHTTPRequestHandler):
    """GET is the only method: this dashboard can never change the board."""

    server_version = "AgentLaneUI"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        try:
            status, body = render(self.server.reader, parsed.path,
                                  urllib.parse.parse_qs(parsed.query), self.server.refresh)
        except (core.BoardError, OSError, ValueError) as error:
            status, body = 500, page("Board error", "<h2>Board unreadable</h2><pre>%s</pre>" % escape(error), 0, "/")
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def loopback_family(host):
    """The bind is the only boundary; this server has no authentication."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise core.BoardError("Cannot resolve --host %s: %s" % (host, error))
    for info in infos:
        address = info[4][0].split("%")[0]
        if not ipaddress.ip_address(address).is_loopback:
            raise core.BoardError(
                "agentlane ui has no authentication and binds loopback only. --host %s resolves to %s; "
                "use 127.0.0.1, ::1 or localhost." % (host, address))
    return infos[0][0]


def make_server(reader, host, port, refresh):
    server_class = type("AgentLaneUIServer", (ThreadingHTTPServer,),
                        {"address_family": loopback_family(host), "daemon_threads": True})
    server = server_class((host, port), Handler)
    server.reader = reader
    server.refresh = refresh
    return server


def serve(args, repo):
    reader = Reader(args.board_dir, repo.git_common, repo.cfg)
    server = make_server(reader, args.host, args.port, max(0, args.refresh))
    host, port = server.server_address[0], server.server_address[1]
    print("AgentLane UI (read-only) at http://%s:%d/" % ("[%s]" % host if ":" in host else host, port))
    print("Board checkout: %s" % reader.board.dir)
    print("Worker receipts: %s" % reader.registry)
    print("Nothing here changes the board. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
