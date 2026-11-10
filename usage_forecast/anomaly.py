"""Unusual-usage detection: runaway sessions, wait loops, pace surges, silent machines.

Session rules look at this machine's transcripts only (each machine checks its
own); the weekly-pace and silent-machine rules use the shared sheet, so every
machine sees them and the alert sheet keeps one notification across machines.

Rules (thresholds in config.json "anomaly"):
  sustained  one session (subagents included) costs >= session_cost_per_hour
             in each of the last session_sustained_hours hours
  burst      one session costs >= session_burst_per_hour in the last hour
  wait_loop  one transcript (main or a subagent) rewrites >= wait_loop_cache_write_mtok
             million cache tokens in the last hour while writing < wait_loop_max_output_ktok
             thousand output tokens: polling with waits longer than the cache lifetime,
             each wake-up re-caching the whole context
  pace       the weekly usage grows >= pace_factor x the even pace (100%/168h) over
             the last pace_window_hours hours (by at least pace_min_points points);
             recorded but not pushed by default, as busy working days look the same
  silent     another machine's per-machine row has not been updated for machine_stale_hours
"""
import json
import logging
import os
import time
from collections import defaultdict

from . import predict, tokens
from .timeutil import cell_to_ts, fmt_jst

log = logging.getLogger(__name__)

H = 3600
DEFAULTS = {
    "enabled": True,
    "session_cost_per_hour": 15.0,
    "session_sustained_hours": 3,
    "session_burst_per_hour": 40.0,
    "wait_loop_cache_write_mtok": 2.0,
    "wait_loop_max_output_ktok": 10.0,
    "wait_loop_min_calls": 3,
    "pace_factor": 3.0,
    "pace_window_hours": 6.0,
    "pace_min_points": 3.0,
    "machine_stale_hours": 3.0,
    "machine_ignore_after_days": 7.0,
    "cooldown_hours": 24.0,
    "pace_cooldown_hours": 12.0,
    "statusline_hours": 3.0,
    # kinds pushed through NOTIFY_METHODS; the others are only recorded in the sheet and the
    # statusline. The weekly pace also rises on busy working days, so it is not pushed by default.
    "push_kinds": ["wait_loop", "sustained", "burst", "silent"],
}
KIND = {
    "sustained": "高額状態の継続",
    "burst": "急増",
    "wait_loop": "待機ループ",
    "pace": "週使用量の急増",
    "silent": "端末の集計停止",
}


def rules_from(raw):
    r = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in r and not k.startswith("_"):
            d = DEFAULTS[k]
            r[k] = bool(v) if isinstance(d, bool) else list(v) if isinstance(d, list) else type(d)(v)
    return r


# ------------------------------------------------------------- transcripts
def _is_human(d):
    """A prompt typed by a person (not a tool result, task notification or meta entry)."""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return False
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip()) and not c.lstrip().startswith("<")
    if isinstance(c, list):
        return any(isinstance(x, dict) and x.get("type") == "text"
                   and not str(x.get("text", "")).lstrip().startswith("<") for x in c)
    return False


def scan_recent(projects_dir, since_ts):
    """Messages since `since_ts` by transcript, and the last human prompt time per session.

    Returns ({(project, session, agent): [Msg]}, {session: last_human_ts}).
    """
    units = defaultdict(dict)
    human = {}
    for base, path in tokens.transcript_paths(projects_dir, since_ts):
        project, session, agent = tokens.source_of(path, base)
        try:
            f = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                is_asst = '"assistant"' in line and '"usage"' in line
                is_user = not agent and '"user"' in line and '"tool_result"' not in line
                if not (is_asst or is_user):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if is_user and _is_human(d):
                    try:
                        ts = tokens._iso_to_ts(d.get("timestamp"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    human[session] = max(human.get(session, 0), ts)
                    continue
                got = tokens.msg_from_entry(d) if is_asst else None
                if got is None or got[1].ts < since_ts:
                    continue
                key, m = got
                m.project, m.session, m.agent = project, session, agent
                tokens.merge_msg(units[(project, session, agent)], key, m)
    return {k: sorted(v.values(), key=lambda m: m.ts) for k, v in units.items()}, human


def _cost(msgs, prices, a, b):
    return sum(tokens.msg_cost(m, prices) or 0.0 for m in msgs if a < m.ts <= b)


def _project_label(project):
    """'-home-me-work-foo' -> 'foo' (last path part, best effort)."""
    for prefix in ("-mnt-data-", "-home-"):
        if project.startswith(prefix):
            parts = project.split("-work-", 1)
            return parts[1] if len(parts) == 2 else project.lstrip("-")
    return project.lstrip("-")


def session_alerts(units, human, prices, now, rules, machine):
    """Pure: alerts for this machine's sessions. Each alert is a dict (see _alert)."""
    out = []
    by_session = defaultdict(list)
    for (project, session, agent), msgs in units.items():
        by_session[(project, session)].extend(msgs)
        recent = [m for m in msgs if now - H < m.ts <= now]
        cw = sum(m.cache_write_5m + m.cache_write_1h for m in recent)
        outk = sum(m.output for m in recent)
        if (len(recent) >= rules["wait_loop_min_calls"]
                and cw >= rules["wait_loop_cache_write_mtok"] * 1e6
                and outk < rules["wait_loop_max_output_ktok"] * 1e3):
            c = _cost(recent, prices, now - H, now)
            out.append(_alert(
                "wait_loop", machine, project, session, agent, human.get(session), now,
                "直近1時間 呼び出し%d回 キャッシュ書き直し%.1fMトークン 出力%dトークン API換算$%.0f"
                % (len(recent), cw / 1e6, outk, c), "$%.0f/h" % c))
    n = int(rules["session_sustained_hours"])
    for (project, session), msgs in by_session.items():
        hourly = [_cost(msgs, prices, now - (i + 1) * H, now - i * H) for i in range(max(n, 1))]
        if n >= 1 and all(c >= rules["session_cost_per_hour"] for c in hourly[:n]):
            out.append(_alert(
                "sustained", machine, project, session, "", human.get(session), now,
                "直近%d時間のAPI換算/時: %s" % (n, ", ".join("$%.0f" % c for c in reversed(hourly[:n]))),
                "$%.0f/h×%dh" % (min(hourly[:n]), n)))
        elif hourly[0] >= rules["session_burst_per_hour"]:
            out.append(_alert("burst", machine, project, session, "", human.get(session), now,
                              "直近1時間 API換算$%.0f" % hourly[0], "$%.0f/h" % hourly[0]))
    return out


def _alert(kind, machine, project, session, agent, last_human, now, detail, amount):
    target = "%s %s" % (_project_label(project), session[:8])
    if agent:
        target += " / " + agent
    if last_human:
        detail += " / 最後の人の入力から%.1f時間" % ((now - last_human) / H)
    key = "%s:%s:%s:%s" % (kind, machine, session, agent)
    return {"key": key, "kind": kind, "machine": machine, "target": target, "detail": detail,
            "short": "%s %s %s" % (KIND[kind], machine, amount), "scope": "machine"}


# --------------------------------------------------------- account-wide rules
def pace_alert(log_rows, now, rules, machine):
    """Weekly usage growing much faster than the even pace (any machine, web or cloud)."""
    pts = []
    for r in log_rows:
        try:
            obs, resets, used = cell_to_ts(r[1]), cell_to_ts(r[2]), float(r[3])
        except (ValueError, TypeError, IndexError):
            continue
        pts.append((obs, resets, used))
    if not pts:
        return None
    pts.sort()
    t1, resets, u1 = pts[-1]
    if now - t1 > 2 * H:
        return None
    window = rules["pace_window_hours"] * H
    base = [p for p in pts if p[0] <= t1 - window and predict.same_week(p[1], resets)]
    if not base:
        return None
    t0, _, u0 = base[-1]
    if t1 - t0 > window + H or u1 < u0:
        return None
    rate = (u1 - u0) / ((t1 - t0) / H)
    even = 100.0 / predict.WEEK_HOURS
    if u1 - u0 < rules["pace_min_points"] or rate < rules["pace_factor"] * even:
        return None
    return {"key": "pace:%d" % int(resets), "kind": "pace", "machine": machine, "target": "全端末",
            "detail": "%s→%s の%.1f時間で %g%%→%g%% (%.1f%%/時、均等ペースの%.1f倍)"
                      % (fmt_jst(t0)[5:16], fmt_jst(t1)[11:16], (t1 - t0) / H, u0, u1, rate, rate / even),
            "short": "%s %.1f%%/h" % (KIND["pace"], rate), "scope": "account"}


def silent_machine_alerts(machine_rows, now, rules, machine):
    if rules["machine_stale_hours"] <= 0:
        return []
    last = {}
    for r in machine_rows:
        try:
            ts = cell_to_ts(r[2])
        except (ValueError, TypeError, IndexError):
            continue
        last[str(r[0])] = max(last.get(str(r[0]), 0), ts)
    out = []
    for name, ts in sorted(last.items()):
        age = now - ts
        if name == machine or age < rules["machine_stale_hours"] * H \
                or age > rules["machine_ignore_after_days"] * 86400:
            continue
        out.append({"key": "silent:%s" % name, "kind": "silent", "machine": name, "target": name,
                    "detail": "最終更新 %s (%.1f時間前)。cron・電源・ログイン切れを確認してください" % (fmt_jst(ts)[5:16], age / H),
                    "short": "%s %s" % (KIND["silent"], name), "scope": "account"})
    return out


# ------------------------------------------------------------- dispatch
def due(alerts, sent_rows, local_sent, now, rules):
    """Drop alerts sent within their cooldown (by any machine for account-wide keys)."""
    last = dict(local_sent)
    for r in sent_rows:
        try:
            ts = cell_to_ts(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        k = str(r[5]) if len(r) > 5 else ""
        if k:
            last[k] = max(last.get(k, 0), ts)
    out = []
    for a in alerts:
        cd = rules["pace_cooldown_hours"] if a["kind"] == "pace" else rules["cooldown_hours"]
        if now - last.get(a["key"], 0) >= cd * H:
            out.append(a)
    return out


def alert_row(a, now):
    return [fmt_jst(now), a["machine"], KIND[a["kind"]], a["target"], a["detail"], a["key"]]


def write_statusline_file(path, sent_rows, new_alerts, now, hours):
    """Recent alerts (all machines) for the statusline hook to show."""
    items = []
    for r in sent_rows:
        try:
            ts = cell_to_ts(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if now - ts <= hours * H:
            items.append({"at": int(ts), "short": "%s %s" % (r[2], r[1])})
    for a in new_alerts:
        items.append({"at": int(now), "short": a["short"]})
    items.sort(key=lambda x: -x["at"])
    data = {"updated": int(now), "expires_after_s": int(hours * H), "alerts": items[:5]}
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


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


def check(cfg, client, notifier, now=None, dry_run=False, out=print):
    """Run every rule, record new alerts in the sheet, notify, update the statusline file.

    Returns the list of alerts that were (or in dry-run would be) sent.
    """
    rules = rules_from(cfg.anomaly)
    if not rules["enabled"]:
        return []
    now = time.time() if now is None else now
    lookback = max(rules["session_sustained_hours"], 1) * H
    units, human = scan_recent(cfg.projects_dir, now - lookback)
    alerts = session_alerts(units, human, cfg.token_prices, now, rules, cfg.machine_name)
    sent_rows = []
    if client is not None:
        try:
            a = pace_alert(client.read_log(), now, rules, cfg.machine_name)
            if a:
                alerts.append(a)
            alerts.extend(silent_machine_alerts(client.read_machines(), now, rules, cfg.machine_name))
            sent_rows = client.read_alerts()
        except Exception as e:
            log.error("anomaly: sheet read failed: %s %s", type(e).__name__, e)
    state_path = os.path.join(cfg.var_dir, "anomaly-state.json")
    local_state = _load_state(state_path)
    sent = local_state.setdefault("sent", {})
    new = due(alerts, sent_rows, sent, now, rules)
    for a in new:
        if dry_run:
            out("[dry-run] 異常検知: %s | %s | %s" % (a["short"], a["target"], a["detail"]))
            continue
        if client is not None:
            try:
                client.append_alert(alert_row(a, now))
            except Exception as e:
                log.error("anomaly: append failed: %s %s", type(e).__name__, e)
        if notifier.enabled and a["kind"] in rules["push_kinds"]:
            # project names stay in the sheet; the push text carries only machine, kind and amounts
            body = a["detail"] if a["scope"] == "account" else "%s\n%s" % (a["short"], a["detail"])
            notifier.send("Claude使用量: " + a["short"], body)
        log.warning("anomaly: %s | %s | %s", a["short"], a["target"], a["detail"])
        sent[a["key"]] = int(now)
    # forget keys long past any cooldown
    keep = max(rules["cooldown_hours"], rules["pace_cooldown_hours"]) * H * 2
    local_state["sent"] = {k: v for k, v in sent.items() if now - v < keep}
    if not dry_run:
        try:
            _save_state(state_path, local_state)
        except OSError as e:
            log.error("anomaly: cannot save state: %s", type(e).__name__)
    if not dry_run and cfg.alerts_file:
        try:
            write_statusline_file(cfg.alerts_file, sent_rows, new, now, rules["statusline_hours"])
        except OSError as e:
            log.error("anomaly: cannot write %s: %s", cfg.alerts_file, type(e).__name__)
    return new
