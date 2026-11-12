#!/bin/sh
# cron entry point: wait a random 0-239 s (so several machines do not hit the sheet at once),
# read /usage (Fable weekly, usage credits), then run the collector.
# Usage: bin/run.sh [--now]
cd "$(dirname "$0")/.." || exit 1
if [ "$1" != "--now" ]; then
  n=$(od -An -N1 -tu1 /dev/urandom | tr -d ' ')
  sleep $((n * 240 / 256))
fi
.venv/bin/python scripts/usage_probe.py >/dev/null 2>&1
.venv/bin/python -m usage_forecast.collector >/dev/null 2>&1
