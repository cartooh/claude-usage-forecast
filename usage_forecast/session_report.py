"""Per-session token usage of this machine, and a replay of the unusual-usage rules.

Standard library only (no sheet access), so it runs on any machine with the repo:

    python3 -m usage_forecast.session_report                       # last 24 hours
    python3 -m usage_forecast.session_report --from 10/29 --to 10/31 --hourly
    python3 -m usage_forecast.session_report --from 2026-10-01 --backtest

Times are JST. Costs are API-list-price equivalents from config.json.
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import anomaly, tokens
from .config import load_config

JST = timezone(timedelta(hours=9))
H = 3600


def parse_when(text, now):
    """'2026-10-29', '2026-10-29 15:00', '10/29', '10/29 15:00' (JST) -> unix seconds."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%m/%d %H:%M", "%m/%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            year = datetime.fromtimestamp(now, JST).year
            dt = dt.replace(year=year)
            if dt.replace(tzinfo=JST).timestamp() > now + 86400:
                dt = dt.replace(year=year - 1)
        return dt.replace(tzinfo=JST).timestamp()
    raise argparse.ArgumentTypeError("日時の形式が不明です: %s" % text)


def fmt(ts):
    return datetime.fromtimestamp(ts, JST).strftime("%m/%d %H:%M")


def report(units, human, prices, a, b, top, hourly, out=print):
    sessions = defaultdict(list)
    agents = defaultdict(set)
    for (project, session, agent), msgs in units.items():
        sel = [m for m in msgs if a <= m.ts <= b]
        if sel:
            sessions[(project, session)].extend(sel)
            if agent:
                agents[(project, session)].add(agent)
    rows = []
    for key, msgs in sessions.items():
        msgs.sort(key=lambda m: m.ts)
        cost = [tokens.msg_cost(m, prices) or 0.0 for m in msgs]
        per_h = defaultdict(float)
        for m, c in zip(msgs, cost):
            per_h[int(m.ts // H)] += c
        rows.append((sum(cost), key, msgs, per_h))
    rows.sort(key=lambda r: -r[0])
    out("期間 %s 〜 %s  セッション %d  合計 API換算 $%.1f" % (fmt(a), fmt(b), len(rows), sum(r[0] for r in rows)))
    out("%-24s %-8s %4s %6s %8s %8s %8s %7s %-11s %-11s %s" % (
        "プロジェクト", "セッション", "子", "呼出", "API$", "最大$/h", "書直M", "出力k", "最初", "最後", "最後の人の入力"))
    for cost, (project, session), msgs, per_h in rows[:top]:
        cw = sum(m.cache_write_5m + m.cache_write_1h for m in msgs) / 1e6
        outk = sum(m.output for m in msgs) / 1e3
        hu = human.get(session)
        out("%-24s %-8s %4d %6d %8.1f %8.1f %8.1f %7.0f %-11s %-11s %s" % (
            anomaly._project_label(project)[:24], session[:8], len(agents[(project, session)]), len(msgs),
            cost, max(per_h.values()), cw, outk, fmt(msgs[0].ts), fmt(msgs[-1].ts), fmt(hu) if hu else "-"))
        if hourly:
            out("    " + " ".join("%s=$%.0f" % (datetime.fromtimestamp(h * H, JST).strftime("%d/%H"), v)
                                  for h, v in sorted(per_h.items())))


def backtest(units, prices, a, b, rules, machine, step_min=30, out=print):
    """Replay the session rules every `step_min` minutes (with the cooldown) and list the alerts."""
    last = {}
    fired = []
    t = a
    while t <= b:
        for al in anomaly.session_alerts(units, {}, prices, t, rules, machine):
            if t - last.get(al["key"], -1e18) >= rules["cooldown_hours"] * H:
                last[al["key"]] = t
                fired.append((t, al))
        t += step_min * 60
    out("ルールの試し打ち (%s 〜 %s、%d分ごと): %d件" % (fmt(a), fmt(b), step_min, len(fired)))
    for t, al in fired:
        out("  %s  %-8s %-40s %s" % (fmt(t), anomaly.KIND[al["kind"]], al["target"][:40], al["detail"]))
    return fired


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="since", help="開始 (JST)。既定は24時間前")
    ap.add_argument("--to", dest="until", help="終了 (JST)。既定は現在")
    ap.add_argument("--top", type=int, default=15, help="表示するセッション数")
    ap.add_argument("--hourly", action="store_true", help="1時間ごとのAPI換算も表示")
    ap.add_argument("--backtest", action="store_true", help="異常検知ルールを期間内で試し打ち")
    ap.add_argument("--config", help="path to config.json")
    args = ap.parse_args(argv)
    now = time.time()
    cfg = load_config(args.config)
    b = parse_when(args.until, now) if args.until else now
    if args.until and len(args.until.strip()) <= 10:
        b += 86400 - 1  # a date alone means the whole day
    a = parse_when(args.since, now) if args.since else b - 86400
    rules = anomaly.rules_from(cfg.anomaly)
    units, human = anomaly.scan_recent(cfg.projects_dir, a - rules["session_sustained_hours"] * H)
    print("端末: %s" % cfg.machine_name)
    report(units, human, cfg.token_prices, a, b, args.top, args.hourly)
    if args.backtest:
        print()
        backtest(units, cfg.token_prices, a, b, rules, cfg.machine_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
