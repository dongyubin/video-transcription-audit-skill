#!/usr/bin/env bash
set -euo pipefail

PROFILE="auto"
DRY_RUN=0
FORCE=0
INDEX_URL=""
PROBE_MIRRORS=0
SIMULATE_PLATFORM=""
SIMULATE_NVIDIA=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?missing profile}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --index-url)
      INDEX_URL="${2:?missing index URL}"
      shift 2
      ;;
    --probe-mirrors)
      PROBE_MIRRORS=1
      shift
      ;;
    --simulate-platform)
      SIMULATE_PLATFORM="${2:?missing simulated platform}"
      shift 2
      ;;
    --simulate-nvidia)
      SIMULATE_NVIDIA=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

case "$PROFILE" in
  auto|local|cloud) ;;
  *) echo "Profile must be auto, local, or cloud" >&2; exit 2 ;;
esac

if [[ -n "$INDEX_URL" && "$PROBE_MIRRORS" -eq 1 ]]; then
  echo "--index-url and --probe-mirrors are mutually exclusive" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_PATH="$SCRIPT_DIR/asr_cli.py"
ASR_HOME="${ASR_HOME:-$HOME/.video-transcription-audit}"
VENV_DIR="$ASR_HOME/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
OFFICIAL_INDEX="https://pypi.org/simple"

run_step() {
  local description="$1"
  shift
  echo "==> $description"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  else
    printf '    '
    printf '%q ' "$@"
    printf '\n'
  fi
}

install_system_package() {
  local package="$1"
  if [[ "$DRY_RUN" -eq 1 && "$SIMULATE_PLATFORM" == darwin-* ]]; then
    local brew_package="$package"
    [[ "$package" == "python3" ]] && brew_package="python@3.11"
    run_step "Install $brew_package with Homebrew" brew install "$brew_package"
    return
  fi
  if [[ "$DRY_RUN" -eq 1 && "$SIMULATE_PLATFORM" == ubuntu-* ]]; then
    run_step "Refresh apt package metadata" sudo apt-get update
    run_step "Install $package with apt" sudo apt-get install -y "$package"
    return
  fi
  if command -v brew >/dev/null 2>&1; then
    local brew_package="$package"
    [[ "$package" == "python3" ]] && brew_package="python@3.11"
    run_step "Install $brew_package with Homebrew" brew install "$brew_package"
  elif command -v apt-get >/dev/null 2>&1; then
    run_step "Refresh apt package metadata" sudo apt-get update
    run_step "Install $package with apt" sudo apt-get install -y "$package"
  elif command -v dnf >/dev/null 2>&1; then
    run_step "Install $package with dnf" sudo dnf install -y "$package"
  elif command -v pacman >/dev/null 2>&1; then
    run_step "Install $package with pacman" sudo pacman -S --needed --noconfirm "$package"
  else
    echo "No supported package manager found. Install $package manually." >&2
    exit 1
  fi
}

python_is_valid() {
  local python_path="$1"
  [[ -x "$python_path" ]] &&
    "$python_path" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1
}

doctor_json() {
  local python_path="$1"
  "$python_path" "$CLI_PATH" doctor \
    --profile "$PROFILE" \
    --install-check \
    --json 2>/dev/null || true
}

json_bool() {
  local python_path="$1"
  local field="$2"
  "$python_path" -c \
    'import json,sys; print("1" if json.load(sys.stdin).get(sys.argv[1]) else "0")' \
    "$field"
}

json_list() {
  local python_path="$1"
  local field="$2"
  "$python_path" -c \
    'import json,sys; print("\n".join(json.load(sys.stdin).get(sys.argv[1], [])))' \
    "$field"
}

json_unsatisfied_specs() {
  local python_path="$1"
  "$python_path" -c \
    'import json,sys; print("\n".join(item["spec"] for item in json.load(sys.stdin).get("requirements", []) if not item.get("satisfied")))'
}

echo "ASR_HOME: $ASR_HOME"
echo "Profile: $PROFILE"
[[ "$DRY_RUN" -eq 1 ]] && echo "Dry-run enabled; no changes will be made."

CANDIDATE_PYTHON=""
CANDIDATE_SOURCE=""
if [[ -n "${ASR_PYTHON:-}" ]]; then
  if ! python_is_valid "$ASR_PYTHON"; then
    echo "ASR_PYTHON does not point to a working Python 3.9+ executable." >&2
    exit 1
  fi
  CANDIDATE_PYTHON="$ASR_PYTHON"
  CANDIDATE_SOURCE="ASR_PYTHON"
elif [[ -e "$VENV_PYTHON" ]]; then
  if ! python_is_valid "$VENV_PYTHON"; then
    echo "The existing ASR_HOME virtual environment is damaged or uses Python older than 3.9." >&2
    echo "Move it aside and rerun setup; it will not be deleted automatically." >&2
    exit 1
  fi
  CANDIDATE_PYTHON="$VENV_PYTHON"
  CANDIDATE_SOURCE="ASR_HOME"
fi

PREFLIGHT_JSON=""
if [[ -n "$CANDIDATE_PYTHON" && -z "$SIMULATE_PLATFORM" ]]; then
  PREFLIGHT_JSON="$(doctor_json "$CANDIDATE_PYTHON")"
  if [[ -z "$PREFLIGHT_JSON" ]]; then
    echo "Environment doctor did not return valid JSON using $CANDIDATE_PYTHON." >&2
    exit 1
  fi
  echo "Candidate environment: $CANDIDATE_SOURCE ($CANDIDATE_PYTHON)"
  INSTALL_READY="$(printf '%s' "$PREFLIGHT_JSON" | json_bool "$CANDIDATE_PYTHON" install_ready)"
  if [[ "$INSTALL_READY" -eq 1 && "$FORCE" -eq 0 ]]; then
    echo "Environment already install-ready; no packages or indexes were accessed."
    exit 0
  fi
fi

SYSTEM_PYTHON=""
if command -v python3 >/dev/null 2>&1 &&
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
  SYSTEM_PYTHON="$(command -v python3)"
fi

if [[ -z "$CANDIDATE_PYTHON" && -z "$SYSTEM_PYTHON" ]]; then
  install_system_package python3
  if [[ "$DRY_RUN" -eq 0 ]]; then
    SYSTEM_PYTHON="$(command -v python3 || true)"
    if [[ -z "$SYSTEM_PYTHON" ]] ||
      ! "$SYSTEM_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
      echo "Python was installed but Python 3.9+ is not available in the current shell." >&2
      exit 1
    fi
  fi
fi

MEDIA_READY=0
if [[ -n "$PREFLIGHT_JSON" ]]; then
  MEDIA_READY="$(printf '%s' "$PREFLIGHT_JSON" | json_bool "$CANDIDATE_PYTHON" media_tools_ready)"
elif [[ -z "$SIMULATE_PLATFORM" ]] &&
  command -v ffmpeg >/dev/null 2>&1 &&
  command -v ffprobe >/dev/null 2>&1; then
  MEDIA_READY=1
fi
if [[ "$MEDIA_READY" -eq 0 ]]; then
  install_system_package ffmpeg
fi

if [[ "$SIMULATE_PLATFORM" == ubuntu-* && -z "$CANDIDATE_PYTHON" ]]; then
  install_system_package python3-venv
elif [[ -z "$SIMULATE_PLATFORM" ]] &&
  command -v apt-get >/dev/null 2>&1 &&
  [[ -n "$SYSTEM_PYTHON" ]] &&
  ! "$SYSTEM_PYTHON" -m venv --help >/dev/null 2>&1; then
  install_system_package python3-venv
fi

NEW_ENVIRONMENT=0
if [[ -z "$CANDIDATE_PYTHON" ]]; then
  run_step "Create runtime directory" mkdir -p "$ASR_HOME"
  run_step "Create Python virtual environment" "${SYSTEM_PYTHON:-python3}" -m venv "$VENV_DIR"
  CANDIDATE_PYTHON="$VENV_PYTHON"
  CANDIDATE_SOURCE="new ASR_HOME"
  NEW_ENVIRONMENT=1
fi

REQUIREMENTS_READY=0
IMPORTS_READY=0
PIP_READY=0
CUDA_READY=0
if [[ -n "$PREFLIGHT_JSON" ]]; then
  REQUIREMENTS_READY="$(printf '%s' "$PREFLIGHT_JSON" | json_bool "$CANDIDATE_PYTHON" requirements_ready)"
  IMPORTS_READY="$(printf '%s' "$PREFLIGHT_JSON" | json_bool "$CANDIDATE_PYTHON" imports_ready)"
  PIP_READY="$(
    printf '%s' "$PREFLIGHT_JSON" |
      "$CANDIDATE_PYTHON" -c \
        'import json,sys; print("1" if json.load(sys.stdin).get("pip_check", {}).get("ok") else "0")'
  )"
  CUDA_READY="$(printf '%s' "$PREFLIGHT_JSON" | json_bool "$CANDIDATE_PYTHON" cuda_ready)"
fi

NEEDS_PYTHON_REPAIR=0
if [[ "$NEW_ENVIRONMENT" -eq 1 || "$FORCE" -eq 1 ||
  "$REQUIREMENTS_READY" -eq 0 || "$IMPORTS_READY" -eq 0 ||
  "$PIP_READY" -eq 0 || "$CUDA_READY" -eq 0 ]]; then
  NEEDS_PYTHON_REPAIR=1
fi

if [[ "$NEEDS_PYTHON_REPAIR" -eq 1 ]]; then
  SELECTED_INDEX="${INDEX_URL:-$OFFICIAL_INDEX}"
  PROBE_PYTHON="$CANDIDATE_PYTHON"
  if [[ "$DRY_RUN" -eq 1 && ! -x "$PROBE_PYTHON" ]]; then
    PROBE_PYTHON="${SYSTEM_PYTHON:-python3}"
  fi
  if [[ "$PROBE_MIRRORS" -eq 1 ]]; then
    echo "==> Probe configured package indexes"
    SELECTED_INDEX="$("$PROBE_PYTHON" "$CLI_PATH" probe-index)"
  fi
  echo "Package index: $SELECTED_INDEX"

  if [[ "$NEW_ENVIRONMENT" -eq 1 ]]; then
    run_step \
      "Upgrade pip tooling in the new environment" \
      "$CANDIDATE_PYTHON" -m pip install \
      --index-url "$SELECTED_INDEX" \
      --upgrade pip setuptools wheel
  elif [[ "$DRY_RUN" -eq 0 ]] &&
    ! "$CANDIDATE_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$CANDIDATE_PYTHON" -m ensurepip --upgrade
  fi

  REQUIREMENT_GROUPS=()
  if [[ -n "$PREFLIGHT_JSON" ]]; then
    while IFS= read -r group; do
      [[ -n "$group" ]] && REQUIREMENT_GROUPS+=("$group")
    done < <(printf '%s' "$PREFLIGHT_JSON" | json_list "$CANDIDATE_PYTHON" required_groups)
  else
    REQUIREMENT_GROUPS=("base")
    if [[ "$PROFILE" != "cloud" ]]; then
      if [[ "$SIMULATE_PLATFORM" == darwin-arm64 ||
        "$SIMULATE_PLATFORM" == darwin-aarch64 ]]; then
        REQUIREMENT_GROUPS+=("macos")
      elif [[ "$SIMULATE_NVIDIA" -eq 1 ]]; then
        REQUIREMENT_GROUPS+=("nvidia")
      elif [[ -z "$SIMULATE_PLATFORM" ]] && command -v nvidia-smi >/dev/null 2>&1; then
        NVIDIA_MEMORY="$(
          nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null |
            head -n 1 |
            tr -d '[:space:]'
        )"
        if [[ "$NVIDIA_MEMORY" =~ ^[0-9]+$ && "$NVIDIA_MEMORY" -ge 4096 ]]; then
          REQUIREMENT_GROUPS+=("nvidia")
        fi
      fi
    fi
  fi

  REINSTALL=0
  BROKEN_RUNTIME=0
  if [[ -n "$PREFLIGHT_JSON" && "$REQUIREMENTS_READY" -eq 1 &&
    ("$IMPORTS_READY" -eq 0 || "$CUDA_READY" -eq 0 || "$PIP_READY" -eq 0) ]]; then
    BROKEN_RUNTIME=1
  fi
  if [[ "$NEW_ENVIRONMENT" -eq 0 &&
    ("$FORCE" -eq 1 || "$BROKEN_RUNTIME" -eq 1) ]]; then
    REINSTALL=1
  fi
  UNSATISFIED_SPECS=()
  if [[ -n "$PREFLIGHT_JSON" ]]; then
    while IFS= read -r spec; do
      [[ -n "$spec" ]] && UNSATISFIED_SPECS+=("$spec")
    done < <(printf '%s' "$PREFLIGHT_JSON" | json_unsatisfied_specs "$CANDIDATE_PYTHON")
  fi
  if [[ -n "$PREFLIGHT_JSON" && "$FORCE" -eq 0 &&
    "$BROKEN_RUNTIME" -eq 0 && "${#UNSATISFIED_SPECS[@]}" -gt 0 ]]; then
    run_step \
      "Install missing or incompatible dependencies" \
      "$CANDIDATE_PYTHON" -m pip install \
      --index-url "$SELECTED_INDEX" \
      "${UNSATISFIED_SPECS[@]}"
  else
    for group in "${REQUIREMENT_GROUPS[@]}"; do
      REQUIREMENT_FILE="$SCRIPT_DIR/requirements-$group.txt"
      PIP_ARGS=(
        "$CANDIDATE_PYTHON" -m pip install
        --index-url "$SELECTED_INDEX"
      )
      [[ "$REINSTALL" -eq 1 ]] && PIP_ARGS+=(--force-reinstall)
      PIP_ARGS+=(-r "$REQUIREMENT_FILE")
      run_step "Install or repair $group dependencies" "${PIP_ARGS[@]}"
    done
  fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry-run complete."
  exit 0
fi

FINAL_JSON="$(doctor_json "$CANDIDATE_PYTHON")"
FINAL_READY="$(printf '%s' "$FINAL_JSON" | json_bool "$CANDIDATE_PYTHON" install_ready)"
if [[ "$FINAL_READY" -ne 1 ]]; then
  "$CANDIDATE_PYTHON" "$CLI_PATH" doctor --profile "$PROFILE" || true
  echo "Environment installation finished, but the strict readiness check failed." >&2
  exit 1
fi

echo "Environment verification passed."
echo "Setup complete. Runtime: $CANDIDATE_PYTHON"
