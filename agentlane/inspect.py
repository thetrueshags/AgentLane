"""Read-only task views and installation diagnostics."""
import json
import os
import shutil

from agentlane import board as core


def task_view(board, task):
    result = dict(task)
    claim = board.claim(task["id"])
    result["updated"] = task.get("updated", task.get("created"))
    result["branch"] = claim["branch"] if claim else task.get("last_branch")
    result["claim"] = claim
    result["stale"] = bool(claim and core.claim_stale(claim, board.cfg))
    result["notes"] = [n for n in board.all_notes() if n.get("task") == task["id"]]
    return result


def cmd_list(args, repo):
    board = core.Board(repo, args.board_dir)
    board.sync()
    claims = board.claims()
    tasks = board.tasks()
    if args.mine:
        tasks = [t for t in tasks if t.get("owner") == repo.member]
    if args.available:
        tasks = [t for t in tasks if t["status"] == "open" and not any(
            not core.claim_stale(c, repo.cfg) and core.any_overlap(t.get("globs", []), c["globs"])
            for c in claims)]
    result = [task_view(board, t) for t in tasks]
    lines = ["ID        STATE      OWNER        TASK"]
    for t in result:
        lines.append("%-9s %-10s %-12s %s%s" % (
            t["id"], t["status"], t.get("owner") or "-", t["title"], " [STALE]" if t["stale"] else ""))
        lines.append("  Paths: " + (", ".join(t.get("globs", [])) or "choose when claiming"))
    if not result:
        lines.append('No matching tasks. Create one with: agentlane add "title" --paths "path/**"')
    core.out(args, "\n".join(lines), {"tasks": result})


def cmd_show(args, repo):
    board = core.Board(repo, args.board_dir)
    board.sync()
    t = task_view(board, board.task(args.task))
    lines = ["%s - %s" % (t["id"], t["title"]), t.get("description") or "No description.",
             "State: %s%s" % (t["status"], " (stale claim)" if t["stale"] else ""),
             "Owner: " + (t.get("owner") or "unassigned"),
             "Paths: " + (", ".join(t.get("globs", [])) or "not selected"),
             "Branch: " + (t["branch"] or "none"),
             "Created: " + (t.get("created") or "unknown"),
             "Updated: " + (t.get("updated") or "unknown")]
    if t.get("pr"):
        lines.append("PR: " + t["pr"])
    if t.get("blocker"):
        lines.append("Blocker: " + t["blocker"])
    lines.append("Notes:")
    lines.extend("  %s %s [%s] %s" % (n["ts"], n["member"], n["kind"], n["text"]) for n in t["notes"])
    core.out(args, "\n".join(lines), t)


def cmd_sync(args, repo):
    repo.git(["fetch", "-q", repo.remote, repo.main])
    if args.reviews:
        return core.cmd_sync_reviews(args, repo)
    board = core.Board(repo, args.board_dir)
    board.sync()
    core.out(args, "Fetched %s and %s. Your work branch is unchanged; done updates it before landing." %
             (repo.main, board.branch), {"ok": True, "main": repo.main, "board": board.branch})


def doctor(args):
    checks = []

    def check(name, ok, message, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "message": message})

    git = shutil.which("git")
    check("git", git, "Git available" if git else "Install Git, then run agentlane doctor again.")
    repo = None
    if git:
        try:
            repo = core.Repo()
        except (core.BoardError, OSError) as e:
            check("repository", False, str(e))
    if repo:
        check("repository", True, repo.root)
        remote = repo.git(["remote", "get-url", repo.remote], check=False)
        check("remote", remote.returncode == 0,
              "Remote %s configured" % repo.remote if remote.returncode == 0 else
              "Configure a remote: git remote add %s <repository-url>" % repo.remote)
        if remote.returncode == 0:
            refs = repo.git(["ls-remote", "--heads", repo.remote, repo.main, repo.cfg["board_branch"]], check=False)
            check("remote_access", refs.returncode == 0,
                  "Remote reachable" if refs.returncode == 0 else "Cannot read remote; check network and Git credentials.")
            if refs.returncode == 0:
                check("main_branch", "refs/heads/" + repo.main in refs.stdout,
                      "Main branch must have an initial commit on the remote.")
                check("board_branch", "refs/heads/" + repo.cfg["board_branch"] in refs.stdout,
                      "Initialize a missing shared board once: agentlane init")
        check("identity", repo.local.get("name") and repo.local.get("agent"),
              "Local worker: %s / %s" % (repo.local.get("name", "missing"), repo.local.get("agent", "missing")) +
              ". Configure with agentlane join --name <name> --agent <tool>.")
        for key in ("user.name", "user.email"):
            check("git_" + key, repo.git(["config", key], check=False).stdout.strip(),
                  "Set a Git commit identity with git config %s <value>." % key)
        for rel in (".harness/config.json", ".harness/gate/code.sh", ".harness/gate/docs.sh", "AGENTS.md"):
            check(rel, os.path.isfile(os.path.join(repo.root, rel)), "Required project file; use agentlane install if missing.")
        bash = core.find_bash()
        check("bash", bash, "Bash available" if bash else "Install Bash; on Windows install Git for Windows.")
        gate = os.path.join(repo.root, ".harness/gate/code.sh")
        check("gate_executable", os.path.isfile(gate) and (os.name == "nt" or os.access(gate, os.X_OK)),
              "Gates run through Bash; set chmod +x .harness/gate/*.sh on Unix for direct execution.", False)
        hooks = core.hook_path(repo)
        check("pre_push_hook", os.path.isfile(hooks), "Install the local guardrail with agentlane install.")
        check("agent_config", any(os.path.isfile(os.path.join(repo.root, p)) for p in
              (".mcp.json", ".codex/config.toml", ".cursor/mcp.json", ".gemini/settings.json", ".vscode/mcp.json", "opencode.json")),
              "Use agentlane install --agent <tool> for MCP configuration; shell agents can use AGENTS.md.", False)
        check("workflows", os.path.isdir(os.path.join(repo.root, ".github/workflows")),
              "GitHub Actions are optional. Inspect workflow branches and permissions for your project.", False)
        required = repo.cfg["landing_mode"] == "pr"
        gh = shutil.which("gh")
        check("github_cli", gh, "GitHub CLI available" if gh else "Install gh for PR mode; direct mode only needs Git.", required)
        if gh and required:
            auth = core.run([gh, "auth", "status"], check=False)
            check("github_auth", auth.returncode == 0,
                  "GitHub authenticated" if auth.returncode == 0 else "Run gh auth login to authenticate in your browser.")
    ok = all(c["ok"] for c in checks if c["required"])
    lines = ["AgentLane doctor (read-only)"] + ["[%s] %s: %s" %
             ("OK" if c["ok"] else "FAIL" if c["required"] else "OPTIONAL", c["name"], c["message"]) for c in checks]
    core.out(args, "\n".join(lines), {"ok": ok, "checks": checks})
    return 0 if ok else 1
