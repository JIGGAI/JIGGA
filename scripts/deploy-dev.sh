#!/usr/bin/env bash
# Continuous deployment for a dev box: whatever ref a target is pinned to is
# what runs here. Unpinned means `main`, so merging is the deploy.
#
# GitHub cannot reach a tailnet machine, so this pulls rather than waiting to be
# pushed to. A systemd timer runs it every couple of minutes; it exits in under
# a second when nothing has changed.
#
#   ./scripts/deploy-dev.sh                     # deploy anything new, then exit
#   ./scripts/deploy-dev.sh status              # what is running, from which ref
#   ./scripts/deploy-dev.sh pin view feat/x     # run a branch here; keep it fresh
#   ./scripts/deploy-dev.sh unpin view          # back to main (or `unpin all`)
#   ./scripts/deploy-dev.sh --dry-run           # say what it would do, change nothing
#   ./scripts/deploy-dev.sh --force             # redeploy even without new commits
#   ./scripts/deploy-dev.sh --install-timer     # install + start the systemd timer
#   ./scripts/deploy-dev.sh --uninstall-timer
#
# Targets:
#   core   ~/jigga-stable                 the runtime the `jigga` on PATH runs from
#   view   ~/.jigga/plugins/jiggaview     the dashboard the plugin service serves
#
# A commit is deployed only when its CI is green (--skip-ci-check to override).
# A red target means the last good deploy keeps serving, which is the entire
# point of having CI.
set -euo pipefail

CORE_REPO="${JIGGA_DEPLOY_CORE:-$HOME/jigga-stable}"
VIEW_REPO="${JIGGA_DEPLOY_VIEW:-$HOME/jiggaview-stable}"
VIEW_URL="${JIGGA_DEPLOY_VIEW_URL:-https://github.com/JIGGAI/jiggaview.git}"
PLUGIN_DIR="${JIGGA_DEPLOY_PLUGIN_DIR:-$HOME/.jigga/plugins/jiggaview}"
LOG_FILE="${JIGGA_DEPLOY_LOG:-$HOME/.jigga/logs/deploy.log}"
LOCK_FILE="${JIGGA_DEPLOY_LOCK:-${TMPDIR:-/tmp}/jigga-deploy.lock}"
PINS_FILE="${JIGGA_DEPLOY_PINS:-$HOME/.jigga/state/deploy-pins}"
UNIT_DIR="$HOME/.config/systemd/user"
INTERVAL="${JIGGA_DEPLOY_INTERVAL:-2min}"

CORE_SLUG="JIGGAI/JIGGA"
VIEW_SLUG="JIGGAI/jiggaview"

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

# --- pins --------------------------------------------------------------------
#
# A pin is one line, `<target>=<ref>`. Deliberately a file rather than a flag:
# the timer runs unattended, so "which branch is this box on" has to survive
# between invocations and be answerable by reading one thing.

pinned_ref() {
  local target="$1"
  [[ -f "$PINS_FILE" ]] || { echo "main"; return; }
  local line
  line=$(grep -E "^${target}=" "$PINS_FILE" 2>/dev/null | tail -1 || true)
  echo "${line#*=}" | grep -q . && echo "${line#*=}" || echo "main"
}

set_pin() {
  local target="$1" ref="$2"
  mkdir -p "$(dirname "$PINS_FILE")"
  touch "$PINS_FILE"
  grep -vE "^${target}=" "$PINS_FILE" > "$PINS_FILE.tmp" 2>/dev/null || true
  [[ "$ref" == "main" ]] || echo "${target}=${ref}" >> "$PINS_FILE.tmp"
  mv "$PINS_FILE.tmp" "$PINS_FILE"
}

repo_for() { [[ "$1" == "core" ]] && echo "$CORE_REPO" || echo "$VIEW_REPO"; }
slug_for() { [[ "$1" == "core" ]] && echo "$CORE_SLUG" || echo "$VIEW_SLUG"; }

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

remote_sha() {
  git -C "$1" rev-parse "origin/$2" 2>/dev/null
}

# Is there anything new to deploy for this ref? Also reports "dirty" so a
# checkout someone is hand-editing is never clobbered by a background timer,
# and "gone" for a pinned branch that has been merged and deleted.
repo_state() {
  local dir="$1" ref="$2"
  [[ -d "$dir/.git" ]] || { echo "missing"; return; }
  git -C "$dir" fetch --quiet --prune origin 2>/dev/null || { echo "offline"; return; }
  remote_sha "$dir" "$ref" >/dev/null || { echo "gone"; return; }
  if [[ -n "$(git -C "$dir" status --porcelain)" ]]; then
    echo "dirty"
  elif [[ "$(git -C "$dir" rev-parse HEAD)" == "$(remote_sha "$dir" "$ref")" && "$FORCE" != 1 ]]; then
    echo "current"
  else
    echo "behind"
  fi
}

# Deploy targets track a ref; they are not workspaces. Checking out detached at
# `origin/<ref>` makes switching branches, and following a force-push, the same
# operation as a fast-forward — no local branch to diverge or need reconciling.
checkout_ref() {
  local dir="$1" ref="$2"
  run git -C "$dir" checkout --quiet --detach "origin/$ref"
}

# --- deploy ------------------------------------------------------------------

# Resolve a target to a deployable sha, or print why not and return non-zero.
# Shared by both targets so the pin, CI and refusal rules cannot drift apart.
resolve_target() {
  local target="$1" dir ref state sha verdict
  dir=$(repo_for "$target")
  ref=$(pinned_ref "$target")
  state=$(repo_state "$dir" "$ref")

  case "$state" in
    missing) log "$target: $dir is not a git checkout — skipping"; return 1 ;;
    offline) log "$target: cannot reach origin — skipping"; return 1 ;;
    dirty)   log "$target: $dir has local changes — refusing to touch it"; return 1 ;;
    current) return 1 ;;
    gone)
      # The usual way a pin ends: the branch was merged and deleted. Fall back
      # rather than serving a branch that no longer exists anywhere.
      log "$target: pinned ref '$ref' is gone from origin — unpinning, back to main"
      set_pin "$target" "main"
      return 1 ;;
  esac

  sha=$(remote_sha "$dir" "$ref")
  verdict=$(ci_verdict "$(slug_for "$target")" "$sha")
  case "$verdict" in
    red)     log "$target: ${sha:0:8} ($ref) has failing CI — NOT deploying"; return 1 ;;
    pending) log "$target: ${sha:0:8} ($ref) CI still running — waiting for the next tick"; return 1 ;;
  esac

  log "$target: deploying ${sha:0:8} from $ref (ci=$verdict)"
  echo "$ref"
}

deploy_core() {
  local ref
  ref=$(resolve_target core) || return 0
  checkout_ref "$CORE_REPO" "$ref"

  # The venv installs the package editable, so a checkout is usually the whole
  # deploy. Dependencies are the exception — reinstall only when the metadata
  # that declares them actually changed.
  if [[ "$DRY_RUN" == 0 ]] && ! git -C "$CORE_REPO" diff --quiet "HEAD@{1}" HEAD -- pyproject.toml 2>/dev/null; then
    log "core: pyproject changed — reinstalling"
    run "$CORE_REPO/.venv/bin/python" -m pip install --quiet -e "$CORE_REPO"
  fi

  # The supervisor is a long-running process holding the OLD code in memory.
  # Restarting it is the deploy; #205 made that safe to do from a script.
  log "core: restarting the supervisor"
  run systemctl --user restart jigga-supervisor.service
}

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

  local ref
  ref=$(resolve_target view) || return 0
  checkout_ref "$VIEW_REPO" "$ref"

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

# --- subcommands -------------------------------------------------------------

cmd_status() {
  local target dir ref head
  printf "%-6s %-34s %-10s %s\n" TARGET REF DEPLOYED SERVICE
  for target in core view; do
    dir=$(repo_for "$target")
    ref=$(pinned_ref "$target")
    head=$([[ -d "$dir/.git" ]] && git -C "$dir" rev-parse --short HEAD || echo "-")
    local unit="jigga-supervisor.service"
    [[ "$target" == "view" ]] && unit="jigga-plugin-jiggaview.service"
    printf "%-6s %-34s %-10s %s\n" "$target" "$ref" "$head" "$(systemctl --user is-active "$unit" 2>/dev/null)"
  done
  echo
  echo "timer: $(systemctl --user is-active jigga-deploy.timer 2>/dev/null) (every $INTERVAL)"
}

cmd_pin() {
  local target="$1" ref="$2"
  case "$target" in core|view) ;; *) echo "target must be core or view" >&2; exit 2 ;; esac
  [[ -n "$ref" ]] || { echo "usage: deploy-dev.sh pin <core|view> <ref>" >&2; exit 2; }
  local dir
  dir=$(repo_for "$target")
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" fetch --quiet --prune origin 2>/dev/null || true
    remote_sha "$dir" "$ref" >/dev/null || { echo "no such ref on origin: $ref" >&2; exit 1; }
  fi
  set_pin "$target" "$ref"
  log "$target: pinned to $ref"
  FORCE=1   # the ref changed, so deploy it now rather than at the next tick
}

cmd_unpin() {
  local target="$1"
  case "$target" in
    core|view) set_pin "$target" "main"; log "$target: unpinned — back to main" ;;
    all) set_pin core main; set_pin view main; log "unpinned everything — back to main" ;;
    *) echo "usage: deploy-dev.sh unpin <core|view|all>" >&2; exit 2 ;;
  esac
  FORCE=1
}

# --- timer -------------------------------------------------------------------

install_timer() {
  local script
  script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/jigga-deploy.service" <<EOF
[Unit]
Description=JIGGA dev auto-deploy (pull the pinned refs, rebuild, restart)
After=network-online.target

[Service]
Type=oneshot
ExecStart=$script
EOF
  cat > "$UNIT_DIR/jigga-deploy.timer" <<EOF
[Unit]
Description=Check for new commits to deploy

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

SUBCOMMAND=""
PIN_TARGET=""
PIN_REF=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    status) SUBCOMMAND=status ;;
    pin) SUBCOMMAND=pin; PIN_TARGET="${2:-}"; PIN_REF="${3:-}"; shift 2 || true ;;
    unpin) SUBCOMMAND=unpin; PIN_TARGET="${2:-}"; shift || true ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --skip-ci-check) SKIP_CI=1 ;;
    --install-timer) install_timer; exit 0 ;;
    --uninstall-timer) uninstall_timer; exit 0 ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ "$SUBCOMMAND" == "status" ]]; then
  cmd_status
  exit 0
fi

# One deploy at a time: a build outlasting the timer interval must not have a
# second run rsyncing under it.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy is running — skipping this tick"
  exit 0
fi

case "$SUBCOMMAND" in
  pin) cmd_pin "$PIN_TARGET" "$PIN_REF" ;;
  unpin) cmd_unpin "$PIN_TARGET" ;;
esac

deploy_core
deploy_view
