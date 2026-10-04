#!/bin/sh
set -eu
cd "$(dirname "$0")"

# Optional: ./run_mac.sh --setup-code 'your-own-code'
if [ "${1:-}" = "--setup-code" ]; then
  if [ -z "${2:-}" ]; then
    echo "Usage: ./run_mac.sh [--setup-code YOUR_CODE]" >&2
    exit 2
  fi
  export ADMIN_SETUP_CODE="$2"
  shift 2
fi

if [ "$#" -gt 0 ]; then
  echo "Unknown argument: $1" >&2
  echo "Usage: ./run_mac.sh [--setup-code YOUR_CODE]" >&2
  exit 2
fi

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "inhHAB now requires Python 3.11+." >&2
  echo "On macOS with Homebrew: brew install python@3.13" >&2
  echo "Then run ./run_mac.sh again." >&2
  exit 1
fi

if [ -x .venv/bin/python ]; then
  if ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "Recreating .venv with Python 3.11+..."
    rm -rf .venv
  fi
fi

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1; then
  echo "WARNING: Deno/Node is not installed. Full YouTube support requires a supported JavaScript runtime." >&2
  echo "Recommended on macOS: brew install deno" >&2
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "WARNING: ffmpeg is not installed. High-quality video+audio merging may fail." >&2
  echo "Recommended on macOS: brew install ffmpeg" >&2
fi

# Also allow PORT/HOST overrides through environment variables.
export PORT="${PORT:-1616}"
export HOST="${HOST:-127.0.0.1}"
exec python -u index.py
