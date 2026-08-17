#!/usr/bin/env bash
# Continuous deployment for a dev box: whatever is on `main` is what runs here.
#
# GitHub cannot reach a tailnet machine, so this pulls rather than waiting to be
# pushed to. A systemd timer runs it every couple of minutes; it exits in under
# a second when nothing has changed.
#
#   ./scripts/deploy-dev.sh                 # deploy anything new, then exit
#   ./scripts/deploy-dev.sh --dry-run       # say what it would do, change nothing
#   ./scripts/deploy-dev.sh --force         # redeploy even without new commits
#   ./scripts/deploy-dev.sh --install-timer # install + start the systemd timer
#   ./scripts/deploy-dev.sh --uninstall-timer
#
# What it deploys:
#   ~/jigga-stable                  the runtime the `jigga` on PATH runs from
#   ~/.jigga/plugins/jiggaview      the dashboard the plugin service serves
#
# It deploys a commit only when that commit's CI is green (--skip-ci-check to
# override). A red main means the last good deploy keeps serving, which is the
# entire point of having CI.
set -euo pipefail

CORE_REPO="${JIGGA_DEPLOY_CORE:-$HOME/jigga-stable}"
VIEW_REPO="${JIGGA_DEPLOY_VIEW:-$HOME/jiggaview-stable}"
VIEW_URL="${JIGGA_DEPLOY_VIEW_URL:-https://github.com/JIGGAI/jiggaview.git}"
PLUGIN_DIR="${JIGGA_DEPLOY_PLUGIN_DIR:-$HOME/.jigga/plugins/jiggaview}"
LOG_FILE="${JIGGA_DEPLOY_LOG:-$HOME/.jigga/logs/deploy.log}"
LOCK_FILE="${JIGGA_DEPLOY_LOCK:-${TMPDIR:-/tmp}/jigga-deploy.lock}"
UNIT_DIR="$HOME/.config/systemd/user"
INTERVAL="${JIGGA_DEPLOY_INTERVAL:-2min}"

DRY_RUN=0
FORCE=0
SKIP_CI=0

log() {
  local line="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
  echo "$line"
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "$line" >> "$LOG_FILE"
}

run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    log "DRY-RUN would: $*"
    return 0
  fi
  "$@"
}

# --- CI gate -----------------------------------------------------------------

# Deploy only a commit CI already vetted. Three answers, and "pending" is not a
# failure — the next timer tick asks again, which is why this returns a word
# rather than an exit code.
ci_verdict() {
  local repo="$1" sha="$2"
  if [[ "$SKIP_CI" == 1 ]]; then
    echo "skipped"
    return
  fi
  local json
  if ! json=$(gh api "repos/$repo/commits/$sha/check-runs" 2>/dev/null); then
    echo "unknown"   # no gh auth / offline / no CI on this repo
    return
  fi
  python3 - "$json" <<'PY'
import json, sys
runs = json.loads(sys.argv[1]).get("check_runs") or []
if not runs:
    print("unknown")
elif any(r.get("status") != "completed" for r in runs):
    print("pending")
elif all(r.get("conclusion") in ("success", "neutral", "skipped") for r in runs):
    print("green")
else:
    print("red")
PY
}

# --- git ---------------------------------------------------------------------

fetch_head_sha() {
  git -C "$1" rev-parse origin/main
}

# Is there anything new to deploy? Also reports "dirty" so a checkout someone is
# hand-editing is never clobbered by a background timer.
repo_state() {
  local dir="$1"
  [[ -d "$dir/.git" ]] || { echo "missing"; return; }
  git -C "$dir" fetch --quiet origin main 2>/dev/null || { echo "offline"; return; }
  if [[ -n "$(git -C "$dir" status --porcelain)" ]]; then
    echo "dirty"
  elif [[ "$(git -C "$dir" rev-parse HEAD)" == "$(fetch_head_sha "$dir")" && "$FORCE" != 1 ]]; then
    echo "current"
  else
    echo "behind"
  fi
}

# --- core --------------------------------------------------------------------

deploy_core() {
  local state
  state=$(repo_state "$CORE_REPO")
  case "$state" in
    missing) log "core: $CORE_REPO is not a git checkout — skipping"; return 0 ;;
    offline) log "core: cannot reach origin — skipping"; return 0 ;;
    dirty)   log "core: $CORE_REPO has local changes — refusing to touch it"; return 0 ;;
    current) return 0 ;;
  esac

  local sha verdict
  sha=$(fetch_head_sha "$CORE_REPO")
  verdict=$(ci_verdict "JIGGAI/JIGGA" "$sha")
  case "$verdict" in
    red)     log "core: ${sha:0:8} has failing CI — NOT deploying"; return 0 ;;
    pending) log "core: ${sha:0:8} CI still running — waiting for the next tick"; return 0 ;;
  esac

  log "core: deploying ${sha:0:8} (ci=$verdict)"
  run git -C "$CORE_REPO" merge --ff-only origin/main

  # The venv installs the package editable, so a pull is usually the whole
  # deploy. Dependencies are the exception — reinstall only when the metadata
  # that declares them actually changed.
  if [[ "$DRY_RUN" == 0 ]] && ! git -C "$CORE_REPO" diff --quiet "HEAD@{1}" HEAD -- pyproject.toml; then
    log "core: pyproject changed — reinstalling"
    run "$CORE_REPO/.venv/bin/python" -m pip install --quiet -e "$CORE_REPO"
  fi

  # The supervisor is a long-running process holding the OLD code in memory.
  # Restarting it is the deploy; #205 made that safe to do from a script.
  log "core: restarting the supervisor"
  run systemctl --user restart jigga-supervisor.service
}

# --- jiggaview ---------------------------------------------------------------

deploy_view() {
  if [[ ! -d "$VIEW_REPO/.git" ]]; then
    log "view: cloning $VIEW_URL -> $VIEW_REPO"
    run git clone --quiet "$VIEW_URL" "$VIEW_REPO"
    FORCE=1   # nothing is deployed from a fresh clone yet
    if [[ "$DRY_RUN" == 1 ]]; then
      log "DRY-RUN would then: rsync -> $PLUGIN_DIR, npm ci + build, restart the dashboard"
      return 0
    fi
  fi

  local state
  state=$(repo_state "$VIEW_REPO")
  case "$state" in
    offline) log "view: cannot reach origin — skipping"; return 0 ;;
    dirty)   log "view: $VIEW_REPO has local changes — refusing to touch it"; return 0 ;;
    current) return 0 ;;
  esac

  local sha verdict
  sha=$(fetch_head_sha "$VIEW_REPO")
  verdict=$(ci_verdict "JIGGAI/jiggaview" "$sha")
  case "$verdict" in
    red)     log "view: ${sha:0:8} has failing CI — NOT deploying"; return 0 ;;
    pending) log "view: ${sha:0:8} CI still running — waiting for the next tick"; return 0 ;;
  esac

  log "view: deploying ${sha:0:8} (ci=$verdict)"
  run git -C "$VIEW_REPO" merge --ff-only origin/main

  # `jigga plugins install` copies a source tree into the plugin dir; there is
  # no `plugins update`. Mirroring the source and rebuilding in place is the
  # same end state without discarding the recorded approval — and it keeps
  # node_modules, which a reinstall would spend two minutes re-fetching.
  run mkdir -p "$PLUGIN_DIR"
  run rsync -a --delete \
    --exclude node_modules --exclude .next --exclude .git \
    "$VIEW_REPO/" "$PLUGIN_DIR/"

  if [[ "$DRY_RUN" == 0 ]]; then
    if [[ ! -d "$PLUGIN_DIR/node_modules" ]] || ! cmp -s "$VIEW_REPO/package-lock.json" "$PLUGIN_DIR/.deployed-lock"; then
      log "view: dependencies changed — npm ci"
      (cd "$PLUGIN_DIR" && npm ci --silent)
      cp "$VIEW_REPO/package-lock.json" "$PLUGIN_DIR/.deployed-lock"
    fi
    log "view: building"
    (cd "$PLUGIN_DIR" && npm run build >/dev/null)
  else
    log "DRY-RUN would: npm ci (if lock changed) + npm run build in $PLUGIN_DIR"
  fi

  log "view: restarting the dashboard"
  run systemctl --user restart jigga-plugin-jiggaview.service
}

# --- timer -------------------------------------------------------------------

install_timer() {
  local script
  script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/jigga-deploy.service" <<EOF
[Unit]
Description=JIGGA dev auto-deploy (pull main, rebuild, restart)
After=network-online.target

[Service]
Type=oneshot
ExecStart=$script
EOF
  cat > "$UNIT_DIR/jigga-deploy.timer" <<EOF
[Unit]
Description=Check for new main commits to deploy

[Timer]
OnBootSec=1min
OnUnitActiveSec=$INTERVAL
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now jigga-deploy.timer
  log "timer installed — deploying every $INTERVAL (systemctl --user list-timers)"
}

uninstall_timer() {
  systemctl --user disable --now jigga-deploy.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/jigga-deploy.timer" "$UNIT_DIR/jigga-deploy.service"
  systemctl --user daemon-reload
  log "timer removed"
}

# --- main --------------------------------------------------------------------

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --skip-ci-check) SKIP_CI=1 ;;
    --install-timer) install_timer; exit 0 ;;
    --uninstall-timer) uninstall_timer; exit 0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# One deploy at a time: a build outlasting the timer interval must not have a
# second run rsyncing under it.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy is running — skipping this tick"
  exit 0
fi

deploy_core
deploy_view
