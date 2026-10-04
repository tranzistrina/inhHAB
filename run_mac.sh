#!/bin/sh
set -eu
cd "$(dirname "$0")"

if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Можно задать код настройки первым аргументом:
#   ./run_mac.sh MySetupCode
# Либо:
#   ADMIN_SETUP_CODE=MySetupCode ./run_mac.sh
#
# Если код не задан, приложение сгенерирует его и запишет в лог при первом запуске.
SETUP_CODE="${ADMIN_SETUP_CODE:-${1:-}}"

if [ -n "$SETUP_CODE" ]; then
  export ADMIN_SETUP_CODE="$SETUP_CODE"
  echo "[inhHAB] Используется заданный ADMIN_SETUP_CODE."
else
  echo "[inhHAB] ADMIN_SETUP_CODE не задан. При первом запуске код будет сгенерирован и показан в консоли."
fi

export PORT="${PORT:-1616}"
export HOST="${HOST:-127.0.0.1}"
export WAITRESS_THREADS="${WAITRESS_THREADS:-8}"

exec python index.py
