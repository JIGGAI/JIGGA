#!/usr/bin/env bash
set -euo pipefail

# Guardrail: a PR that changes runtime code under jigga/ must also change
# tests/. Not a coverage argument — it is about the reviewer being able to see
# what the new behaviour is supposed to do.
#
# Bypass: a maintainer applies the `no-tests-ok` label (docs-only refactors,
# pure renames, revert commits).
#
# Deliberately NOT required for: docs/, examples/, schemas/, scripts/, and
# .github/ — none of them change what the runtime does.

BASE_SHA="${GITHUB_BASE_SHA:-}"
HEAD_SHA="${GITHUB_HEAD_SHA:-${GITHUB_SHA:-}}"

if [[ -z "$BASE_SHA" || -z "$HEAD_SHA" ]]; then
  echo "[require-tests] Missing BASE_SHA/HEAD_SHA; skipping."
  exit 0
fi

if [[ -n "${PR_LABELS_JSON:-}" ]] && python3 -c '
import json, os, sys
labels = [str(l.get("name", "")) for l in json.loads(os.environ["PR_LABELS_JSON"] or "[]")]
sys.exit(0 if "no-tests-ok" in labels else 1)
'; then
  echo "[require-tests] bypass via label no-tests-ok"
  exit 0
fi

CHANGED=$(git diff --name-only "$BASE_SHA" "$HEAD_SHA" || true)
if [[ -z "$CHANGED" ]]; then
  echo "[require-tests] No changed files."
  exit 0
fi

HAS_RUNTIME_CHANGE=0
HAS_TEST_CHANGE=0

while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  case "$file" in
    jigga/*.py|jigga/**/*.py) HAS_RUNTIME_CHANGE=1 ;;
  esac
  case "$file" in
    tests/*) HAS_TEST_CHANGE=1 ;;
  esac
done <<< "$CHANGED"

if [[ "$HAS_RUNTIME_CHANGE" -eq 1 && "$HAS_TEST_CHANGE" -eq 0 ]]; then
  echo "[require-tests] FAIL: jigga/ changed but tests/ did not."
  echo "[require-tests] Add or update a test under tests/ that shows the new behaviour."
  echo "[require-tests] If genuinely not applicable, apply the label: no-tests-ok"
  exit 1
fi

echo "[require-tests] OK"
