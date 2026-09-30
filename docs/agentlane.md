# AgentLane reference

See [architecture and recovery](architecture.md), [gate customization](gates.md),
[agent integrations](integrations.md), and the [three-worker example](example.md).

This URL is retained for compatibility with earlier documentation.

## Retiring obsolete backlog work

Use `agentlane retire TASK_ID --reason "No longer needed"` for open, unclaimed tasks.
Optionally link an existing replacement with `--superseded-by OTHER_ID`. Self references,
missing IDs and retired replacements are refused. Claims (even stale claims), blocked/review
states and tasks with landing history must be resolved through their normal workflow first.
Retirement does not claim paths, switch branches, run a landing gate or update main.

The task stays in the board with its reserved ID, notes and append-only retirement history:
actor, timestamp, reason and optional replacement ID. It disappears from `list --available`
and the open portion of `status`, but remains in `list`, `show TASK_ID` and the dashboard's
retired group. Restore it to open with `agentlane unretire TASK_ID --reason "Needed again"`.
Restoration preserves the earlier retirement record and adds its own audit entry.
Any board worker can retire or restore eligible work; these are ordinary serialized board
transactions, so concurrent changes are rechecked after a push race.

MCP exposes the same operations as `board_retire` (`task_id`, `reason`, optional
`superseded_by`) and `board_unretire` (`task_id`, `reason`). Upgrade all workers before using
retirement: older clients reject the new `retired` state. Existing task records without
retirement history remain valid.
