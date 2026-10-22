#!/usr/bin/env bash
root="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$root" ] || exit 0
[ -f "$root/.harness/member.json" ] || exit 0
cd "$root" || exit 0
agentlane heartbeat --quiet >/dev/null 2>&1 || true
exit 0
