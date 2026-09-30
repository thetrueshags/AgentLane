"""Reversible backlog retirement; never claims paths or lands code."""
from agentlane import board as core


def validate_history(task):
    history = task.get("retirement_history", [])
    if not isinstance(history, list):
        raise core.BoardError("Invalid retirement history")
    previous = None
    for entry in history:
        if not isinstance(entry, dict) or entry.get("action") != ("unretire" if previous == "retire" else "retire"):
            raise core.BoardError("Invalid retirement history sequence")
        for key in ("actor", "reason", "timestamp"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise core.BoardError("Invalid retirement " + key)
        core.validate_id(entry["actor"])
        try:
            if core.parse_iso(entry["timestamp"]).tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise core.BoardError("Invalid retirement timestamp")
        if "superseded_by" in entry:
            core.validate_id(entry["superseded_by"])
            if entry["action"] != "retire" or entry["superseded_by"] == task["id"]:
                raise core.BoardError("Invalid retirement replacement")
        previous = entry["action"]
    if (task["status"] == "retired") != (previous == "retire"):
        raise core.BoardError("Task state disagrees with retirement history")
    if task["status"] == "retired" and (task.get("owner") or task.get("landed")):
        raise core.BoardError("Retired task cannot have an owner or landing")


def cmd_retirement(args, repo):
    reason = args.reason.strip()
    if not reason:
        raise core.BoardError("A non-empty reason is required")
    core.validate_id(args.task)
    replacement = getattr(args, "superseded_by", None)
    if replacement is not None:
        core.validate_id(replacement)
    actor = repo.member
    board = core.Board(repo, args.board_dir)

    def mutate(b):
        task = b.task(args.task)
        required = "open" if args.cmd == "retire" else "retired"
        if task["status"] != required or b.claim(args.task) or task.get("owner") or task.get("landed"):
            raise core.BoardError("%s requires an unclaimed %s task with no landing history" % (args.cmd, required))
        if replacement is not None:
            if replacement == args.task:
                raise core.BoardError("A task cannot supersede itself")
            target = b.task(replacement)
            if target["status"] == "retired":
                raise core.BoardError("Replacement task is retired")
        entry = {"action": args.cmd, "actor": actor, "timestamp": core.iso(core.now()), "reason": reason}
        if replacement is not None:
            entry["superseded_by"] = replacement
        task.setdefault("retirement_history", []).append(entry)
        task["status"] = "retired" if args.cmd == "retire" else "open"
        b.save_task(task)
        text = "%s %s %s: %s" % (actor, args.cmd, args.task, reason)
        if replacement is not None:
            text += " (superseded by %s)" % replacement
        b.event(actor, args.cmd, text, task=args.task, agent=repo.agent)
        return task

    task = board.txn("board: %s %s %s" % (actor, args.cmd, args.task), mutate)
    core.out(args, "%s is %s. %s" % (args.task, task["status"], reason), task)


def history_lines(task):
    return ["%s by %s at %s: %s%s" % (e["action"], e["actor"], e["timestamp"], e["reason"],
            " (superseded by %s)" % e["superseded_by"] if e.get("superseded_by") else "")
            for e in task.get("retirement_history", [])]


def add_parsers(sub):
    for command in ("retire", "unretire"):
        parser = sub.add_parser(command, help="%s a backlog task without landing code; preserve history" % command)
        parser.add_argument("task")
        parser.add_argument("--reason", required=True)
        if command == "retire":
            parser.add_argument("--superseded-by", help="existing replacement task ID")
        parser.set_defaults(fn=cmd_retirement)
