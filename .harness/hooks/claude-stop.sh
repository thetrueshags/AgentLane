#!/usr/bin/env bash
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
python3 "$root/bin/board" heartbeat --quiet >/dev/null 2>&1 || true
exit 0
