import json
import os

import pytest

from usage_forecast import anomaly, tokens
from usage_forecast.config import Config
from usage_forecast.notify import Notifier
from usage_forecast.timeutil import fmt_jst

import statusline_hook as hook

H = 3600
NOW = 1793000000
PRICES = {"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write_5m": 5.0}}
RULES = anomaly.rules_from({})


def m(ts, out=0, cr=0, cw=0, model="claude-opus-5-5"):
    return tokens.Msg(ts, model, output=out, cache_read=cr, cache_write_5m=cw)


def test_wait_loop_is_detected_for_the_subagent():
    # 12 wake-ups in the last hour, each re-caching 370k tokens and writing almost nothing
    sub = [m(NOW - 5 * 60 * i, out=50, cw=370_000) for i in range(12)]
    units = {("-home-u-work-proj", "5e55100aabc", "agent-a23"): sub}
    alerts = anomaly.session_alerts(units, {"5e55100aabc": NOW - 10 * H}, PRICES, NOW, RULES, "pc1")
    kinds = {a["kind"] for a in alerts}
    assert "wait_loop" in kinds
    w = [a for a in alerts if a["kind"] == "wait_loop"][0]
    assert w["target"] == "proj 5e55100a / agent-a23"
    assert "最後の人の入力から10.0時間" in w["detail"]


def test_busy_but_productive_session_is_not_a_wait_loop():
    msgs = [m(NOW - 60 * i, out=2000, cw=100_000) for i in range(30)]  # 3M written, 60k output
    alerts = anomaly.session_alerts({("p", "s", ""): msgs}, {}, PRICES, NOW, RULES, "pc1")
    assert "wait_loop" not in {a["kind"] for a in alerts}


def test_sustained_cost_over_three_hours():
    # $20 in each of the last 3 hours (4M cache-write tokens at $5/M = $20)
    msgs = [m(NOW - i * H - 60, out=5000, cw=4_000_000) for i in range(3)]
    alerts = anomaly.session_alerts({("p", "s", ""): msgs}, {}, PRICES, NOW, RULES, "pc1")
    assert [a["kind"] for a in alerts] == ["sustained"]


def test_single_hour_burst_without_sustained():
    msgs = [m(NOW - 60, out=5000, cw=10_000_000)]  # $50 in one hour only
    alerts = anomaly.session_alerts({("p", "s", ""): msgs}, {}, PRICES, NOW, RULES, "pc1")
    assert [a["kind"] for a in alerts] == ["burst"]


def _row(obs, used, resets=NOW + 50 * H):
    return ["", fmt_jst(obs), fmt_jst(resets), used]


def test_pace_surge_like_the_october_runaway():
    rows = [_row(NOW - 6.5 * H, 21), _row(NOW - 3 * H, 28), _row(NOW - 600, 34)]  # 13 points in ~6.3h
    a = anomaly.pace_alert(rows, NOW, RULES, "pc1")
    assert a and a["kind"] == "pace" and a["scope"] == "account"


def test_normal_pace_is_quiet():
    rows = [_row(NOW - 6.5 * H, 30), _row(NOW - 600, 36)]
    assert anomaly.pace_alert(rows, NOW, RULES, "pc1") is None


def test_pace_ignores_previous_week():
    rows = [_row(NOW - 2 * H, 95, resets=NOW - 3 * H), _row(NOW - 600, 5)]
    assert anomaly.pace_alert(rows, NOW, RULES, "pc1") is None


def test_silent_machine_but_not_self_or_retired():
    rows = [["pc1", "", fmt_jst(NOW - 10 * H)], ["pc2", "", fmt_jst(NOW - 4 * H)],
            ["pc2", "", fmt_jst(NOW - 20 * H)], ["old", "", fmt_jst(NOW - 30 * 86400)],
            ["pc3", "", fmt_jst(NOW - 600)]]
    got = anomaly.silent_machine_alerts(rows, NOW, RULES, "pc1")
    assert [a["key"] for a in got] == ["silent:pc2"]


def test_cooldown_uses_sheet_rows_from_other_machines():
    a = {"key": "pace:1", "kind": "pace"}
    b = {"key": "silent:pc2", "kind": "silent"}
    sent_rows = [[fmt_jst(NOW - 2 * H), "pc9", "週使用量の急増", "全端末", "", "pace:1"]]
    assert anomaly.due([a, b], sent_rows, {"silent:pc2": NOW - 25 * H}, NOW, RULES) == [b]


def _write_transcripts(base):
    proj = base / "-home-u-work-proj"
    (proj / "sess1" / "subagents").mkdir(parents=True)
    main = proj / "sess1.jsonl"
    from datetime import datetime, timezone

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        {"type": "user", "timestamp": iso(NOW - 5 * H), "message": {"content": "評価して"}},
        {"type": "user", "timestamp": iso(NOW - 4 * H), "message": {"content": "<task-notification>x"}},
    ]
    main.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    sub = []
    for i in range(12):
        sub.append({"type": "assistant", "timestamp": iso(NOW - 300 * i),
                    "message": {"id": "m%d" % i, "model": "claude-opus-5-5",
                                "usage": {"output_tokens": 50, "cache_creation_input_tokens": 370000}}})
    (proj / "sess1" / "subagents" / "agent-a23.jsonl").write_text("\n".join(json.dumps(x) for x in sub) + "\n")


def test_scan_recent_reads_subagents_and_human_prompts(tmp_path):
    _write_transcripts(tmp_path)
    units, human = anomaly.scan_recent(str(tmp_path), NOW - 3 * H)
    assert list(units) == [("-home-u-work-proj", "sess1", "agent-a23")]
    assert len(units[("-home-u-work-proj", "sess1", "agent-a23")]) == 12
    assert human == {"sess1": pytest.approx(NOW - 5 * H)}  # the task notification is not a person


class FakeClient:
    def __init__(self):
        self.alerts = []

    def read_log(self):
        return []

    def read_machines(self):
        return []

    def read_alerts(self):
        return [list(r) for r in self.alerts]

    def append_alert(self, row):
        self.alerts.append(row)


def test_check_records_notifies_once_and_feeds_statusline(tmp_path, monkeypatch):
    _write_transcripts(tmp_path / "projects")
    os.utime(tmp_path / "projects" / "-home-u-work-proj" / "sess1" / "subagents" / "agent-a23.jsonl", (NOW, NOW))
    cfg = Config(projects_dir=str(tmp_path / "projects"), var_dir=str(tmp_path), machine_name="pc1",
                 token_prices=PRICES, alerts_file=str(tmp_path / "alerts.json"))
    sent = []
    notifier = Notifier([])
    monkeypatch.setattr(Notifier, "enabled", property(lambda self: True))
    monkeypatch.setattr(notifier, "send", lambda s, b: sent.append((s, b)) or True)
    client = FakeClient()
    first = anomaly.check(cfg, client, notifier, now=NOW)
    assert [a["kind"] for a in first] == ["wait_loop"]
    assert client.alerts[0][1:3] == ["pc1", "待機ループ"] and client.alerts[0][5].startswith("wait_loop:pc1:sess1")
    assert len(sent) == 1 and "proj" not in sent[0][0] + sent[0][1]  # no project name in the push
    assert anomaly.check(cfg, client, notifier, now=NOW + 600) == []  # cooldown
    assert len(sent) == 1
    text = hook.alerts_text(now=NOW + 700, path=str(tmp_path / "alerts.json"))
    assert text.startswith("⚠ 待機ループ pc1")
    assert hook.alerts_text(now=NOW + 4 * H, path=str(tmp_path / "alerts.json")) == ""


def test_dry_run_prints_and_writes_nothing(tmp_path):
    _write_transcripts(tmp_path / "projects")
    os.utime(tmp_path / "projects" / "-home-u-work-proj" / "sess1" / "subagents" / "agent-a23.jsonl", (NOW, NOW))
    cfg = Config(projects_dir=str(tmp_path / "projects"), var_dir=str(tmp_path), machine_name="pc1",
                 token_prices=PRICES, alerts_file=str(tmp_path / "alerts.json"))
    lines = []
    client = FakeClient()
    anomaly.check(cfg, client, Notifier([]), now=NOW, dry_run=True, out=lines.append)
    assert lines and lines[0].startswith("[dry-run] 異常検知: 待機ループ")
    assert client.alerts == [] and not (tmp_path / "alerts.json").exists()


def test_pace_alert_is_recorded_but_not_pushed(tmp_path, monkeypatch):
    cfg = Config(projects_dir=str(tmp_path / "none"), var_dir=str(tmp_path), machine_name="pc1",
                 token_prices=PRICES, alerts_file=str(tmp_path / "alerts.json"))
    client = FakeClient()
    client.read_log = lambda: [_row(NOW - 6.5 * H, 21), _row(NOW - 600, 34)]
    sent = []
    notifier = Notifier([])
    monkeypatch.setattr(Notifier, "enabled", property(lambda self: True))
    monkeypatch.setattr(notifier, "send", lambda s, b: sent.append(s) or True)
    got = anomaly.check(cfg, client, notifier, now=NOW)
    assert [a["kind"] for a in got] == ["pace"] and client.alerts and sent == []


def test_ntfy_payload(monkeypatch):
    from usage_forecast import notify
    got = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        got["url"], got["data"], got["auth"] = req.full_url, json.loads(req.data), req.get_header("Authorization")
        return R()
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("NTFY_TOPIC", "cuf-secret")
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    notify.send_ntfy("Claude使用量: 待機ループ pc1 $20/h", "detail")
    assert got["url"] == "https://ntfy.sh" and got["auth"] is None
    assert got["data"]["topic"] == "cuf-secret" and got["data"]["title"].startswith("Claude使用量")
