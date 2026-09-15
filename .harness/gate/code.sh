#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

ran=0
if [ -f package.json ]; then
  if grep -q '"typecheck"' package.json; then npm run -s typecheck; ran=1; fi
  if grep -q '"lint"' package.json; then npm run -s lint; ran=1; fi
  if grep -q '"test"' package.json; then CI=1 npm test; ran=1; fi
fi
if [ -f pyproject.toml ] || [ -f pytest.ini ] || ls tests/*.py >/dev/null 2>&1; then
  if command -v pytest >/dev/null 2>&1; then pytest -q; else python3 -m unittest discover -q -s tests; fi
  ran=1
fi
if [ -f mix.exs ]; then mix test; ran=1; fi
if [ -f go.mod ]; then go build ./... && go test ./...; ran=1; fi
if [ -f Cargo.toml ]; then cargo test -q; ran=1; fi

if [ "$ran" = "0" ]; then
  echo "code gate: no test runner detected for this project; add one to .harness/gate/code.sh"
fi
echo "code gate passed"
