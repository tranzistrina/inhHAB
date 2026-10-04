#!/bin/sh
set -eu
cd "$(dirname "$0")"

# Local: ./run_mac.sh
# External: HOST=0.0.0.0 PORT=1616 ./run_mac.sh
# Then open http://SERVER_IP:1616

if [ "${1:-}" = "--setup-code" ]; then
  if [ -z "${2:-}" ]; then echo "Usage: ./run_mac.sh [--setup-code YOUR_CODE]" >&2; exit 2; fi
  export ADMIN_SETUP_CODE="$2"; shift 2
fi
if [ "$#" -gt 0 ]; then echo "Unknown argument: $1" >&2; exit 2; fi

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then command -v "$candidate"; return 0; fi
    fi
  done
  if command -v brew >/dev/null 2>&1; then
    for version in 3.13 3.12 3.11; do
      prefix="$(brew --prefix "python@${version}" 2>/dev/null || true)"
      candidate="$prefix/bin/python${version}"
      if [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
    done
  fi
  return 1
}
PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then echo "inhHAB requires Python 3.11+." >&2; exit 1; fi
if [ -x .venv/bin/python ]; then
  if ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then rm -rf .venv; fi
fi
if [ ! -d .venv ]; then "$PYTHON_BIN" -m venv .venv; fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ! command -v ffmpeg >/dev/null 2>&1; then echo "WARNING: ffmpeg is not installed." >&2; fi
export PORT="${PORT:-1616}"
export HOST="${HOST:-127.0.0.1}"
if [ "$HOST" = "0.0.0.0" ]; then
  echo "[inhHAB] WARNING: server is exposed on all network interfaces."
  echo "[inhHAB] Access: http://SERVER_IP:${PORT}"
fi
exec python -u index.py
