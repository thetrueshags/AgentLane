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
STATES = ("claimed", "blocked", "review", "open", "done", "retired")
NAV = (("/", "Overview"), ("/activity", "Activity"), ("/sessions", "Sessions"))
STYLE = """
:root{color-scheme:light;--bg:#f5f7fb;--panel:#fff;--ink:#172338;--muted:#53627a;
--line:#d6dfea;--link:#2158a6;--focus:#9c3e00}
*{box-sizing:border-box}
body{font:15px/1.55 Segoe UI,system-ui,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
a{color:var(--link);text-underline-offset:.2em}a:hover{text-decoration-thickness:2px}
:focus-visible{outline:3px solid var(--focus);outline-offset:4px}
header{background:var(--panel);border-bottom:1px solid var(--line)}
.shell{max-width:78rem;margin:auto;padding:1rem 2rem;display:flex;align-items:center;gap:2rem;flex-wrap:wrap}
.brand{font-weight:750;letter-spacing:.03em}.brand small{display:block;font-size:.75rem;color:var(--muted);font-weight:400}
nav{display:flex;flex-wrap:wrap;gap:.5rem 1.2rem}nav a{text-decoration:none;padding:.3rem 0}
nav a[aria-current]{color:var(--ink);font-weight:650;border-bottom:2px solid var(--link)}
.skip{position:absolute;left:1rem;top:-8rem;padding:.5rem;background:var(--panel);z-index:1}.skip:focus{top:1rem}
main{padding:2rem;max-width:78rem;margin:auto;min-width:0;overflow-wrap:anywhere}
h1{font-size:2rem;line-height:1.2;margin:0 0 1rem;letter-spacing:-.03em}
h2{font-size:1.15rem;margin:1.8rem 0 .6rem;scroll-margin-top:1rem}
p{margin:.6rem 0}.snapshot{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--link);
padding:.8rem 1rem;border-radius:6px;color:var(--muted);font-size:.85rem;margin-bottom:1.5rem}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1rem;margin:1rem 0 1.5rem}
.summary a{display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:1rem;text-decoration:none}.summary strong{font-size:1.9rem;color:var(--ink)}.summary span{font-size:.9rem}
form{display:flex;flex-wrap:wrap;align-items:end;gap:.8rem;padding:1rem;background:var(--panel);border:1px solid var(--line);border-radius:8px}
label{display:flex;flex-direction:column;gap:.25rem;font-size:.85rem;font-weight:600;min-width:0;flex:1 1 10rem}
input,select,button{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--muted);border-radius:4px;padding:.5rem;max-width:100%}
button{background:var(--link);color:#fff;border-color:var(--link);cursor:pointer;padding:.5rem 1rem}
.table-scroll{max-width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
table{border-collapse:collapse;width:100%;min-width:52rem;table-layout:fixed}
th,td{text-align:left;padding:.7rem .8rem;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
th{font-size:.75rem;background:var(--bg);color:var(--muted)}th:first-child{width:24%}
tr:last-child td{border-bottom:0}td small{display:block;color:var(--muted);margin-top:.3rem}
summary{cursor:pointer;color:var(--link)}.paths ul{margin:.5rem 0;padding-left:1.2rem}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--panel);border:1px solid var(--line);padding:1rem;
border-radius:6px;margin:.5rem 0;font:13px/1.55 ui-monospace,Consolas,monospace}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:999px;font-size:.75rem;border:1px solid var(--line)}
.bad{background:#fce9e9;color:#8a2029;border-color:#cf969b}.ok{background:#e5f3ed;color:#16583c;border-color:#93b9a7}
.warn{background:#fff2d9;color:#734b03;border-color:#cab279}
.entry{border-bottom:1px solid var(--line);padding:.8rem 0}.meta,dt{font-size:.85rem;color:var(--muted)}
.empty{color:var(--muted);padding:1rem 0}dl{display:grid;grid-template-columns:10rem minmax(0,1fr);gap:.4rem 1rem;margin:1rem 0}
dd{margin:0;white-space:pre-wrap}.section-nav{padding:.8rem 0;border-bottom:1px solid var(--line)}
@media(max-width:600px){main{padding:1.2rem}.shell{padding:1rem 1.2rem;gap:.7rem 1.5rem}h1{font-size:1.65rem}
.summary{grid-template-columns:repeat(2,minmax(0,1fr));gap:.6rem}.summary a{padding:.8rem}dl{grid-template-columns:1fr;gap:.2rem}dd{margin-bottom:.6rem}}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#101827;--panel:#192438;--ink:#edf1f7;
--muted:#b1bed2;--line:#3e4d66;--link:#9bc2ff;--focus:#ffc58d}button{color:#172338}}
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
    if not when:
        return "unknown"
    delta = (at or core.now()) - when
    return "in " + span(delta) if delta.total_seconds() < 0 else span(delta) + " ago"


def expiry(value, at=None):
    relative = ago(value, at)
    return "expired " + relative if relative.endswith(" ago") else relative


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
                # Receipt snapshot only: status() probes another clone's exclusive launch lock.
                found.append(workers.read_receipt(path))
            except (core.BoardError, OSError) as error:
                found.append({"run_id": path.parent.name, "name": "-", "status": "unreadable",
                              "task": None, "command": [], "started": None, "finished": None,
                              "exit_code": None, "error": str(error)})
        found.sort(key=lambda run: run.get("started") or "", reverse=True)
        return [r for r in found if task is None or r.get("task") == task]

    def activity(self):
        """Claims, approvals, landings and notes from every worker, newest first."""
        merged = self.board.all_events()
        seen = {(r.get("ts"), r.get("member"), r.get("text")) for r in merged}
        for note in self.board.all_notes():
            if (note.get("ts"), note.get("member"), note.get("text")) not in seen:
                merged.append(dict(note, type="note"))
        merged.sort(key=lambda r: (r.get("ts") or "", r.get("member") or ""), reverse=True)
        return merged


def badge(text, kind=""):
    return '<span class="badge %s">%s</span>' % (escape(kind), escape(text))


def link(href, text):
    return '<a href="%s">%s</a>' % (escape(href), escape(text))


def task_link(task_id):
    return link("/task?id=" + urllib.parse.quote(str(task_id)), task_id) if task_id else "-"


def table(headers, rows, empty="Nothing here yet.", label="Records"):
    if not rows:
        return '<p class="empty">%s</p>' % escape(empty)
    head = "".join('<th scope="col">%s</th>' % escape(name) for name in headers)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % cell for cell in row) for row in rows)
    return ('<div class="table-scroll" role="region" aria-label="%s" tabindex="0">'
            '<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (escape(label), head, body))


def fields(pairs):
    return "<dl>%s</dl>" % "".join("<dt>%s</dt><dd>%s</dd>" % (escape(name), value) for name, value in pairs)


def page(title, body, refresh, active, reader=None):
    nav = "".join('<a href="%s"%s>%s</a>' % (href, ' aria-current="page"' if href == active else "", escape(label))
                  for href, label in NAV)
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">%s<title>%s</title>'
            '<style>%s</style></head><body><a class="skip" href="#main">Skip to main content</a>'
            '<header><div class="shell"><div class="brand">AgentLane<small>Local owner dashboard</small></div>'
            '<nav aria-label="Primary">%s</nav></div></header><main id="main" tabindex="-1"><h1>%s</h1>%s%s</main></body></html>'
            % (meta, escape(title) + " - AgentLane", STYLE, nav, escape(title),
               snapshot_guidance(reader) if reader else "", body))


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


def checkout_notice(reader):
    """Distinguish missing data from an empty tasks directory without invoking Git."""
    directory = Path(reader.board.dir)
    if not directory.is_dir():
        reason = "Board checkout missing: the configured path is not a directory."
    elif not (directory / "tasks").is_dir():
        reason = "No tasks directory: this path does not look like a board checkout."
    else:
        return ""
    return ('<p class="empty">%s Path: %s. Check --board-dir; it must name the board checkout. '
            'Run <code>agentlane status</code> in the project clone to create or refresh its '
            'default board checkout, then reload.</p>' % (escape(reason), escape(directory)))


def snapshot_guidance(reader):
    return ('<p class="snapshot">This page reads a local snapshot at %s. '
            'Reloading this page does not fetch remote changes. '
            'Run <code>agentlane status</code> in the project clone to refresh its default board checkout, '
            'then reload. If using --board-dir, check that it points to the refreshed checkout.</p>'
            % escape(reader.board.dir))


def approval_cue(task):
    approvals = task.get("approvals") or []
    withdrawn = sum("withdrawal" in approval for approval in approvals)
    recorded = len(approvals) - withdrawn
    parts = ["%d %s approval%s" % (number, label, "s" if number != 1 else "")
             for number, label in ((recorded, "recorded"), (withdrawn, "withdrawn")) if number]
    text = "; ".join(parts) or "No approvals recorded"
    return link("/task?" + urllib.parse.urlencode({"id": task["id"]}) + "#approvals", text)


def path_details(task):
    paths = task.get("globs") or []
    if not paths:
        return "-"
    return '<details class="paths"><summary>%d declared path%s</summary><ul>%s</ul></details>' % (
        len(paths), "s" if len(paths) != 1 else "", "".join("<li>%s</li>" % escape(path) for path in paths))


def overview_section(reader, query=None):
    notice = checkout_notice(reader)
    if notice:
        return 200, notice
    at = core.now()
    rows = reader.overview()
    values = {key: ((query or {}).get(key) or [""])[0] for key in ("q", "state", "owner", "claim")}
    parts = ['<div class="summary" aria-label="Snapshot summary">']
    summaries = [("Active claims", sum(bool(r["claim"] and not r["stale"]) for r in rows), "/?claim=active")]
    for state, label in (("blocked", "Blocked tasks"), ("review", "Review tasks"), ("open", "Open tasks")):
        summaries.append((label, sum(r["task"]["status"] == state for r in rows), "/?state=" + state))
    for label, count, href in summaries:
        parts.append('<a href="%s"><strong>%d</strong><span>%s</span></a>' % (href, count, label))
    parts.append('</div><form method="get" action="/" aria-label="Filter tasks">')
    parts.append('<label for="q">Task ID or title<input id="q" name="q" type="search" value="%s"></label>' % escape(values["q"]))
    options = [("", "All states")] + [(state, state.capitalize()) for state in STATES]
    if values["state"] and values["state"] not in STATES:
        options.append((values["state"], "Unknown: " + values["state"]))
    parts.append('<label for="state">State<select id="state" name="state">%s</select></label>' % "".join(
        '<option value="%s"%s>%s</option>' % (escape(value), ' selected' if value == values["state"] else "", escape(label))
        for value, label in options))
    parts.append('<label for="owner">Owner (exact name)<input id="owner" name="owner" value="%s"></label>' % escape(values["owner"]))
    parts.append('<label for="claim">Claim<select id="claim" name="claim"><option value="">All claims</option>'
                 '<option value="active"%s>Active claims</option>%s</select></label>' % (
                     ' selected' if values["claim"] == "active" else "",
                     '<option selected value="%s">Unknown: %s</option>' % (escape(values["claim"]), escape(values["claim"]))
                     if values["claim"] not in ("", "active") else ""))
    parts.append('<button type="submit">Apply filters</button>%s</form>' % link("/", "Clear all"))
    for key, allowed in (("state", ("",) + STATES), ("claim", ("", "active"))):
        if values[key] not in allowed:
            return 400, "".join(parts) + '<p>Unknown %s: %s. Choose a listed option or clear all.</p>' % (key, escape(values[key]))
    matching = [r for r in rows if
                (not values["q"] or values["q"].casefold() in (r["task"]["id"] + " " + r["task"]["title"]).casefold()) and
                (not values["state"] or r["task"]["status"] == values["state"]) and
                (not values["owner"] or (r["task"].get("owner") or "").casefold() == values["owner"].casefold()) and
                (not values["claim"] or r["claim"] and not r["stale"])]
    parts.append('<p class="meta">%d matching of %d tasks in this local snapshot.</p>' % (len(matching), len(rows)))
    if not rows:
        parts.append('<p class="empty">No tasks in the local board snapshot at %s.</p>' % escape(reader.board.dir))
    elif not matching:
        parts.append('<p class="empty">No tasks match these filters. Clear all to see every state.</p>')
    for state in STATES:
        group = [r for r in matching if r["task"]["status"] == state]
        if not group:
            continue
        group.sort(key=lambda r: r["task"].get("updated") or r["task"].get("created") or "", reverse=True)
        parts.append("<h2>%s (%d)</h2>" % (escape(state), len(group)))
        parts.append(table(("Task", "Owner / agent", "Updated", "Claim health", "Paths", "Approval history"), [(
            task_link(r["task"]["id"]) + " " + badge(state) + '<div>%s</div>%s' % (
                escape(r["task"]["title"]), '<small>Blocker: %s</small>' % escape(r["task"]["blocker"])
                if state == "blocked" and r["task"].get("blocker") else ""),
            escape(r["task"].get("owner") or "-") +
            ((" / " + escape(r["claim"].get("agent"))) if r["claim"] and r["claim"].get("agent") else ""),
            escape(ago(r["task"].get("updated") or r["task"].get("created"), at)),
            claim_cell(r, reader.cfg, at),
            path_details(r["task"]),
            approval_cue(r["task"]),
        ) for r in group], label=state.capitalize() + " tasks"))
    return 200, "".join(parts)


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
    parts = []
    for approval in approvals:
        withdrawal = approval.get("withdrawal")
        parts.append('<div class="entry"><p>%s %s</p>%s<pre>%s</pre>' % (
            badge("withdrawn" if withdrawal else "recorded", "warn" if withdrawal else ""),
            escape(approval.get("id")), fields([
                ("Reviewer", escape(approval.get("reviewer"))),
                ("Recorded", escape(approval.get("timestamp"))),
                ("Candidate", escape(approval.get("commit"))),
                ("Base", escape(approval.get("base")))]), escape(approval.get("evidence"))))
        if withdrawal:
            parts.append('<p class="meta">Withdrawn by %s at %s.</p>' % (
                escape(withdrawal.get("reviewer")), escape(withdrawal.get("timestamp"))))
        parts.append("</div>")
    return "".join(parts)


def runs_table(runs, empty, columns=()):
    """One run table; the sessions view adds the task and the literal argv it was given."""
    extra = bool(columns)
    return table(("Run", "Worker", "Status") + columns + ("Started", "Duration", "Exit"), [tuple(
        [link("/sessions?run=" + urllib.parse.quote(run["run_id"]), run["run_id"][:12]),
         escape(run.get("name")), status_badge(run)] +
        ([task_link(run.get("task")), escape(" ".join(run.get("command") or []) or run.get("error") or "-")]
         if extra else []) +
        [escape(run.get("started")), escape(duration(run)),
         escape("-" if run.get("exit_code") is None else run["exit_code"])]) for run in runs], empty,
                 label="Worker run records")


def task_section(reader, task):
    claim = task.get("claim")
    head = [("State", escape(task["status"]) + (" " + badge("stale claim", "bad") if task["stale"] else "")),
            ("Owner", escape(task.get("owner") or "unassigned")),
            ("Kind", escape(task.get("kind") or "-")),
            ("Branch", escape(task.get("branch") or "none")),
            ("Paths", escape(", ".join(task.get("globs", [])) or "not selected")),
            ("Created", escape(task.get("created"))),
            ("Updated", escape(task.get("updated")))]
    from agentlane.retirement import history_lines
    if task.get("retirement_history"):
        head.append(("Retirement history", "<br>".join(escape(line) for line in history_lines(task))))
    if task.get("pr"):
        head.append(("PR", escape(task["pr"])))
    if task.get("blocker"):
        head.append(("Blocker", escape(task["blocker"])))
    if task.get("landed"):
        head.append(("Landed", "<br>".join(escape("%s -> %s (%s)" % (entry.get("before"), entry.get("after"),
                     entry.get("tier"))) for entry in task["landed"])))
    sections = (("claim", "Claim"), ("notes", "Notes / evidence"), ("approvals", "Approval history"), ("runs", "Worker runs"))
    parts = ['<p>%s</p><p>%s</p><nav class="section-nav" aria-label="Task sections">%s</nav>' % (
                 link("/", "Return to overview"), escape(task["title"]),
                 "".join(link("#" + target, label) for target, label in sections)), fields(head),
             "<h2>Description</h2><pre>%s</pre>" % escape(task.get("description") or "No description."),
             '<h2 id="claim">Claim</h2>']
    if claim:
        parts.append(fields([("Owner", escape(claim["owner"]) + " / " + escape(claim.get("agent") or "-")),
                             ("Lease", escape(claim.get("lease") or "legacy claim")),
                             ("Branch", escape(claim["branch"]) + " on " + escape(claim.get("base") or "-")),
                             ("Created", escape(claim["created"])),
                             ("Expires", escape(claim["expires"]) + " (" + escape(expiry(claim["expires"])) + ")"),
                             ("Heartbeat", escape(ago(claim.get("last_heartbeat") or claim["created"]))),
                             ("Extensions", escape("%s of %s used" % (claim.get("extensions"),
                                                                      reader.cfg["max_extensions"]))),
                             ("Hot", escape("yes" if claim.get("hot") else "no"))]))
    else:
        parts.append('<p class="empty">No claim recorded in this local snapshot.</p>')
    parts.append('<h2 id="notes">Notes / evidence (%d)</h2>%s' % (len(task["notes"]), notes_block(task["notes"])))
    parts.append('<h2 id="approvals">Approval history</h2><p class="meta">These are recorded approvals; '
                 'eligibility is rechecked at landing against the candidate, base, policy and lease.</p>' + approvals_block(task))
    parts.append('<h2 id="runs">Worker runs</h2><p class="meta">Receipt status describes a process, '
                 'not task completion or gate results.</p>' + runs_table(reader.runs(task["id"]),
                                                     "No worker runs recorded for this task."))
    return "".join(parts)


def status_badge(run):
    if run.get("status") == "running":
        return badge("running (unverified)", "warn")
    kind = {"exited": "ok", "running": "ok", "failed": "bad", "unreadable": "bad"}.get(run.get("status"), "warn")
    return badge(run.get("status") or "unknown", kind)


def duration(run):
    started, finished = moment(run.get("started")), moment(run.get("finished"))
    if not started:
        return "-"
    return span((finished or core.now()) - started) + ("" if finished else " so far")


def log_block(reader, run, stream):
    """A receipt names its own log paths, so follow one only inside that run's receipt directory."""
    path = run.get(stream + "_log")
    home = os.path.realpath(str(reader.registry / run["run_id"]))
    resolved = os.path.realpath(path) if path else ""
    try:
        inside = bool(path) and os.path.normcase(
            os.path.commonpath([home, resolved])) == os.path.normcase(home)
    except ValueError:
        inside = False  # A different drive cannot share a prefix with the receipt directory.
    if not inside or not os.path.isfile(resolved):
        why = "not a file on disk" if inside else "outside this run's receipt directory " + home
        return '<h2>%s</h2><p class="empty">No %s log read: %s is %s.</p>' % (
            escape(stream), escape(stream), escape(path or "the receipt's empty log path"), escape(why))
    # Reuse the checked path; this is not protection against replacing its components concurrently.
    text, truncated, size = tail(resolved)
    note = ("last %d of %d bytes; truncated" % (LOG_TAIL_BYTES, size)) if truncated else ("%d bytes" % size)
    return '<h2>%s</h2><p class="meta">%s &middot; %s</p><pre>%s</pre>' % (
        escape(stream), escape(path), escape(note), escape(text))


def sessions_section(reader, selected):
    runs = reader.runs()
    chosen = [r for r in runs if r["run_id"] == selected] if selected else [r for r in runs if r.get("status") == "running"]
    if selected and not chosen:
        return 404, '<h2>Run not found</h2><p>No receipt for %s in this local snapshot. %s</p>' % (
            escape(selected), link("/sessions", "View all sessions"))
    parts = ['<h2>Worker sessions</h2><p class="meta">%d runs in %s</p>' % (len(runs), escape(str(reader.registry)))]
    parts.append('<p class="meta">Receipt status describes a process, not task completion or gate results. '
                 'Running receipts are unverified; they do not establish process liveness.</p>')
    parts.append(runs_table(runs, "No worker runs recorded in this coordinator checkout.",
                            ("Task", "Command")))
    for run in chosen[:3]:
        parts.append("<h2>Output of %s (%s)</h2>" % (escape(run["run_id"]), escape(run.get("name"))))
        for stream in ("stdout", "stderr"):
            parts.append(log_block(reader, run, stream))
    return 200, "".join(parts)


def activity_section(reader, number=1):
    notice = checkout_notice(reader)
    if notice:
        return 200, "<h2>Activity</h2>" + notice
    records = reader.activity()
    total = len(records)
    pages = max(1, (total + ACTIVITY_LIMIT - 1) // ACTIVITY_LIMIT)
    if number > pages:
        return 404, ('<h2>Activity page not found</h2><p>The local snapshot has %d pages. %s</p>'
                     % (pages, link("/activity", "View latest activity")))
    if not records:
        return 200, '<h2>Activity</h2><p class="empty">No notes or events on this board yet.</p>'
    start = (number - 1) * ACTIVITY_LIMIT
    end = min(start + ACTIVITY_LIMIT, total)
    links = []
    if number > 1:
        links.append(link("/activity?page=%d" % (number - 1), "Newer"))
    if number < pages:
        links.append(link("/activity?page=%d" % (number + 1), "Older"))
    navigation = '<nav aria-label="Activity pages">%s</nav>' % " ".join(links)
    parts = ['<h2>Activity</h2><p class="meta">Entries %d-%d of %d, newest first. Page %d of %d.</p>'
             % (start + 1, end, total, number, pages), navigation]
    for record in records[start:end]:
        parts.append('<div class="entry"><div class="meta">%s &middot; %s %s &middot; %s</div><div>%s</div></div>' % (
            escape(record.get("ts")), badge(record.get("type") or record.get("kind") or "event"),
            escape(record.get("member")), task_link(record.get("task")) if record.get("task") else "no task",
            escape(record.get("text"))))
    return 200, "".join(parts) + navigation


def render(reader, path, query, refresh):
    if path in ("/", "/index.html"):
        status, body = overview_section(reader, query)
        return status, page("Overview", body, refresh if status == 200 else 0, "/", reader)
    if path == "/task":
        identifier = (query.get("id") or [""])[0]
        try:
            core.validate_id(identifier)
            detail = reader.task(identifier)
        except core.BoardError as error:
            body = "<h2>Unknown task</h2><p>%s</p>" % escape(error)
            return 404, page("Unknown task", body, 0, "/", reader)
        return 200, page(detail["id"], task_section(reader, detail), refresh, "/", reader)
    if path == "/sessions":
        status, body = sessions_section(reader, (query.get("run") or [""])[0])
        return status, page("Sessions", body, refresh if status == 200 else 0, "/sessions", reader)
    if path == "/activity":
        value = (query.get("page") or ["1"])[0]
        if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
            return 400, page("Invalid page", "<h2>Invalid page</h2><p>Use a positive integer of at most "
                             "18 digits for page.</p>", 0, "/activity")
        status, body = activity_section(reader, int(value))
        return status, page("Activity", body, refresh if status == 200 else 0, "/activity", reader)
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
        parsed = ipaddress.ip_address(address)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            raise core.BoardError("IPv4-mapped IPv6 addresses are not supported by agentlane ui; "
                                  "use 127.0.0.1, ::1 or localhost.")
        if not parsed.is_loopback:
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
