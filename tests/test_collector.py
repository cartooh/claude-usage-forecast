import json

import pytest

from usage_forecast import collector
from usage_forecast.config import Config
from usage_forecast.notify import Notifier
from usage_forecast.timeutil import parse_jst

H = 3600
RESET = 1792029600  # Thu 11:00 JST
START = RESET - 168 * H


class FakeClient:
    """In-memory stand-in for GSheetClient. Dates are stored as JST text like USER_ENTERED input."""

    def __init__(self, weekday=4, hour=11):
        self.rows = []
        self.usage_cell = None
        self.settings = {"usage_row": 27, "usage_cell_formula": 0.55,
                         "reset_weekday": weekday, "reset_hour": hour}

    def read_log(self):
        return [list(r) for r in self.rows]

    def pace_settings(self):
        return dict(self.settings)

    def write_current_usage(self, used, settings=None):
        self.usage_cell = used
        return "B27"

    def append_log(self, row):
        self.rows.append(list(row))

    def write_chart_data(self, rows):
        self.chart = rows
        return True

    machines = None

    def read_machines(self):
        return [list(r) for r in (self.machines or [])]

    def upsert_machine(self, row):
        from usage_forecast.timeutil import cell_to_ts
        self.machines = [r for r in (self.machines or [])
                         if not (r[0] == row[0] and cell_to_ts(r[1]) == cell_to_ts(row[1]))] + [list(row)]
        return "ok"


class FakeNotifier(Notifier):
    def __init__(self):
        self.methods = ["webhook"]
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        return True


@pytest.fixture
def cfg(tmp_path):
    c = Config(state_file=str(tmp_path / "usage-state.json"), var_dir=str(tmp_path / "var"),
               models_file=str(tmp_path / "usage-models.json"),
               projects_dir=str(tmp_path / "projects"),
               token_prices={"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2,
                                                 "cache_write_5m": 5, "cache_write_1h": 8},
                             "claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25,
                                                  "cache_write_5m": 12.5, "cache_write_1h": 20}})
    (tmp_path / "var").mkdir()
    return c


def write_state(cfg, observed_at, used, resets=RESET, five=None):
    with open(cfg.state_file, "w") as f:
        json.dump({"observed_at": observed_at,
                   "seven_day": {"used_percentage": used, "resets_at": resets},
                   "five_hour": five}, f)


def test_same_observation_twice_is_not_duplicated(cfg):
    client = FakeClient()
    write_state(cfg, START + 63 * H, 58)
    assert collector.run(cfg, client, Notifier([]), now=START + 63.2 * H) == "written"
    assert collector.run(cfg, client, Notifier([]), now=START + 63.7 * H) == "unchanged"
    # even if the local collector state is lost, the sheet itself is checked
    (cfg_var := __import__("pathlib").Path(cfg.var_dir) / "collector-state.json").unlink()
    assert collector.run(cfg, client, Notifier([]), now=START + 64 * H) == "duplicate"
    assert len(client.rows) == 1
    assert client.usage_cell == 58


def test_row_contents(cfg):
    client = FakeClient()
    obs = START + 84 * H
    write_state(cfg, obs, 50, five={"used_percentage": 4, "resets_at": obs + 4 * H})
    collector.run(cfg, client, Notifier([]), now=obs + 1800)
    row = client.rows[0]
    assert parse_jst(row[1]) == obs and row[2] == "2026/10/15 11:00:00"
    assert row[3] == 50 and row[4] == 84.0 and row[7] == 100.0 and row[8] == ""
    assert row[9] == 4 and row[11] == 0.5 and row[12] == ""


def test_recent_pace_uses_log_history(cfg):
    client = FakeClient()
    write_state(cfg, START + 30 * H, 20)
    collector.run(cfg, client, Notifier([]), now=START + 30 * H)
    write_state(cfg, START + 60 * H, 50)
    collector.run(cfg, client, Notifier([]), now=START + 60 * H)
    assert client.rows[1][6] == pytest.approx(1.0)  # (50-20)/30h
    assert client.rows[1][8] == pytest.approx(50 + 1.0 * 108, abs=0.05)


def test_expired_week_is_recorded_but_not_written_to_pace(cfg):
    client = FakeClient()
    write_state(cfg, RESET - 2 * H, 97)
    assert collector.run(cfg, client, Notifier([]), now=RESET + 600) == "written-invalid"
    assert client.usage_cell is None
    assert client.rows[0][3] == "" and collector.REMARK_WEEK_EXPIRED in client.rows[0][12]


def test_stale_observation_suppresses_forecast_notification(cfg):
    client, n = FakeClient(), FakeNotifier()
    write_state(cfg, START + 100 * H, 95)
    collector.run(cfg, client, n, now=START + 113 * H)
    assert collector.REMARK_STALE in client.rows[0][12]
    assert n.sent == []


def test_reset_mismatch_remark(cfg):
    client = FakeClient(weekday=3, hour=11)
    write_state(cfg, START + 10 * H, 5)
    collector.run(cfg, client, Notifier([]), now=START + 10 * H)
    assert collector.REMARK_RESET_MISMATCH in client.rows[0][12]


def test_notifications_once_per_level_per_week(cfg):
    client, n = FakeClient(), FakeNotifier()
    write_state(cfg, START + 100 * H, 85)
    collector.run(cfg, client, n, now=START + 100 * H)
    assert len(n.sent) == 2  # forecast >= 100 and used >= 80
    write_state(cfg, START + 101 * H, 91)
    collector.run(cfg, client, n, now=START + 101 * H)
    assert len(n.sent) == 3 and "90%" in n.sent[-1]
    write_state(cfg, START + 102 * H, 93)
    collector.run(cfg, client, n, now=START + 102 * H)
    assert len(n.sent) == 3
    # next week: levels re-arm
    write_state(cfg, RESET + 100 * H, 85, resets=RESET + 168 * H)
    collector.run(cfg, client, n, now=RESET + 100 * H)
    assert len(n.sent) == 5


def test_dry_run_writes_nothing(cfg):
    client, out = FakeClient(), []
    write_state(cfg, START + 63 * H, 58)
    assert collector.run(cfg, client, Notifier([]), now=START + 63 * H, dry_run=True,
                         out=out.append) == "dry-run"
    assert client.rows == [] and client.usage_cell is None
    assert any("58%" in line for line in out)
    assert not (__import__("pathlib").Path(cfg.var_dir) / "collector-state.json").exists()


def test_mid_week_drop_is_remarked(cfg):
    client = FakeClient()
    write_state(cfg, START + 100 * H, 99)
    collector.run(cfg, client, Notifier([]), now=START + 100 * H)
    write_state(cfg, START + 110 * H, 30)
    collector.run(cfg, client, Notifier([]), now=START + 110 * H)
    assert collector.REMARK_DROP in client.rows[1][12]
    assert client.usage_cell == 30


def test_week_expiry_recorded_once_after_already_written_observation(cfg):
    client = FakeClient()
    write_state(cfg, RESET - 80 * 60, 89)
    assert collector.run(cfg, client, Notifier([]), now=RESET - 3600) == "written"
    assert collector.run(cfg, client, Notifier([]), now=RESET - 1800) == "unchanged"
    # reset passes with no new observation -> one 'expired' row, B cell untouched
    assert collector.run(cfg, client, Notifier([]), now=RESET + 120) == "written-invalid"
    assert collector.REMARK_WEEK_EXPIRED in client.rows[-1][12] and client.usage_cell == 89
    assert collector.run(cfg, client, Notifier([]), now=RESET + 1800) == "unchanged"
    assert len(client.rows) == 2
    # lost local state: the sheet still prevents a second expired row
    (__import__("pathlib").Path(cfg.var_dir) / "collector-state.json").unlink()
    assert collector.run(cfg, client, Notifier([]), now=RESET + 3600) == "duplicate"
    assert len(client.rows) == 2


def test_chart_rows_break_at_reset_and_hide_early_forecasts():
    from usage_forecast.timeutil import fmt_jst
    nxt = RESET + 168 * H
    rows = [
        ["", fmt_jst(RESET - 2 * H), fmt_jst(RESET), 98, 166, "", "", 99.0, 99.5],
        ["", fmt_jst(RESET + 3 * H), fmt_jst(nxt), 7, 3, "", "", 318.0, ""],
        ["", fmt_jst(RESET + 20 * H), fmt_jst(nxt), 15, 20, "", "", 126.0, 110.0],
        ["", fmt_jst(RESET - 20 * 86400), fmt_jst(RESET - 14 * 86400), 50, 1, "", "", 1, 1],  # too old
        ["", fmt_jst(RESET - 1 * H), fmt_jst(RESET), "", "", "", "", "", ""],  # expired row
    ]
    out = collector.chart_rows(rows, now=RESET + 21 * H)
    assert [r[1] for r in out] == [98.0, "", "", 7.0, 15.0]
    assert all(len(r) == 9 for r in out)
    assert out[1][0] == out[2][0] == fmt_jst(RESET) and out[1][2] == 100 and out[2][2] == 0
    assert out[3][3] == "" and out[4][3] == 126.0  # forecast hidden before 12h
    assert out[0][2] == pytest.approx(166 / 168 * 100, abs=0.01)


def test_run_refreshes_chart(cfg):
    client = FakeClient()
    write_state(cfg, START + 30 * H, 20)
    collector.run(cfg, client, Notifier([]), now=START + 30 * H)
    assert len(client.chart) == 1 and client.chart[0][1] == 20.0


def write_models(cfg, observed_at, used, resets=RESET, label="Fable"):
    with open(cfg.models_file, "w") as f:
        json.dump({"observed_at": observed_at,
                   "windows": {label: {"used_percentage": used, "resets_at": resets}}}, f)


def test_fable_usage_is_logged_and_charted(cfg):
    from usage_forecast.timeutil import fmt_jst
    client = FakeClient()
    obs = START + 50 * H
    write_state(cfg, obs, 30)
    write_models(cfg, obs - 600, 5.0)
    collector.run(cfg, client, Notifier([]), now=obs + 60)
    row = client.rows[0]
    assert len(row) == 24 and row[13] == 5.0 and row[14] == fmt_jst(obs - 600)
    assert client.chart[0][6] == 5.0


def test_fable_usage_skipped_when_old_or_expired(cfg):
    obs = START + 50 * H
    write_models(cfg, obs - 3 * H, 5.0)
    assert collector.read_model_usage(cfg.models_file, "Fable", obs, 2) is None
    write_models(cfg, obs, 5.0, resets=obs - 1)
    assert collector.read_model_usage(cfg.models_file, "Fable", obs, 2) is None
    write_models(cfg, obs, 5.0)
    assert collector.read_model_usage(cfg.models_file, "Fable", obs + 60, 2) == (5.0, obs)
    assert collector.read_model_usage(cfg.models_file, "Opus", obs + 60, 2) is None


def write_transcript(cfg, entries):
    import os
    from datetime import datetime, timezone
    d = os.path.join(cfg.projects_dir, "proj")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "s.jsonl"), "a") as f:
        for mid, ts, model, usage in entries:
            f.write(json.dumps({"type": "assistant", "timestamp": datetime.fromtimestamp(ts, timezone.utc)
                                .isoformat().replace("+00:00", "Z"),
                                "message": {"id": mid, "model": model, "usage": usage}}) + "\n")


def test_token_columns_in_row(cfg):
    client = FakeClient()
    obs = START + 50 * H
    u = {"input_tokens": 1000, "output_tokens": 1_000_000, "cache_read_input_tokens": 10_000_000,
         "cache_creation_input_tokens": 0}
    write_transcript(cfg, [
        ("m1", obs - H, "claude-opus-5-5", dict(u, output_tokens=10)),   # partial entry
        ("m1", obs - H, "claude-opus-5-5", u),                             # final entry, same id
        ("m2", obs - 2 * H, "claude-fable-5-1", {"input_tokens": 0, "output_tokens": 100_000,
                                                 "cache_read_input_tokens": 0,
                                                 "cache_creation_input_tokens": 0}),
        ("m3", START - H, "claude-opus-5-5", u),                           # previous week
    ])
    write_state(cfg, obs, 20)
    write_models(cfg, obs, 10.0)
    collector.run(cfg, client, Notifier([]), now=obs + 60)
    row = client.rows[0]
    assert row[15] == pytest.approx(1.101)            # (1000 + 1M + 100k) / 1M
    assert row[16] == pytest.approx(10.0)
    # opus: 1000*4 + 1M*20 + 10M*0.2 = 22.004 ; fable: 100k*50 = 5.0
    assert row[17] == pytest.approx(27.0, abs=0.01)
    assert row[18] == pytest.approx(1.101 / 20, abs=0.001)
    assert row[19] == pytest.approx(27.0 / 20, abs=0.001)
    assert row[20] == pytest.approx(5.0) and row[21] == pytest.approx(0.5)
    assert row[22] == 0 and row[23] == ""
    assert client.chart[0][7] == row[18] and client.chart[0][8] == row[19]
    assert len(client.machines) == 1 and client.machines[0][5] == pytest.approx(27.0, abs=0.01)


def test_tokens_are_summed_across_machines(cfg):
    from usage_forecast.timeutil import fmt_jst
    client = FakeClient()
    client.machines = [["other-pc", fmt_jst(RESET), fmt_jst(START + 40 * H), 2.0, 30.0, 40.0, 10.0, 0, ""],
                       ["old-week-pc", fmt_jst(RESET - 168 * H), fmt_jst(START - H), 9.0, 9.0, 99.0, 0, 0, ""]]
    write_state(cfg, START + 50 * H, 20)
    collector.run(cfg, client, Notifier([]), now=START + 50 * H)
    row = client.rows[0]
    assert row[15] == pytest.approx(2.0) and row[17] == pytest.approx(40.0)
    assert row[19] == pytest.approx(2.0)  # $40 / 20%
    assert {m[0] for m in client.machines} == {"other-pc", "old-week-pc", cfg.machine_name}


def test_stale_value_from_another_machine_is_not_logged(cfg):
    client = FakeClient()
    write_state(cfg, START + 50 * H, 40)
    collector.run(cfg, client, Notifier([]), now=START + 50 * H)
    write_state(cfg, START + 51 * H, 37)   # e.g. a stale session on this PC after another PC logged 40
    assert collector.run(cfg, client, Notifier([]), now=START + 51 * H) == "stale-skip"
    assert len(client.rows) == 1 and client.usage_cell == 40


def test_ratios_blank_below_min_usage(cfg):
    client = FakeClient()
    write_state(cfg, START + 10 * H, 2)
    collector.run(cfg, client, Notifier([]), now=START + 10 * H)
    assert client.rows[0][18] == "" and client.rows[0][19] == ""


def test_exhausted_intervals():
    from usage_forecast.timeutil import fmt_jst
    rows = [
        ["", fmt_jst(START + 100 * H), fmt_jst(RESET), 99, "", "", "", "", "", 50, fmt_jst(START + 103 * H)],
        ["", fmt_jst(START + 120 * H), fmt_jst(RESET), 100, "", "", "", "", "", 100, fmt_jst(START + 122 * H)],
        ["", fmt_jst(START + 130 * H), fmt_jst(RESET), 100, "", "", "", "", "", 20, fmt_jst(START + 134 * H)],
    ]
    iv = sorted(collector.exhausted_intervals(rows))
    assert iv == [(START + 120 * H, START + 122 * H), (START + 120 * H, RESET)]
