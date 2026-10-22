"""GitHub-bound identities for cooperative boards; repository protection remains essential."""
import re
import subprocess

from agentlane import board as core


def capture(repo, required=False):
    email = repo.git(["config", "user.email"], check=False).stdout.strip()
    login = None
    try:
        result = core.run(["gh", "api", "user", "--jq", ".login"], cwd=repo.root, check=False, timeout=10)
        value = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value):
            login = value
    except (OSError, subprocess.TimeoutExpired):
        pass
    if required and not login:
        raise core.BoardError("identity_mode=github requires an authenticated GitHub CLI: run gh auth login")
    return {"github_login": login, "git_email": email or None}


def policy(repo):
    if not hasattr(repo, "_identity_policy"):
        from agentlane.review import fetch_target
        repo._identity_policy = fetch_target(repo)[1]
    return repo._identity_policy


def verify(repo, name, cfg=None):
    cfg = cfg if cfg is not None else policy(repo)
    value = capture(repo, required=cfg["identity_mode"] == "github")
    if cfg["identity_mode"] == "github" and name != value["github_login"].lower():
        raise core.BoardError("identity_mode=github requires member name %s (authenticated gh login), not %s" %
                              (value["github_login"].lower(), name))
    return value


def require_registered(board, name):
    if not any(member["name"] == name and (member.get("github_login") or "").lower() == name
               for member in board.members()):
        raise core.BoardError("GitHub identity must be registered; run agentlane join with your gh login")


def check_reviewer(board, task, reviewer, login):
    members = {m["name"]: m for m in board.members()}
    owners = task.get("implementation", {}).get("owners", [])
    owner_logins = [members.get(owner, {}).get("github_login") for owner in owners]
    # Persisted ownership snapshots survive later re-registration of a member.
    owner_logins += list(task.get("implementation_logins", []))
    if not login or any(not members.get(owner, {}).get("github_login") for owner in owners):
        raise core.BoardError("GitHub identity missing for reviewer/implementation owner; rejoin before review")
    if login.lower() in [value.lower() for value in owner_logins if value]:
        raise core.BoardError("Reviewer GitHub login must be independent of all implementation owners")
    if (members.get(reviewer, {}).get("github_login") or "").lower() != login.lower():
        raise core.BoardError("Reviewer must rejoin with the authenticated GitHub login")
