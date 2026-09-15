#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

ran=0
py="${AGENTLANE_PYTHON:-python3}"
if [ -f package.json ]; then
  runner=npm
  if [ -f pnpm-lock.yaml ]; then runner=pnpm; elif [ -f yarn.lock ]; then runner=yarn; fi
  scripts=$("$py" -c 'import json; s=json.load(open("package.json")).get("scripts", {}); print(" ".join(k for k in ("typecheck", "lint", "test") if k in s))')
  for script in $scripts; do CI=1 "$runner" run "$script"; ran=1; done
fi
if [ -f pyproject.toml ] || [ -f pytest.ini ] || ls tests/*.py >/dev/null 2>&1; then
  if [ -f pytest.ini ] || { [ -f pyproject.toml ] && grep -q 'tool.pytest' pyproject.toml; }; then
    "$py" -m pytest -q
  elif [ -d tests ]; then
    "$py" -m unittest discover -q -s tests
  else
    echo "Python project has no tests/ directory; customize .harness/gate/code.sh" >&2
    exit 1
  fi
  ran=1
fi
if [ -f mix.exs ]; then mix test; ran=1; fi
if [ -f go.mod ]; then go build ./... && go test ./...; ran=1; fi
if [ -f Cargo.toml ]; then cargo test -q; ran=1; fi
if [ -f pom.xml ]; then
  if [ -f mvnw ]; then bash ./mvnw test; else mvn test; fi
  ran=1
elif [ -f build.gradle ] || [ -f build.gradle.kts ]; then
  if [ -f gradlew ]; then bash ./gradlew test; else gradle test; fi
  ran=1
fi

if [ "$ran" = "0" ]; then
  echo "code gate: no test runner detected for this project; add one to .harness/gate/code.sh"
fi
echo "code gate passed"
