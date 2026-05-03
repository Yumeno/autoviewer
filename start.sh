#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "[ERROR] Python was not found. Install Python 3 and try again." >&2
  exit 1
fi

cd "$SCRIPT_DIR"

if [[ ! -f requirements.txt ]]; then
  echo "[ERROR] requirements.txt was not found." >&2
  exit 1
fi

echo "=========================================="
echo "Auto Image Viewer startup"
echo "=========================================="

"$PYTHON_BIN" --version

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Local virtual environment was not found. Bootstrapping .venv..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_PYTHON" -m pip install --upgrade pip
  "$VENV_PYTHON" -m pip install -r "$SCRIPT_DIR/requirements.txt"
fi

echo "Launching viewer..."
exec "$VENV_PYTHON" "$SCRIPT_DIR/viewer.py"
