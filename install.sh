#!/usr/bin/env bash
# Set up claude-usage-forecast on this machine.
#
#   ./install.sh --spreadsheet-id ID --credentials /path/to/service-account.json [--machine NAME]
#                [--no-cron] [--no-statusline]
#
# Re-running is safe: every step checks what is already there.
set -euo pipefail
cd "$(dirname "$0")"

SHEET_ID=""; CREDS=""; MACHINE=""; DO_CRON=1; DO_STATUSLINE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --spreadsheet-id) SHEET_ID="$2"; shift 2 ;;
    --credentials)    CREDS="$2"; shift 2 ;;
    --machine)        MACHINE="$2"; shift 2 ;;
    --no-cron)        DO_CRON=0; shift ;;
    --no-statusline)  DO_STATUSLINE=0; shift ;;
    -h|--help)        sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

echo "== 1/6 checks"
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "python3 >= 3.9 is required" >&2; exit 1; }
command -v crontab >/dev/null || { echo "crontab is required" >&2; exit 1; }
command -v tmux >/dev/null || echo "warning: tmux not found; Fable usage and usage-credits will not be recorded"
command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ] || echo "warning: claude CLI not found in PATH"

echo "== 2/6 python environment"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then uv venv -q -p "$(command -v python3)" .venv
  else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null; then uv pip install -q -p .venv/bin/python -r requirements.txt
else .venv/bin/python -m pip install -q -r requirements.txt; fi
mkdir -p var && chmod 700 var

echo "== 3/6 settings (.env)"
ARGS=()
[ -n "$SHEET_ID" ] && ARGS+=(--spreadsheet-id "$SHEET_ID")
[ -n "$CREDS" ] && ARGS+=(--credentials "$CREDS")
[ -n "$MACHINE" ] && ARGS+=(--machine "$MACHINE")
.venv/bin/python -m usage_forecast.setup env ${ARGS[@]+"${ARGS[@]}"}

echo "== 4/6 spreadsheet access and sheets"
.venv/bin/python -m usage_forecast.collector --check-access
.venv/bin/python -m usage_forecast.setup sheets

echo "== 5/6 statusline"
if [ "$DO_STATUSLINE" = 1 ]; then .venv/bin/python -m usage_forecast.setup statusline install; else echo "skipped"; fi

echo "== 6/6 cron"
chmod +x bin/run.sh
if [ "$DO_CRON" = 1 ]; then .venv/bin/python -m usage_forecast.setup cron install; else echo "skipped"; fi

echo
.venv/bin/python -m usage_forecast.setup status
echo
echo "Done. Values are recorded after Claude Code's next response on this machine."
echo "Run now:  bin/run.sh --now   /   check:  .venv/bin/python -m usage_forecast.collector --dry-run"
