"""Hourly token use per machine, so a sudden rise on one PC shows up in the sheet.

Each machine appends one row per completed hour in which it used Claude Code
(sheet 端末別1時間), with the session that cost the most in that hour. Any
machine then rebuilds the pivot on 端末別グラフ (hours x machines, and days x
machines) that the two stacked charts there read.
"""
import logging
import os
import json
import time
from collections import defaultdict

from . import anomaly, tokens
from .timeutil import cell_to_ts, fmt_jst, to_jst

log = logging.getLogger(__name__)

H = 3600
BACKFILL_HOURS = 14 * 24
SETTLE_SEC = 300          # an hour is written once it has been over for 5 minutes
MAX_MACHINES = 8          # chart columns; smaller machines are merged into その他
OTHERS = "その他"


def hourly_rows(msgs, prices, machine, first_hour, last_hour):
    """Rows for the completed hours [first_hour, last_hour] (hour starts, unix) that had any use."""
    buckets = defaultdict(lambda: {"cost": 0.0, "nc": 0, "cr": 0, "ids": 0, "sess": defaultdict(float)})
    for m in msgs.values():
        h = int(m.ts // H) * H
        if not first_hour <= h <= last_hour:
            continue
        b = buckets[h]
        c = tokens.msg_cost(m, prices) or 0.0
        b["cost"] += c
        b["nc"] += m.input + m.output + m.cache_write_5m + m.cache_write_1h
        b["cr"] += m.cache_read
        b["ids"] += 1
        b["sess"][(m.project, m.session)] += c
    rows = []
    for h in sorted(buckets):
        b = buckets[h]
        (project, session), top = max(b["sess"].items(), key=lambda kv: kv[1])
        rows.append([fmt_jst(h), machine, round(b["cost"], 2), round(b["nc"] / 1e6, 3), round(b["cr"] / 1e6, 2),
                     b["ids"], "%s %s" % (anomaly._project_label(project), session[:8]), round(top, 2)])
    return rows


def build_pivot(long_rows, now, hours=168, days=30):
    """(header, hourly, daily) for the chart sheet from the rows of 端末別1時間.

    hourly: the last `hours` completed hours, one row per hour, API $ per machine (0 when idle).
    daily:  the last `days` JST days, one row per day.
    """
    per_hour = defaultdict(lambda: defaultdict(float))
    per_day = defaultdict(lambda: defaultdict(float))
    totals = defaultdict(float)
    end_h = int(now // H) * H - H
    start_h = end_h - (hours - 1) * H
    day0 = to_jst(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - (days - 1) * 86400
    for r in long_rows:
        try:
            ts, name, cost = cell_to_ts(r[0]), str(r[1]), float(r[2])
        except (ValueError, TypeError, IndexError):
            continue
        if ts >= day0:
            d = to_jst(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            per_day[d][name] += cost
            totals[name] += cost
        if start_h <= ts <= end_h:
            per_hour[int(ts)][name] += cost
    ranked = sorted(totals, key=lambda n: -totals[n])
    names = ranked if len(ranked) <= MAX_MACHINES else ranked[:MAX_MACHINES - 1] + [OTHERS]
    shown = set(names)

    def row(label, vals):
        out = [label] + [round(vals.get(n, 0.0), 2) for n in names if n != OTHERS]
        if OTHERS in shown:
            out.append(round(sum(v for k, v in vals.items() if k not in shown), 2))
        return out
    hourly = [row(fmt_jst(h), per_hour.get(h, {})) for h in range(start_h, end_h + 1, H)]
    daily = [row(fmt_jst(day0 + i * 86400)[:10], per_day.get(day0 + i * 86400, {})) for i in range(days)]
    return ["時刻"] + names, hourly, daily


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, d):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def update(cfg, client, now=None, dry_run=False, out=print):
    """Append this machine's finished hours and refresh the per-machine charts. Returns rows added."""
    now = time.time() if now is None else now
    state_path = os.path.join(cfg.var_dir, "hourly-state.json")
    state = _load_state(state_path)
    last_done = int((now - SETTLE_SEC) // H) * H - H      # newest hour that is over and settled
    first = int(state.get("last_hour", last_done - BACKFILL_HOURS * H)) + H
    first = max(first, last_done - BACKFILL_HOURS * H + H)
    rows = []
    if first <= last_done:
        msgs = tokens.scan(cfg.projects_dir, since_ts=first)
        rows = hourly_rows(msgs, cfg.token_prices, cfg.machine_name, first, last_done)
    if dry_run:
        for r in rows[-5:]:
            out("[dry-run] 端末別1時間: %s" % r)
        return rows
    if client is None:
        return []
    if rows:
        client.append_hourly(rows)
        state["chart_pending"] = True
    if first <= last_done:
        state["last_hour"] = last_done
        _save_state(state_path, state)
    # Rebuild only after new rows (about once an hour per machine, to stay inside the Sheets API
    # quota), and again on the next run if that rebuild failed.
    if state.get("chart_pending"):
        header, hourly, daily = build_pivot(client.read_hourly(), now)
        client.write_machine_chart(header, hourly, daily)
        state["chart_pending"] = False
        _save_state(state_path, state)
    return rows
