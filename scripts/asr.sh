#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASR_HOME="${ASR_HOME:-$HOME/.video-transcription-audit}"
VENV_PYTHON="$ASR_HOME/venv/bin/python"

if [[ -n "${ASR_PYTHON:-}" && -x "$ASR_PYTHON" ]]; then
  PYTHON="$ASR_PYTHON"
elif [[ -x "$VENV_PYTHON" ]]; then
  PYTHON="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "Python runtime not found. Run setup.sh first." >&2
  exit 1
fi

exec "$PYTHON" "$SCRIPT_DIR/asr_cli.py" "$@"
