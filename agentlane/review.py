"""Structured independent review, bound to a commit, target base and live claim.

The board is a cooperative Git protocol, not an authorization boundary against
writers who directly edit its history. Missing legacy provenance is never guessed.
"""
import json
import re
import uuid

from agentlane import board as core


def full_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise core.BoardError("Review requires a full lowercase commit/base SHA")


def timestamp(value):
    try:
        if core.parse_iso(value).tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise core.BoardError("Malformed review timestamp")


def validate_task_review(task):
    if "implementation" in task:
        provenance = task["implementation"]
        if (not isinstance(provenance, dict) or set(provenance) != {"version", "owners"}
                or type(provenance["version"]) is not int or provenance["version"] != 1
                or not isinstance(provenance["owners"], list)):
            raise core.BoardError("Malformed implementation provenance")
        for owner in provenance["owners"]:
            core.validate_id(owner)
        if len(set(provenance["owners"])) != len(provenance["owners"]):
            raise core.BoardError("Duplicate implementation owner")
        if task.get("owner") and task["owner"] not in provenance["owners"]:
            raise core.BoardError("Current owner missing from implementation provenance")
    if "approvals" not in task:
        return
    if not isinstance(task["approvals"], list):
        raise core.BoardError("Malformed approvals list")
    ids = set()
    for approval in task["approvals"]:
        fields = {"id", "reviewer", "timestamp", "commit", "base", "evidence", "claim"}
        if (not isinstance(approval, dict) or not fields <= set(approval)
                or set(approval) - fields - {"withdrawal"}):
            raise core.BoardError("Malformed approval record")
        core.validate_id(approval["id"])
        core.validate_id(approval["reviewer"])
        if approval["id"] in ids:
            raise core.BoardError("Duplicate approval ID")
        ids.add(approval["id"])
        timestamp(approval["timestamp"])
        full_sha(approval["commit"])
        full_sha(approval["base"])
        if not isinstance(approval["evidence"], str) or not approval["evidence"].strip():
            raise core.BoardError("Approval evidence must describe reviewer tests and results")
        claim = approval["claim"]
        if not isinstance(claim, dict) or set(claim) != {"owner", "lease", "branch", "globs", "hot"}:
            raise core.BoardError("Malformed approval claim identity")
        validate_identity(claim)
        if "withdrawal" in approval:
            withdrawal = approval["withdrawal"]
            if (not isinstance(withdrawal, dict) or set(withdrawal) != {"reviewer", "timestamp"}
                    or withdrawal["reviewer"] != approval["reviewer"]):
                raise core.BoardError("Malformed approval withdrawal")
            timestamp(withdrawal["timestamp"])


def validate_identity(claim):
    core.validate_id(claim.get("owner"))
    if (not isinstance(claim.get("lease"), str) or not re.fullmatch(r"[0-9a-f]{32}", claim["lease"])
            or type(claim.get("hot")) is not bool
            or not isinstance(claim.get("branch"), str)
            or not re.fullmatch(r"claim/[A-Za-z0-9][A-Za-z0-9_-]*", claim["branch"])
            or not isinstance(claim.get("globs"), list) or not claim["globs"]
            or core.normalize_paths(claim["globs"]) != claim["globs"]):
        raise core.BoardError("Malformed review claim identity; a live modern lease is required")


def identity(claim):
    validate_identity(claim)
    return {key: claim[key] for key in ("owner", "lease", "branch", "globs", "hot")}


def record_owner(task, owner):
    # Never upgrade an old task by assuming its current owner was its only author.
    if "implementation" in task and owner not in task["implementation"]["owners"]:
        task["implementation"]["owners"].append(owner)


def fetch_target(repo):
    repo.git(["fetch", "-q", repo.remote, "refs/heads/" + repo.main])
    base = repo.git(["rev-parse", "FETCH_HEAD"]).stdout.strip()
    cfg = dict(core.DEFAULTS)
    path = ".harness/config.json"
    if repo.git(["ls-tree", "--name-only", base, "--", path]).stdout.strip():
        try:
            data = json.loads(repo.git(["show", base + ":" + path]).stdout)
        except ValueError:
            raise core.BoardError("Malformed target main configuration")
        if not isinstance(data, dict):
            raise core.BoardError("Target main configuration must contain a JSON object")
        cfg.update(data)
    core.validate_config(cfg)
    if cfg["landing_mode"] not in ("direct", "pr"):
        raise core.BoardError("Target landing_mode must be 'direct' or 'pr'")
    return base, cfg


def independent(board, task, claim, reviewer):
    validate_task_review(task)
    owners = task.get("implementation", {}).get("owners")
    if not owners or claim["owner"] not in owners:
        raise core.BoardError("Reliable implementation provenance is missing; legacy tasks cannot satisfy independent review")
    if reviewer == claim["owner"] or reviewer in owners:
        raise core.BoardError("Reviewer must be independent of current and prior implementation owners")
    if not any(m["name"] == reviewer for m in board.members()):
        raise core.BoardError("Reviewer must be a registered member")


def check_candidate(repo, claim, commit, base, cfg):
    core.require_live(claim, cfg)
    identity(claim)
    if repo.git(["merge-base", "--is-ancestor", base, commit], check=False).returncode:
        raise core.BoardError("Exact current main base must be an ancestor of the review candidate; rebase and test again")
    paths = core.changed_paths(repo, base, commit)
    outside = core.paths_outside(paths, claim["globs"])
    if not claim["hot"]:
        outside += core.paths_matching(paths, cfg["hot_paths"])
    if not paths or outside:
        raise core.BoardError("Review candidate has no changes or changes outside the claim paths")


def require_approval(board, task, claim, commit, base, cfg):
    check_candidate(board.repo, claim, commit, base, cfg)
    for approval in task.get("approvals", []):
        if ("withdrawal" not in approval and approval["commit"] == commit and approval["base"] == base
                and approval["claim"] == identity(claim)):
            independent(board, task, claim, approval["reviewer"])
            return approval
    # Explain legacy failures even when there is no approval yet.
    if not task.get("implementation", {}).get("owners"):
        raise core.BoardError("Reliable implementation provenance is missing; legacy tasks cannot satisfy independent review")
    raise core.BoardError("Independent approval required for this exact post-rebase/gated HEAD, main base and live lease. "
                          "Push the candidate; have an independent registered reviewer test and approve it while you retain the claim.")


def cmd_approve(args, repo):
    full_sha(args.commit)
    full_sha(args.base)
    if not args.evidence.strip():
        raise core.BoardError("Approval evidence must describe reviewer tests and results")
    board = core.Board(repo, args.board_dir)
    original = None

    def mutate(b):
        nonlocal original
        if repo.git(["status", "--porcelain"]).stdout.strip():
            raise core.BoardError("Review requires a clean checkout, including untracked files")
        if repo.git(["rev-parse", "HEAD"]).stdout.strip() != args.commit:
            raise core.BoardError("Check out the exact candidate commit in your separate reviewer clone/worktree and test it first")
        base, cfg = fetch_target(repo)
        if base != args.base:
            raise core.BoardError("Review base differs from exact remote main; fetch and test again")
        task = b.task(args.task)
        claim = b.claim(args.task)
        if not claim:
            raise core.BoardError("Approval requires a live implementation claim")
        independent(b, task, claim, repo.member)
        check_candidate(repo, claim, args.commit, base, cfg)
        if original is not None and original != identity(claim):
            raise core.BoardError("Claim changed during approval; test the new candidate/lease again")
        original = identity(claim)
        repo.git(["fetch", "-q", repo.remote, "refs/heads/" + claim["branch"]])
        if repo.git(["rev-parse", "FETCH_HEAD"]).stdout.strip() != args.commit:
            raise core.BoardError("Approval commit must equal the pushed claim branch candidate")
        approval = {"id": uuid.uuid4().hex, "reviewer": repo.member, "timestamp": core.iso(core.now()),
                    "commit": args.commit, "base": base, "evidence": args.evidence.strip(), "claim": identity(claim)}
        task.setdefault("approvals", []).append(approval)
        b.save_task(task)
        b.event(repo.member, "approve", "Approved %s against %s: %s" % (args.commit, base, args.evidence),
                task=task["id"], agent=repo.agent, approval=approval["id"])
        return approval

    approval = board.txn("board: %s approves %s" % (repo.member, args.task), mutate)
    core.out(args, "Approval %s recorded for exact commit/base and lease" % approval["id"], approval)


def cmd_withdraw(args, repo):
    board = core.Board(repo, args.board_dir)

    def mutate(b):
        task = b.task(args.task)
        choices = [a for a in task.get("approvals", []) if a["reviewer"] == repo.member
                   and "withdrawal" not in a and (not args.approval or a["id"] == args.approval)]
        if len(choices) != 1:
            raise core.BoardError("Specify --approval ID for exactly one of your active approvals")
        approval = choices[0]
        approval["withdrawal"] = {"reviewer": repo.member, "timestamp": core.iso(core.now())}
        b.save_task(task)
        b.event(repo.member, "withdraw", "Withdrew approval " + approval["id"], task=task["id"],
                agent=repo.agent, approval=approval["id"])
        return approval

    approval = board.txn("board: %s withdraws approval" % repo.member, mutate)
    core.out(args, "Withdrew approval " + approval["id"], approval)
