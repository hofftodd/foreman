#!/usr/bin/env bash
# End-to-end: start the whole stack against a throwaway data dir and a local bare
# repo standing in for GitHub, then drive the dashboard with Playwright.
#
#   scripts/e2e.sh                  # docker runtime (needs foreman/executor-base)
#   FOREMAN_RUNTIME=local scripts/e2e.sh
#   E2E_TMPDIR="$PWD/data" scripts/e2e.sh   # Lima VM: work inside the mounted data dir
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "${E2E_TMPDIR:-${TMPDIR:-/tmp}}"
work="$(mktemp -d "${E2E_TMPDIR:-${TMPDIR:-/tmp}}/foreman-e2e.XXXXXX")"
port="${FOREMAN_PORT:-8711}"
llm_port="${FAKE_LLM_PORT:-8712}"
cleanup() {
  kill "${pid:-0}" "${llm_pid:-0}" 2>/dev/null || true
  wait "${pid:-0}" 2>/dev/null || true
  # The throwaway installation's gateway relay and sandbox network.
  (cd "$root" && FOREMAN_DATA_DIR="$work/data" uv run python -c \
    "from foreman.sandbox import teardown_isolation; teardown_isolation()" >/dev/null 2>&1) || true
  rm -rf "$work"
}
trap cleanup EXIT

# Origin repo (override the location with ORIGIN_REPO, e.g. for nicer screenshots)
origin="${ORIGIN_REPO:-$work/origin.git}"
rm -rf "$origin" && mkdir -p "$(dirname "$origin")"
git init -q --bare -b main "$origin"
git init -q -b main "$work/seed"
echo "# e2e" > "$work/seed/README.md"
mkdir -p "$work/seed/src" && echo "X = 1" > "$work/seed/src/app.py"
git -C "$work/seed" add -A
git -C "$work/seed" -c user.name=e2e -c user.email=e2e@local commit -qm init
git -C "$work/seed" push -q "$origin" main

(cd "$root/web" && npm run build >/dev/null)

export FOREMAN_DATA_DIR="$work/data" FOREMAN_PORT="$port" FOREMAN_POLL_INTERVAL_S=0.25
export FOREMAN_RUNTIME="${FOREMAN_RUNTIME:-docker}"
(cd "$root" && exec uv run foreman up) > "$work/foreman.log" 2>&1 &
pid=$!
# Scripted planner model for the Phase 1 tests.
(cd "$root" && exec uv run python -m tests.fakellm "$llm_port" --demo) > "$work/fakellm.log" 2>&1 &
llm_pid=$!

for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$port/api/system" >/dev/null && break
  sleep 0.5
done

cd "$root/web"
FOREMAN_URL="http://127.0.0.1:$port" ORIGIN_REPO="$origin" FAKE_LLM_URL="http://127.0.0.1:$llm_port/v1" \
  SHOTS_DIR="${SHOTS_DIR:-$root/web/e2e-results}" npx playwright test "$@" \
  || { echo "--- foreman log ---"; tail -50 "$work/foreman.log"; exit 1; }
