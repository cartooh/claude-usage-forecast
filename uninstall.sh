#!/usr/bin/env bash
# Remove claude-usage-forecast from this machine.
#
#   ./uninstall.sh [--remove-data] [--purge]
#
#   (default)      remove the cron entry and restore the original statusline
#   --remove-data  also delete this machine's rows from the 端末別集計 sheet
#                  (rows already in ログ are kept: they are shared history)
#   --purge        also delete .venv, var/ and .env in this folder
#                  (the service-account key in ~/.config/claude-usage-forecast is kept)
set -euo pipefail
cd "$(dirname "$0")"

REMOVE_DATA=0; PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --remove-data) REMOVE_DATA=1; shift ;;
    --purge)       PURGE=1; shift ;;
    -h|--help)     sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

"$PY" -m usage_forecast.setup cron uninstall
"$PY" -m usage_forecast.setup statusline uninstall
if [ "$REMOVE_DATA" = 1 ]; then
  "$PY" -m usage_forecast.setup remove-machine-data
fi
if [ "$PURGE" = 1 ]; then
  rm -rf .venv var .env
  echo "purged .venv, var/ and .env"
fi
echo "Done."
