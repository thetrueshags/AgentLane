#!/usr/bin/env bash
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
if [ ! -f "$root/.harness/member.json" ]; then
  echo "AgentLane: read AGENTS.md to choose source contribution or shared-board mode. Source contributors follow CONTRIBUTING.md without board registration. Register a member only when using a shared board."
  exit 0
fi
python3 "$root/bin/board" status --brief 2>/dev/null || echo "board: not reachable right now (offline, or board not initialised). Continue, and call board_status before taking a task."
exit 0
