#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
merge_markers_found=0
while IFS= read -r changed_path; do
  [ -z "$changed_path" ] && continue
  [ -f "$changed_path" ] || continue
  if grep -nE '(<<<<<<<|>>>>>>>|=======$)' "$changed_path" >/dev/null; then
    echo "docs gate: merge markers left in $changed_path"
    merge_markers_found=1
  fi
done <<< "${BOARD_PATHS:-}"
[ "$merge_markers_found" = "0" ] && echo "docs gate passed"
exit $merge_markers_found
