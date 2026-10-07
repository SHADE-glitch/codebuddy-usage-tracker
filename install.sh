#!/usr/bin/env bash
#
# install.sh — install or re-install the CodeBuddy usage tracker from this repo.
#
# Idempotent: re-running is safe. It creates repo/.venv with the TUI dependency
# (textual) and symlinks bin/cbut into ~/.local/bin, so `git pull` here updates
# the running install. The headless subcommands (cbut sync/stats/...) need only
# python3; only the interactive TUI needs the venv.
#
#   ./install.sh            install (refuses to replace a real file)
#   ./install.sh --force    also replace an existing non-symlink at the target
#
# Environment overrides (mainly for testing a sandboxed install):
#   CBUT_BIN_DIR    default: ~/.local/bin
set -euo pipefail

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--force]"
      echo "  --force  replace an existing non-symlink at the install location"
      exit 0
      ;;
    *) echo "install.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || true)"
REPO_DIR="$(cd "$(dirname "${SELF:-${BASH_SOURCE[0]}}")" && pwd)"
cd "$REPO_DIR"

BIN_DIR="${CBUT_BIN_DIR:-$HOME/.local/bin}"

# --- 1. TUI virtualenv -----------------------------------------------------
VENV="$REPO_DIR/.venv"
if [ -x "$VENV/bin/python" ]; then
  echo "==> venv already present: $VENV"
else
  echo "==> creating venv: $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.13 "$VENV" || uv venv "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi

echo "==> installing TUI dependencies"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" -r "$REPO_DIR/requirements.txt"
else
  "$VENV/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"
fi

# --- 2. symlink the entry point --------------------------------------------
DST="$BIN_DIR/cbut"
if [ -e "$DST" ] && [ ! -L "$DST" ] && [ "$FORCE" -ne 1 ]; then
  echo "install.sh: refusing to replace '$DST' (exists and is not a symlink)" >&2
  echo "  move or delete it first, or re-run with --force" >&2
  exit 1
fi
mkdir -p "$BIN_DIR"
[ -L "$DST" ] && rm -f "$DST"
[ -e "$DST" ] && rm -f "$DST"
ln -s "$REPO_DIR/bin/cbut" "$DST"
echo "  $DST -> $REPO_DIR/bin/cbut"

# --- 3. verify -------------------------------------------------------------
echo
echo "==> cbut health"
"$DST" health || echo "  (health reported a problem — see above)"

cat <<EOF

Installed.

  cbut            launch the interactive TUI
  cbut sync       index new CodeBuddy logs (run this first)
  cbut stats      usage summary, no TUI required

Optional daily sync (so the index stays current without opening the TUI):
  mkdir -p ~/.config/systemd/user
  cp "$REPO_DIR/systemd/cbut-sync."* ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now cbut-sync.timer
EOF
