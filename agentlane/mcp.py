#!/usr/bin/env python3
"""AgentLane MCP server (stdio, newline-delimited JSON-RPC) exposing the Git board as tools.

Every tool invokes agentlane --json, so the CLI stays the single source of behaviour.
Standard library only. Configured for each agent in the committed config files at the repo root.
"""
import json
import os
import subprocess
import sys

from agentlane import __version__
ROOT = os.environ.get("BOARD_REPO_ROOT") or os.getcwd()

TOOLS = [
    {"name": "board_doctor", "description": "Read-only setup diagnostics with fixes for missing prerequisites.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "board_list", "description": "List tasks, paths, owners and stale claims; filter available work or this worker's tasks.",
     "inputSchema": {"type": "object", "properties": {"available": {"type": "boolean"}, "mine": {"type": "boolean"}}}},
    {"name": "board_show", "description": "Inspect one task, including description, timestamps, claim, branch and durable notes.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}}},
    {"name": "board_sync", "description": "Fetch main and board without changing the work branch. Optionally reconcile GitHub review outcomes.",
     "inputSchema": {"type": "object", "properties": {"reviews": {"type": "boolean"}}}},
    {"name": "board_add",
     "description": "Plan work in the backlog without claiming paths or switching branches. Include context and acceptance criteria in description. Paths can be supplied when taking the task.",
     "inputSchema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string"}, "description": {"type": "string"},
         "globs": {"type": "array", "items": {"type": "string"}},
         "kind": {"type": "string", "enum": ["code", "docs", "test", "research", "design", "ops", "other"]}}}},
    {"name": "board_sync_reviews",
     "description": "Check GitHub PR outcomes: mark tasks done after a verified merge into the main branch, or reopen tasks whose PRs closed without merging. Requires gh authentication.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "board_status",
     "description": "Who is doing what right now, what is ready to test, who is stuck, open tasks, and recent "
                    "notes. Call this at the start of a session and before taking any task.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "board_take",
     "description": "Claim a task for this member and switch to its branch. Give task_id for an existing open task, "
                    "or title plus globs to propose and take a new one. Globs are the paths the work will touch "
                    "(e.g. src/auth/**, docs/pitch/**). Fails loudly if the paths overlap someone else's live claim; "
                    "in that case pick other paths or wait. Non-code work (research, design, test plans) is a task "
                    "too, with docs/ paths.",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"},
         "title": {"type": "string"},
         "globs": {"type": "array", "items": {"type": "string"}},
         "kind": {"type": "string", "enum": ["code", "docs", "test", "research", "design", "ops", "other"]},
         "hot": {"type": "boolean", "description": "10 minute micro-claim on shared hot paths only"},
         "base": {"type": "string", "description": "one of your own claim branches, to stack on it"}}}},
    {"name": "board_note",
     "description": "Record a finding, decision, blocker, idea or test result so nobody rediscovers it. "
                    "Write one every time you learn something another member would need.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "text": {"type": "string"},
         "kind": {"type": "string", "enum": ["finding", "decision", "blocker", "test", "idea"]},
         "task_id": {"type": "string"}}}},
    {"name": "board_stuck",
     "description": "Flag that this member is blocked on their current task and say why. Keeps the claim.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}, "task_id": {"type": "string"}}}},
    {"name": "board_done",
     "description": "Land the current claim: rebase on main, run the quality gate, push to main, release the claim. "
                    "Commit all work first. Runs the gate before either direct landing or PR submission. Target main require_review needs independent approval of exact commit/base/lease and rejects PR mode. Otherwise honors landing_mode=pr or pr=true. A PR submission is not a completed task.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "pr": {"type": "boolean"}}}},
    {"name": "board_approve",
     "description": "Approve an exact pushed candidate and remote main base after testing in a clean separate reviewer checkout. Must be registered and never an implementation owner. Engineering retains the live claim.",
     "inputSchema": {"type": "object", "required": ["task_id", "commit", "base", "evidence"], "properties": {
         "task_id": {"type": "string"}, "commit": {"type": "string"}, "base": {"type": "string"}, "evidence": {"type": "string"}}}},
    {"name": "board_withdraw",
     "description": "Withdraw your structured approval, keeping its history. Supply approval ID if more than one is active.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "string"}, "approval": {"type": "string"}}}},
    {"name": "board_handoff",
     "description": "Pass the current claim and its branch to another member by name.",
     "inputSchema": {"type": "object", "required": ["to"], "properties": {"to": {"type": "string"}, "task_id": {"type": "string"}}}},
    {"name": "board_extend",
     "description": "Extend the current claim once. Only two extensions exist; after that land or split.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "board_release",
     "description": "Give up the current claim so someone else can take the task. The branch keeps the work.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "board_join",
     "description": "Register this member and their agent on the board. Run once per person per checkout.",
     "inputSchema": {"type": "object", "required": ["name", "agent"], "properties": {"name": {"type": "string"}, "agent": {"type": "string"}}}},
]


def board(argv):
    p = subprocess.run([sys.executable, "-m", "agentlane", "--json"] + argv, cwd=ROOT, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = p.stdout.strip() or p.stderr.strip()
    return text, p.returncode != 0


def call(name, a):
    a = {} if a is None else a
    if not isinstance(a, dict):
        return json.dumps({"ok": False, "error": "Tool arguments must be an object"}), True
    definition = next((t for t in TOOLS if t["name"] == name), None)
    if definition:
        schema = definition["inputSchema"]
        if any(key not in a for key in schema.get("required", [])):
            return json.dumps({"ok": False, "error": "Required arguments: " + ", ".join(schema["required"])}), True
        types = {"string": str, "boolean": bool, "array": list}
        for key, value in a.items():
            rule = schema.get("properties", {}).get(key)
            if rule and (not isinstance(value, types[rule["type"]]) or
                         (rule["type"] == "array" and any(not isinstance(item, str) for item in value))):
                return json.dumps({"ok": False, "error": "Invalid argument: " + key}), True
    if name == "board_doctor":
        return board(["doctor"])
    if name == "board_show":
        return board(["show", a["task_id"]])
    if name == "board_list":
        return board(["list"] + (["--available"] if a.get("available") else []) + (["--mine"] if a.get("mine") else []))
    if name == "board_sync":
        return board(["sync"] + (["--reviews"] if a.get("reviews") else []))
    if name == "board_add":
        argv = ["add", a["title"]]
        for key in ("description", "kind"):
            if a.get(key):
                argv += ["--" + key, a[key]]
        if a.get("globs"):
            argv += ["--globs", ",".join(a["globs"])]
        return board(argv)
    if name == "board_sync_reviews":
        return board(["sync-reviews"])
    if name == "board_status":
        return board(["status"])
    if name == "board_take":
        argv = ["take"]
        if a.get("task_id"):
            argv.append(a["task_id"])
        if a.get("title"):
            argv += ["--new", a["title"]]
        if a.get("globs"):
            argv += ["--globs", ",".join(a["globs"])]
        if a.get("kind"):
            argv += ["--kind", a["kind"]]
        if a.get("hot"):
            argv.append("--hot")
        if a.get("base"):
            argv += ["--base", a["base"]]
        return board(argv)
    if name == "board_note":
        argv = ["note", a["text"], "--kind", a.get("kind") or "finding"]
        if a.get("task_id"):
            argv += ["--task", a["task_id"]]
        return board(argv)
    if name == "board_stuck":
        argv = ["stuck", a["text"]] + (["--task", a["task_id"]] if a.get("task_id") else [])
        return board(argv)
    if name == "board_done":
        argv = ["done"] + (["--task", a["task_id"]] if a.get("task_id") else []) + (["--pr"] if a.get("pr") else [])
        return board(argv)
    if name == "board_approve":
        return board(["approve", a["task_id"], "--commit", a["commit"], "--base", a["base"], "--evidence", a["evidence"]])
    if name == "board_withdraw":
        return board(["withdraw", a["task_id"]] + (["--approval", a["approval"]] if a.get("approval") else []))
    if name == "board_handoff":
        return board(["handoff", a["to"]] + (["--task", a["task_id"]] if a.get("task_id") else []))
    if name == "board_extend":
        return board(["extend"] + (["--task", a["task_id"]] if a.get("task_id") else []))
    if name == "board_release":
        return board(["release"] + (["--task", a["task_id"]] if a.get("task_id") else []))
    if name == "board_join":
        return board(["join", "--name", a["name"], "--agent", a["agent"]])
    return json.dumps({"ok": False, "error": "unknown tool %s" % name}), True


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if not isinstance(req, dict):
            reply(None, error={"code": -32600, "message": "Request must be an object"})
            continue
        method = req.get("method")
        msg_id = req.get("id")
        params = req.get("params", {})
        if not isinstance(params, dict):
            reply(msg_id, error={"code": -32602, "message": "Params must be an object"})
            continue
        if method == "initialize":
            reply(msg_id, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "agentlane", "version": __version__}})
        elif method == "notifications/initialized" or msg_id is None:
            continue
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            text, is_error = call(params.get("name"), params.get("arguments"))
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        else:
            reply(msg_id, error={"code": -32601, "message": "method not found: %s" % method})


if __name__ == "__main__":
    main()
