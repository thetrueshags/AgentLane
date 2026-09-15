#!/usr/bin/env bash
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
if [ ! -f "$root/.harness/member.json" ]; then
  echo "board: this checkout has no member yet. Ask the human their name and which agent this is, then call board_join (or: board join --name <name> --agent claude-code)."
  exit 0
fi
python3 "$root/bin/board" status --brief 2>/dev/null || echo "board: not reachable right now (offline, or board not initialised). Continue, and call board_status before taking a task."
exit 0
