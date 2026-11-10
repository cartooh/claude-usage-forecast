"""Cron collector: state file -> Google Sheet (+ optional notifications).

Usage:
    python -m usage_forecast.collector [--dry-run] [--config PATH] [--state-file PATH]
"""
import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from . import anomaly, predict, tokens
from .config import load_config
from .notify import Notifier
from .sheets import LOG_HEADER, SheetError, format_percent
from .timeutil import cell_to_ts, fmt_jst, to_jst

log = logging.getLogger("usage_forecast")

REMARK_STALE = "要更新"
REMARK_RESET_MISMATCH = "リセット時刻不一致"
REMARK_WEEK_EXPIRED = "週リセット経過のため無効(次の観測待ち)"
REMARK_SETTINGS_UNREADABLE = "ペース表設定読取不可"
TOKEN_COLS = 9  # log columns P..X
REMARK_DROP = "週途中で使用量が減少(上限リセット/プラン変更?)"


# ---------------------------------------------------------------- state files
def read_usage_state(path):
    """Return (observed_at, seven_day, five_hour) or None when unusable."""
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        log.warning("state file not found: %s", path)
        return None
    except Exception as e:
        log.error("state file unreadable: %s", type(e).__name__)
        return None
    obs = st.get("observed_at")
    sd = st.get("seven_day")
    if not isinstance(obs, (int, float)) or not isinstance(sd, dict):
        log.warning("state file has no observation yet")
        return None
    if not isinstance(sd.get("used_percentage"), (int, float)) or not isinstance(sd.get("resets_at"), (int, float)):
        log.warning("state file seven_day is malformed")
        return None
    fh = st.get("five_hour") if isinstance(st.get("five_hour"), dict) else None
    return int(obs), sd, fh


def read_model_usage(path, label, now, max_age_h):
    """Per-model weekly usage written by scripts/usage_probe.py -> (used, observed_at) or None."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        w = (d.get("windows") or {}).get(label)
        obs = d.get("observed_at")
        if not isinstance(w, dict) or not isinstance(obs, (int, float)):
            return None
        if now - obs > max_age_h * 3600:
            return None
        if isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] <= now:
            return None
        return float(w["used_percentage"]), int(obs)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("model usage file unreadable: %s", type(e).__name__)
        return None


def _round5min(ts):
    """The /usage screen shows reset times rounded down to the minute ("10:59am"); snap to 5 min."""
    return int(round(ts / 300.0)) * 300


def read_probe_observation(path, now, max_age_s, statusline_resets=None):
    """Weekly and 5-hour usage read from the /usage screen by scripts/usage_probe.py.

    Returns (observed_at, seven_day, five_hour) like read_usage_state, or None when
    the file is missing, too old, or has no weekly window. The reset time is snapped
    to the statusline's exact value when they agree within an hour.
    """
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("usage screen file unreadable: %s", type(e).__name__)
        return None
    obs = d.get("observed_at")
    windows = d.get("windows") or {}
    week = windows.get("all models")
    if not isinstance(obs, (int, float)) or now - obs > max_age_s or not isinstance(week, dict):
        return None
    if not isinstance(week.get("used_percentage"), (int, float)) or not isinstance(week.get("resets_at"), (int, float)):
        return None
    resets = int(week["resets_at"])
    if statusline_resets is not None and abs(resets - statusline_resets) <= 3600:
        resets = int(statusline_resets)
    else:
        resets = _round5min(resets)
    seven = {"used_percentage": week["used_percentage"], "resets_at": resets}
    five = None
    ses = windows.get("session")
    if isinstance(ses, dict) and isinstance(ses.get("used_percentage"), (int, float)) \
            and isinstance(ses.get("resets_at"), (int, float)):
        five = {"used_percentage": ses["used_percentage"], "resets_at": _round5min(ses["resets_at"])}
    return int(obs), seven, five


def read_usage_credits(path, now, max_age_h):
    """'Usage credits' (pay-as-you-go) text from the /usage screen, or ''."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if now - d.get("observed_at", 0) > max_age_h * 3600:
            return ""
        uc = d.get("usage_credits") or {}
        return str(uc.get("text") or "")
    except Exception:
        return ""


def _num(v):
    try:
        return None if v in ("", None) else float(v)
    except (TypeError, ValueError):
        return None


def exhausted_intervals(log_rows):
    """Time ranges in which a plan limit was at 100% (from log rows).

    Weekly: from the first observation at >=100% until that week's reset.
    5-hour: from each observation at >=100% until that window's reset.
    """
    out = []
    first_full = {}
    for r in log_rows:
        try:
            obs, resets = cell_to_ts(r[1]), cell_to_ts(r[2])
        except (ValueError, TypeError, IndexError):
            continue
        if obs is None or resets is None:
            continue
        used = _num(r[3])
        if used is not None and used >= 100:
            first_full[resets] = min(first_full.get(resets, obs), obs)
        five = _num(r[9]) if len(r) > 10 else None
        if five is not None and five >= 100:
            try:
                five_reset = cell_to_ts(r[10])
            except (ValueError, TypeError):
                five_reset = None
            if five_reset and five_reset > obs:
                out.append((obs, five_reset))
    out += [(start, resets) for resets, start in first_full.items()]
    return out


def machine_row(cfg, msgs, resets, until_ts, intervals):
    """This machine's week-to-date totals as a row of the per-machine sheet."""
    week_start = resets - predict.WEEK_HOURS * 3600
    t_all = tokens.summarize(msgs, week_start, until_ts, cfg.token_prices)
    t_model = tokens.summarize(msgs, week_start, until_ts, cfg.token_prices, model_contains=cfg.model_label)
    t_cap = tokens.summarize(msgs, week_start, until_ts, cfg.token_prices,
                             predicate=tokens.exhausted_predicate(intervals))
    return [cfg.machine_name, fmt_jst(resets), fmt_jst(until_ts),
            round(t_all.non_cache_read / 1e6, 3), round(t_all.cache_read / 1e6, 1), round(t_all.cost, 2),
            round(t_model.cost, 2), round(t_cap.cost, 2), ",".join(sorted(t_all.unpriced_models))]


def aggregate_machines(machine_rows, own_row, resets):
    """Sum the week's rows of all machines; this machine's fresh row replaces its stored one."""
    agg = {"machines": [], "non_cache_read_m": 0.0, "cache_read_m": 0.0, "cost": 0.0,
           "model_cost": 0.0, "cap_cost": 0.0, "unpriced": set()}
    rows = [r for r in machine_rows if str(r[0]) != str(own_row[0])] + [own_row]
    for r in rows:
        try:
            if not predict.same_week(cell_to_ts(r[1]), resets):
                continue
        except (ValueError, TypeError):
            continue
        agg["machines"].append(str(r[0]))
        for k, i in (("non_cache_read_m", 3), ("cache_read_m", 4), ("cost", 5), ("model_cost", 6),
                     ("cap_cost", 7)):
            agg[k] += _num(r[i]) or 0.0
        if len(r) > 8 and str(r[8]).strip():
            agg["unpriced"].update(str(r[8]).split(","))
    return agg


def token_columns(cfg, agg, used, model_used, credits_text):
    """The 9 token/cost columns of a log row (totals of all machines), plus remarks."""
    remarks = []
    if agg["unpriced"]:
        remarks.append("単価未設定: " + ",".join(sorted(agg["unpriced"])))
    per_tok = per_cost = model_per_cost = ""
    if used is not None and used >= cfg.ratio_min_used:
        per_tok = round(agg["non_cache_read_m"] / used, 3)
        per_cost = round(agg["cost"] / used, 3)
    if model_used is not None and model_used >= cfg.ratio_min_used:
        model_per_cost = round(agg["model_cost"] / model_used, 3)
    cols = [
        round(agg["non_cache_read_m"], 3), round(agg["cache_read_m"], 1), round(agg["cost"], 2),
        per_tok, per_cost, round(agg["model_cost"], 2), model_per_cost,
        round(agg["cap_cost"], 2), credits_text,
    ]
    return cols, remarks


def sync_machine_tokens(cfg, client, resets, now, log_rows, dry_run):
    """Tally this machine's tokens, store them in the per-machine sheet, return the all-machine sum."""
    msgs = tokens.scan(cfg.projects_dir, since_ts=resets - (predict.WEEK_HOURS + 24) * 3600)
    own = machine_row(cfg, msgs, resets, min(now, resets), exhausted_intervals(log_rows))
    stored = []
    if client is not None:
        if not dry_run:
            client.upsert_machine(own)
        stored = client.read_machines()
    return aggregate_machines(stored, own, resets)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.error("cannot read %s (%s); starting fresh", path, type(e).__name__)
        return default


def save_json_atomic(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- core logic
def chart_rows(log_rows, now, days=14.0, min_elapsed_h=12.0):
    """Data for the 推移グラフ sheet (A:I), built from log rows.

    - only the last `days` days and rows that have a usage value
    - at each weekly reset: usage gets a gap, the even-pace line drops 100 -> 0
    - forecasts are left blank while the week is younger than `min_elapsed_h`
    - column G: per-model (Fable) weekly usage when it was logged
    - columns H, I: tokens (M, excl. cache reads) and API-equivalent $ per 1% of weekly usage
    """
    pts = []
    for r in log_rows:
        try:
            obs, resets = cell_to_ts(r[1]), cell_to_ts(r[2])
            if obs is None or resets is None or r[3] in ("", None):
                continue
            if obs < now - days * 86400:
                continue
            ext = lambda i: r[i] if len(r) > i else ""
            pts.append((obs, resets, float(r[3]), r[7], r[8], ext(13), ext(18), ext(19)))
        except (ValueError, TypeError, IndexError):
            continue
    pts.sort(key=lambda p: p[0])
    out = []
    prev_resets = None
    for obs, resets, used, fa, fr, model, per_tok, per_cost in pts:
        if prev_resets is not None and not predict.same_week(prev_resets, resets):
            t = fmt_jst(prev_resets)
            out.append([t, "", 100, "", "", 100, "", "", ""])
            out.append([t, "", 0, "", "", 100, "", "", ""])
        prev_resets = resets
        elapsed = predict.elapsed_hours(obs, resets)
        pace_line = round(max(0.0, min(100.0, elapsed / predict.WEEK_HOURS * 100)), 2)
        show = elapsed >= min_elapsed_h
        out.append([fmt_jst(obs), used, pace_line, fa if show else "", fr if show else "", 100,
                    model if model is not None else "", per_tok, per_cost])
    return out


def history_from_log(rows):
    """Parse log rows into predict.Point list (skips rows without usage)."""
    pts = []
    for r in rows:
        try:
            obs = cell_to_ts(r[1])
            resets = cell_to_ts(r[2])
            used = r[3]
            if obs is None or resets is None or used in ("", None):
                continue
            pts.append(predict.Point(obs, float(used), resets))
        except (ValueError, TypeError, IndexError):
            continue
    return pts


def already_logged(rows, observed_at, resets_at, expired=False):
    """Same observation already in the log. A valid row and the 'week expired'
    row (blank usage) for the same observation are tracked separately."""
    for r in rows:
        try:
            if (cell_to_ts(r[1]) == observed_at and cell_to_ts(r[2]) == resets_at
                    and (r[3] in ("", None)) == expired):
                return True
        except (ValueError, TypeError, IndexError):
            continue
    return False


def reset_mismatch(resets_at, settings):
    """True if the pace sheet's weekday/hour disagree with the measured resets_at (JST)."""
    wd, hr = settings.get("reset_weekday"), settings.get("reset_hour")
    try:
        wd, hr = int(float(wd)), float(hr)
    except (TypeError, ValueError):
        return None
    dt = to_jst(resets_at)
    return not (dt.isoweekday() == wd and dt.hour == hr and dt.minute == 0)


def _r(x, nd):
    return "" if x is None else round(x, nd)


def build_row(now, observed_at, seven, five, history, settings, stale_hours, model=None):
    """Return (row, forecast_or_None, remarks, valid)."""
    resets = int(seven["resets_at"])
    used = float(seven["used_percentage"])
    freshness = (now - observed_at) / 3600.0
    remarks = []
    five_used = five_reset = ""
    if five and isinstance(five.get("resets_at"), (int, float)) and five["resets_at"] > observed_at:
        five_used = five.get("used_percentage", "")
        five_reset = fmt_jst(five["resets_at"])

    if not predict.is_valid_week(observed_at, resets, now):
        remarks.append(REMARK_WEEK_EXPIRED)
        row = [fmt_jst(now), fmt_jst(observed_at), fmt_jst(resets), "", "", "", "", "", "",
               "", "", round(freshness, 2), " / ".join(remarks), "", ""]
        return row, None, remarks, False

    fc = predict.compute(predict.Point(observed_at, used, resets), history)
    prev = [p for p in history
            if predict.same_week(p.resets_at, resets) and p.observed_at < observed_at]
    if prev and predict.is_drop(max(prev, key=lambda p: p.observed_at).used, used):
        remarks.append(REMARK_DROP)
    if freshness > stale_hours:
        remarks.append(REMARK_STALE)
    if settings is None:
        remarks.append(REMARK_SETTINGS_UNREADABLE)
    else:
        mm = reset_mismatch(resets, settings)
        if mm is None:
            remarks.append(REMARK_SETTINGS_UNREADABLE)
        elif mm:
            remarks.append(REMARK_RESET_MISMATCH)
    row = [
        fmt_jst(now), fmt_jst(observed_at), fmt_jst(resets), used,
        round(fc.elapsed_h, 2), _r(fc.avg_pace, 4), _r(fc.recent_pace, 4),
        _r(fc.forecast_avg, 1), _r(fc.forecast_recent, 1),
        five_used, five_reset, round(freshness, 2), " / ".join(remarks),
        model[0] if model else "", fmt_jst(model[1]) if model else "",
    ]
    return row, fc, remarks, True


def due_notifications(used, fc, resets_at, notified, forecast_threshold, used_thresholds):
    """Return [(level, subject, body)] not yet sent this week."""
    sent = set(notified.get(str(resets_at), []))
    out = []
    reset_txt = fmt_jst(resets_at)
    if fc is not None:
        hits = [(name, val, dep) for name, val, dep in (
            ("平均ペース法", fc.forecast_avg, fc.depletion_avg),
            ("直近ペース法", fc.forecast_recent, fc.depletion_recent),
        ) if val is not None and val >= forecast_threshold]
        level = "forecast>=%g" % forecast_threshold
        if hits and level not in sent:
            lines = ["%s: リセット時点の予測 %.1f%%%s" % (
                n, v, "、使い切り予定 %s" % fmt_jst(d) if d else "") for n, v, d in hits]
            out.append((level, "[Claude使用量] 週内に上限到達の見込み (現在 %g%%)" % used,
                        "\n".join(lines + ["週リセット: %s" % reset_txt])))
    for t in sorted(used_thresholds):
        level = "used>=%g" % t
        if used >= t and level not in sent:
            out.append((level, "[Claude使用量] 週間使用量が %g%% を超えました (現在 %g%%)" % (t, used),
                        "週リセット: %s" % reset_txt))
    return out


def run(cfg, client, notifier, now=None, dry_run=False, out=print):
    """One collector pass. `client` is a GSheetClient-like object (or None if unavailable)."""
    now = time.time() if now is None else now
    var = Path(cfg.var_dir)
    cstate_path = var / "collector-state.json"
    cstate = load_json(cstate_path, {})

    obs = read_usage_state(cfg.state_file)
    # The /usage screen (read every run on every machine) shows the same figure as the web
    # settings page (rounded, while the statusline floors) and does not depend on Claude Code
    # being used, so a recent reading is preferred. The statusline wins only when it is newer
    # and already shows a higher value (usage grew after the screen was read).
    probe = read_probe_observation(cfg.models_file, now, cfg.probe_max_age_min * 60,
                                   obs[1]["resets_at"] if obs else None)
    if probe is not None:
        statusline_ahead = (
            obs is not None and obs[0] > probe[0]
            and predict.same_week(obs[1]["resets_at"], probe[1]["resets_at"])
            and obs[1]["used_percentage"] > probe[1]["used_percentage"])
        if not statusline_ahead:
            obs = probe
    if obs is None:
        return "no-state"
    observed_at, seven, five = obs
    resets = int(seven["resets_at"])
    key = [observed_at, resets]
    freshness_h = (now - observed_at) / 3600.0

    expired = not predict.is_valid_week(observed_at, resets, now)
    if cstate.get("last_key") == key and not dry_run and not (
            expired and cstate.get("expired_key") != key):
        if expired:
            log.info("week reset passed at %s; waiting for a new observation", fmt_jst(resets))
        elif freshness_h > cfg.stale_hours:
            log.warning("%s: no new observation for %.1fh (observed %s)", REMARK_STALE,
                        freshness_h, fmt_jst(observed_at))
        else:
            log.info("no new observation (observed %s)", fmt_jst(observed_at))
        if client is not None and not expired:
            try:
                sync_machine_tokens(cfg, client, resets, now, client.read_log(), dry_run)
            except Exception as e:
                log.error("machine token sync failed: %s %s", type(e).__name__, e)
        return "unchanged"

    rows = []
    settings = None
    if client is not None:
        rows = client.read_log()
        try:
            settings = client.pace_settings()
        except Exception as e:
            log.error("cannot read pace settings: %s", e)

    if already_logged(rows, observed_at, resets, expired):
        log.info("observation %s already in log; skipping", fmt_jst(observed_at))
        if not dry_run:
            cstate["last_key"] = key
            if expired:
                cstate["expired_key"] = key
            save_json_atomic(cstate_path, cstate)
        return "duplicate"

    history = history_from_log(rows)
    used_now = float(seven["used_percentage"])
    same = [p for p in history if predict.same_week(p.resets_at, resets)]
    if not expired and same:
        latest = max(same, key=lambda p: p.observed_at)
        if used_now == latest.used and observed_at - latest.observed_at < cfg.min_row_interval_min * 60:
            # nothing new: keep the log to about one row per hour while usage is flat
            log.info("usage unchanged at %s%% since %s; no row", used_now, fmt_jst(latest.observed_at))
            if not dry_run:
                try:
                    sync_machine_tokens(cfg, client, resets, now, rows, dry_run)
                except Exception as e:
                    log.error("machine token sync failed: %s %s", type(e).__name__, e)
                cstate["last_key"] = key
                save_json_atomic(cstate_path, cstate)
            return "throttled"
        if used_now < latest.used and not predict.is_drop(latest.used, used_now):
            # another machine already logged a fresher (higher) value; ours is a stale render
            log.info("observed %s%% is older than the logged %s%% (another machine); not logged",
                     used_now, latest.used)
            if not dry_run:
                try:
                    sync_machine_tokens(cfg, client, resets, now, rows, dry_run)
                except Exception as e:
                    log.error("machine token sync failed: %s %s", type(e).__name__, e)
                cstate["last_key"] = key
                save_json_atomic(cstate_path, cstate)
            return "stale-skip"
    model = read_model_usage(cfg.models_file, cfg.model_label, now, cfg.model_max_age_h)
    row, fc, remarks, valid = build_row(now, observed_at, seven, five, history, settings,
                                        cfg.stale_hours, model)
    tok_cols = [""] * TOKEN_COLS
    if valid:
        try:
            agg = sync_machine_tokens(cfg, client, resets, now, rows + [row], dry_run)
            tok_cols, tok_remarks = token_columns(
                cfg, agg, used_now, model[0] if model else None,
                read_usage_credits(cfg.models_file, now, cfg.model_max_age_h))
            remarks += tok_remarks
            if dry_run:
                out("[dry-run] トークン合算の対象端末: %s" % ", ".join(agg["machines"]))
        except Exception as e:
            log.error("token tally failed: %s %s", type(e).__name__, e)
            remarks.append("トークン集計失敗")
        row[12] = " / ".join(remarks)
    row += tok_cols

    notes = []
    stale = REMARK_STALE in remarks
    if valid and not stale:
        notes = due_notifications(float(seven["used_percentage"]), fc, resets,
                                  cstate.get("notified", {}), cfg.forecast_threshold,
                                  cfg.used_thresholds)

    if dry_run:
        out("[dry-run] ペース表 %s ← %s" % (
            "B%s" % settings["usage_row"] if settings and settings.get("usage_row") else "(未特定)",
            format_percent(seven["used_percentage"]) if valid else "(書き込まない: 無効な週)"))
        out("[dry-run] ログ追記行:")
        for h, v in zip(LOG_HEADER, row):
            out("  %s: %s" % (h, v))
        if fc is not None:
            out("  (使い切り予定 平均: %s / 直近: %s)" % (
                fmt_jst(fc.depletion_avg) if fc.depletion_avg else "-",
                fmt_jst(fc.depletion_recent) if fc.depletion_recent else "-"))
        if stale:
            out("[dry-run] 観測が古いため予測通知は出さない(%s)" % REMARK_STALE)
        if not notes:
            out("[dry-run] 通知: なし")
        for level, subject, body in notes:
            out("[dry-run] 通知(%s, 送信方法=%s): %s\n%s" % (
                level, ",".join(notifier.methods) or "オフ", subject, body))
        return "dry-run"

    if client is None:
        raise SheetError("no sheet client")

    if valid:
        cell = client.write_current_usage(seven["used_percentage"], settings)
        log.info("wrote %s to %s!%s", format_percent(seven["used_percentage"]), cfg.pace_sheet, cell)
    client.append_log(row)
    log.info("appended log row: observed %s used %s remarks=%s", row[1], row[3], row[12] or "-")
    try:
        if client.write_chart_data(chart_rows(rows + [row], now, cfg.chart_days,
                                              cfg.chart_min_elapsed_h)):
            log.info("chart data refreshed")
    except Exception as e:  # the chart is secondary; never fail the run for it
        log.error("chart data update failed: %s %s", type(e).__name__, e)

    notified = cstate.setdefault("notified", {})
    for level, subject, body in notes:
        if notifier.enabled and notifier.send(subject, body):
            notified.setdefault(str(resets), []).append(level)
        elif not notifier.enabled:
            log.info("notification (disabled): %s", subject)
    # keep only recent weeks
    cstate["notified"] = {k: v for k, v in notified.items() if int(k) > now - 14 * 86400}
    cstate["last_key"] = key
    if not valid:
        cstate["expired_key"] = key
    save_json_atomic(cstate_path, cstate)
    return "written" if valid else "written-invalid"


# ---------------------------------------------------------------- entry point
def setup_logging(var_dir, verbose):
    Path(var_dir).mkdir(parents=True, exist_ok=True)
    os.chmod(var_dir, 0o700)
    logger = logging.getLogger("usage_forecast")
    logger.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(var_dir, "collector.log"), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(sh)


def check_access(cfg):
    """Read-only access check. Prints OK/NG with a hint; never prints secrets."""
    try:
        with open(cfg.credentials_path, encoding="utf-8") as f:
            email = json.load(f).get("client_email", "?")
    except Exception as e:
        print("NG: 認証情報ファイルを読めません (%s)" % type(e).__name__)
        return 1
    print("サービスアカウント: %s" % email)
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            cfg.credentials_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        r = AuthorizedSession(creds).get(
            "https://sheets.googleapis.com/v4/spreadsheets/%s" % cfg.spreadsheet_id,
            params={"fields": "properties(title,timeZone),sheets.properties.title"}, timeout=30)
    except Exception as e:
        print("NG: 接続できません (%s)" % type(e).__name__)
        return 1
    if r.status_code == 200:
        d = r.json()
        print("OK: 「%s」を開けました (タイムゾーン %s)" % (d["properties"]["title"], d["properties"]["timeZone"]))
        print("シート: %s" % ", ".join(s["properties"]["title"] for s in d.get("sheets", [])))
        return 0
    err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    msg = err.get("message", "")
    print("NG: HTTP %s %s" % (r.status_code, msg))
    if r.status_code == 403 and "has not been used" in msg:
        print("→ Google Sheets API が有効になっていません。")
    elif r.status_code == 403:
        print("→ スプレッドシートがこのアドレスに共有されていません(編集者で共有して保存してください)。")
    elif r.status_code == 404:
        print("→ SPREADSHEET_ID が違います。")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show planned writes/notifications only")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--state-file", help="override usage state file")
    ap.add_argument("-v", "--verbose", action="store_true", help="also log to stderr")
    ap.add_argument("--check-access", action="store_true",
                    help="only check that the service account can open the spreadsheet (read-only)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.check_access:
        return check_access(cfg)
    if args.state_file:
        cfg.state_file = os.path.expanduser(args.state_file)
    setup_logging(cfg.var_dir, args.verbose or args.dry_run)

    lock = open(os.path.join(cfg.var_dir, "collector.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("another collector is running; exit")
        return 0

    try:
        client = None
        try:
            from .sheets import GSheetClient
            client = GSheetClient(cfg)
        except Exception as e:
            if not args.dry_run:
                log.error("sheet connection failed: %s %s", type(e).__name__, e)
                return 1
            print("[dry-run] シートに接続できないため履歴なしで計算します: %s %s" % (type(e).__name__, e))
        notifier = Notifier(cfg.notify_methods)
        rc = 0
        try:
            result = run(cfg, client, notifier, dry_run=args.dry_run)
            log.info("result: %s", result)
        except Exception as e:
            log.exception("collector failed: %s", e)
            rc = 1
        try:
            found = anomaly.check(cfg, client, notifier, dry_run=args.dry_run)
            if found:
                log.info("anomaly alerts: %d", len(found))
        except Exception as e:
            log.exception("anomaly check failed: %s", e)
            rc = 1
        return rc
    except Exception as e:
        log.exception("collector failed: %s", e)
        return 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
