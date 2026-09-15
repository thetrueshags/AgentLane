"""Additive project installation. Existing project rules and gates always win."""
import json
import os
from pathlib import Path

from agentlane import board as core

AGENTS = {
    "claude-code": ".mcp.json", "claude": ".mcp.json", "codex": ".codex/config.toml",
    "cursor": ".cursor/mcp.json", "gemini": ".gemini/settings.json", "gemini-cli": ".gemini/settings.json",
    "copilot": ".vscode/mcp.json", "vscode": ".vscode/mcp.json", "opencode": "opencode.json",
}


def install_hook(repo):
    source = Path(repo.root, ".harness/hooks/pre-push")
    if not source.is_file():
        raise core.BoardError("Missing pre-push template. Run agentlane install --project first.")
    destination = Path(core.hook_path(repo))
    body = source.read_text(encoding="utf-8")
    if destination.exists() and destination.read_text(encoding="utf-8") != body:
        raise core.BoardError("Existing pre-push hook preserved at %s. Integrate AgentLane's hook with your existing checks before installing." % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    destination.chmod(0o755)
    return str(destination)


def install_agent(repo, agent):
    relative = AGENTS.get(agent)
    if not relative:
        return "Use the shell CLI and AGENTS.md (no tool-specific config needed)."
    path = Path(repo.root, relative)
    if path.exists():
        return "Preserved existing agent configuration: " + relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if agent == "codex":
        path.write_text('[mcp_servers.board]\ncommand = "agentlane"\nargs = ["mcp"]\n', encoding="utf-8")
    else:
        server = {"command": "agentlane", "args": ["mcp"]}
        if agent in ("copilot", "vscode"):
            config = {"servers": {"board": {"type": "stdio", **server}}}
        elif agent == "opencode":
            config = {"mcp": {"board": {"type": "local", "command": ["agentlane", "mcp"], "enabled": True}}}
        else:
            config = {"mcpServers": {"board": server}}
        core.write_json(str(path), config)
    return "Created " + relative


def install(args, repo):
    added = []
    if args.project:
        template = Path(__file__).parent / "templates"
        for filename, relative in (("code.sh", ".harness/gate/code.sh"), ("docs.sh", ".harness/gate/docs.sh"),
                                   ("pre-push", ".harness/hooks/pre-push"), ("AGENTS.md", "AGENTS.md"),
                                   ("LICENSE", ".harness/LICENSE")):
            path = Path(repo.root, relative)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8", newline="\n") as f:
                    f.write((template / filename).read_text(encoding="utf-8"))
                if filename.endswith(".sh") or filename == "pre-push":
                    path.chmod(0o755)
                added.append(relative)
        cfg = Path(repo.root, ".harness/config.json")
        if not cfg.exists():
            core.write_json(str(cfg), core.DEFAULTS)
            added.append(".harness/config.json")
        ignore = Path(repo.root, ".harness/.gitignore")
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if "member.json" not in existing.splitlines():
            ignore.write_text(existing.rstrip() + "\nmember.json\n", encoding="utf-8")
    hook = install_hook(repo)
    integration = install_agent(repo, args.agent or repo.agent)
    if args.name or args.agent:
        core.write_json(os.path.join(repo.root, ".harness/member.json"),
                        {"name": core.slug(args.name or repo.member), "agent": args.agent or repo.agent})
    core.out(args, "Installed pre-push guardrail. %s\n%s\nNext: agentlane join --name <name> --agent <tool>" %
             (integration, "Commit the added project files before taking a task." if added else "Existing project files preserved."),
             {"ok": True, "added": added, "pre_push_hook": hook, "agent_config": integration})
