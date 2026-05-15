#!/bin/sh
# Kros host-side launcher installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/zhixiangxue/kros/main/launcher/install.sh | sh
#
# Custom install location:
#   curl -fsSL .../install.sh | KROS_INSTALL_DIR=$HOME/bin sh

set -e

INSTALL_DIR="${KROS_INSTALL_DIR:-/usr/local/bin}"
LAUNCHER_URL="${KROS_LAUNCHER_URL:-https://raw.githubusercontent.com/zhixiangxue/kros/main/launcher/kros}"
BIN_PATH="$INSTALL_DIR/kros"

echo "Kros launcher installer"
echo "  install dir: $INSTALL_DIR"
echo "  source:      $LAUNCHER_URL"
echo

# Soft warning: docker is required at runtime, but missing now is not fatal.
if ! command -v docker >/dev/null 2>&1; then
    echo "WARNING: docker not found in PATH. Install Docker before running 'kros run'." >&2
fi

# Decide whether sudo is needed.
SUDO=""
if [ ! -w "$INSTALL_DIR" ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        echo "Note: $INSTALL_DIR is not writable; will use sudo."
    else
        echo "ERROR: $INSTALL_DIR is not writable and sudo is not available." >&2
        echo "Try: KROS_INSTALL_DIR=\$HOME/bin curl ... | sh" >&2
        exit 1
    fi
fi

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

echo "Downloading launcher..."
curl -fsSL "$LAUNCHER_URL" -o "$TMPFILE"

echo "Installing to $BIN_PATH..."
$SUDO install -m 0755 "$TMPFILE" "$BIN_PATH"

echo
echo "Installed: $BIN_PATH"
echo "Try: kros --help"
