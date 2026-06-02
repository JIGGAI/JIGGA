#!/usr/bin/env bash
# JIGGA bootstrap installer.
#
# Runs with whatever Python/shell a fresh machine already has — it does NOT
# assume `jigga` is installed yet (that's the point). It finds a Python 3.11+,
# creates an isolated .venv with it, upgrades pip past the PEP 660 cutoff, and
# installs JIGGA in editable mode. This catches the two failures a stock macOS
# hits otherwise:
#   - "editable mode requires a setuptools-based build"  (pip < 21.3)
#   - "requires a different Python: 3.9.x not in '>=3.11'" (macOS system python3)
#
# Usage:
#   ./scripts/install.sh              # create .venv and install
#   ./scripts/install.sh --init       # also run `jigga init` + `jigga setup`
#   VENV=.venv311 ./scripts/install.sh  # custom venv dir
#   PYTHON=/path/to/python3.12 ./scripts/install.sh  # force an interpreter

set -euo pipefail

MIN_MAJOR=3
MIN_MINOR=11
VENV="${VENV:-.venv}"
DO_INIT=0

for arg in "$@"; do
  case "$arg" in
    --init) DO_INIT=1 ;;
    -h|--help)
      # Print the leading comment header (everything from line 2 up to the
      # first non-comment line), stripping the leading "# ".
      sed -n '2,${/^#/!q;s/^# \{0,1\}//;p;}' "$0"
      exit 0 ;;
    *) printf 'Unknown argument: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

# Resolve repo root from this script's location so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m  ✗\033[0m %s\n' "$*" >&2; exit 1; }

# Is the given interpreter >= MIN_MAJOR.MIN_MINOR? (silent; returns status)
py_ok() {
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($MIN_MAJOR, $MIN_MINOR) else 1)" \
    >/dev/null 2>&1
}

# Find a suitable interpreter into the global PY: honor $PYTHON, else probe
# newest-first so we don't settle for the stock `python3` (often 3.9 on macOS)
# when a newer one is installed alongside it. Returns non-zero if none found.
# Calls die() directly (in the main shell, so the script stops) when an
# explicit $PYTHON is given but unusable — that's a precise user error, not the
# generic "no Python installed" case.
find_python() {
  if [ -n "${PYTHON:-}" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || die "PYTHON=$PYTHON not found on PATH"
    py_ok "$PYTHON" || die "PYTHON=$PYTHON is older than ${MIN_MAJOR}.${MIN_MINOR}"
    PY="$PYTHON"
    return 0
  fi
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && py_ok "$candidate"; then
      PY="$candidate"
      return 0
    fi
  done
  return 1
}

say "Looking for Python ${MIN_MAJOR}.${MIN_MINOR}+"
if ! find_python; then
  warn "No Python ${MIN_MAJOR}.${MIN_MINOR}+ found."
  case "$(uname -s)" in
    Darwin) warn "Install one with:  brew install python@3.12" ;;
    Linux)  warn "Install one with:  sudo apt install python3.12 python3.12-venv   (or use pyenv)" ;;
    *)      warn "Install Python ${MIN_MAJOR}.${MIN_MINOR}+ from https://python.org" ;;
  esac
  die "Re-run this script once a recent Python is available (or pass PYTHON=/path/to/python3.12)."
fi
ok "Using $("$PY" --version 2>&1) at $(command -v "$PY")"

say "Creating virtual environment at $VENV"
if [ -d "$VENV" ]; then
  if py_ok "$VENV/bin/python"; then
    ok "Reusing existing $VENV ($("$VENV/bin/python" --version 2>&1))"
  else
    warn "Existing $VENV uses an old Python — recreating it."
    rm -rf "$VENV"
    "$PY" -m venv "$VENV"
  fi
else
  "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
[ -x "$VPY" ] || VPY="$VENV/Scripts/python.exe"  # Windows layout
[ -x "$VPY" ] || die "venv python not found under $VENV"

say "Upgrading pip (need >= 21.3 for editable installs)"
"$VPY" -m pip install --quiet --upgrade pip
ok "pip $("$VPY" -m pip --version | awk '{print $2}')"

say "Installing JIGGA (editable)"
"$VPY" -m pip install --quiet -e .
ok "jigga installed"

# Resolve the installed console script for the chained --init flow.
JIGGA="$VENV/bin/jigga"
[ -x "$JIGGA" ] || JIGGA="$VENV/Scripts/jigga.exe"  # Windows layout

if [ "$DO_INIT" -eq 1 ]; then
  [ -x "$JIGGA" ] || die "jigga console script not found under $VENV after install"
  say "Creating the local runtime (jigga init --examples)"
  "$JIGGA" init --examples
  say "First-run setup (jigga setup)"
  "$JIGGA" setup
  cat <<EOF

$(say "Ready. Activate the environment and connect a model:")
  source $VENV/bin/activate
  jigga model setup         # connect a model so agents can think

EOF
else
  cat <<EOF

$(say "Done. Next steps:")
  source $VENV/bin/activate
  jigga init --examples     # create ~/.jigga
  jigga setup               # who the assistant works for + your default agent
  jigga model setup         # connect a model so agents can think

  (or re-run with --init to do the first two automatically)

EOF
fi
