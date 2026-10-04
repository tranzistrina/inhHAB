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

if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Also allow PORT/HOST overrides through environment variables.
export PORT="${PORT:-1616}"
export HOST="${HOST:-127.0.0.1}"
exec python -u index.py
