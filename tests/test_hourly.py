import pytest
from usage_forecast import hourly, tokens
from usage_forecast.config import Config
from usage_forecast.timeutil import fmt_jst, parse_jst

H = 3600
PRICES = {"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write_5m": 5.0}}
T0 = parse_jst("2026/11/11 20:00:00")


def msg(ts, cw=1_000_000, session="s1", project="-home-u-work-proj"):
    m = tokens.Msg(ts, "claude-opus-5-5", cache_write_5m=cw)
    m.session, m.project = session, project
    return m


def test_hourly_rows_buckets_and_top_session():
    msgs = {"a": msg(T0 + 60), "b": msg(T0 + 120, cw=3_000_000, session="s2abcdefgh"),
            "c": msg(T0 + 2 * H + 5), "d": msg(T0 + 9 * H)}
    rows = hourly.hourly_rows(msgs, PRICES, "pc1", T0, T0 + 2 * H)
    assert [r[0][11:16] for r in rows] == ["20:00", "22:00"]  # idle hour 21 and the later hour are left out
    assert rows[0][1:4] == ["pc1", 20.0, 4.0]
    assert rows[0][6:] == ["proj s2abcdef", 15.0]


def test_pivot_fills_idle_hours_and_orders_machines():
    rows = [[fmt_jst(T0), "pc1", 5], [fmt_jst(T0), "pc2", 30], [fmt_jst(T0 + 2 * H), "pc1", 7],
            [fmt_jst(T0 - 40 * 86400), "old", 99]]
    header, hourly_, daily = hourly.build_pivot(rows, T0 + 3 * H + 10, hours=4, days=2)
    assert header == ["時刻", "pc2", "pc1"]
    assert [r[1:] for r in hourly_] == [[0, 0], [30, 5], [0, 0], [0, 7]]  # 19:00 .. 22:00
    assert hourly_[-1][0].endswith("22:00:00")
    assert daily[-1][1:] == [30, 12] and len(daily) == 2


def test_pivot_merges_small_machines(monkeypatch):
    monkeypatch.setattr(hourly, "MAX_MACHINES", 3)
    rows = [[fmt_jst(T0), "pc%d" % i, i] for i in range(1, 6)]
    header, _, daily = hourly.build_pivot(rows, T0 + H + 10, hours=2, days=1)
    assert header == ["時刻", "pc5", "pc4", "その他"]
    assert daily[0][1:] == [5, 4, 6]


class FakeClient:
    def __init__(self):
        self.rows, self.chart = [], None

    def append_hourly(self, rows):
        self.rows += rows

    def read_hourly(self):
        return [list(r) for r in self.rows]

    def write_machine_chart(self, header, hourly_, daily):
        self.chart = (header, hourly_, daily)


def test_update_is_incremental(tmp_path, monkeypatch):
    msgs = {"a": msg(T0 + 60), "b": msg(T0 + H + 60)}
    monkeypatch.setattr(hourly.tokens, "scan", lambda d, since_ts: msgs)
    cfg = Config(var_dir=str(tmp_path), machine_name="pc1", token_prices=PRICES)
    c = FakeClient()
    hourly.update(cfg, c, now=T0 + H + 400)          # only 20:00 is over and settled
    assert [r[0][11:16] for r in c.rows] == ["20:00"]
    c.chart = None
    hourly.update(cfg, c, now=T0 + H + 1000)         # nothing new: no rows, no chart rewrite
    assert len(c.rows) == 1 and c.chart is None
    hourly.update(cfg, c, now=T0 + 2 * H + 400)      # 21:00 now settled
    assert [r[0][11:16] for r in c.rows] == ["20:00", "21:00"]
    assert c.chart[0] == ["時刻", "pc1"]


def test_failed_chart_rebuild_is_retried(tmp_path, monkeypatch):
    msgs = {"a": msg(T0 + 60)}
    monkeypatch.setattr(hourly.tokens, "scan", lambda d, since_ts: msgs)
    cfg = Config(var_dir=str(tmp_path), machine_name="pc1", token_prices=PRICES)
    c = FakeClient()

    def quota(*a):
        raise RuntimeError("429")
    c.read_hourly = quota
    with pytest.raises(RuntimeError):
        hourly.update(cfg, c, now=T0 + H + 400)      # rows appended, chart rebuild fails
    assert len(c.rows) == 1 and c.chart is None
    del c.read_hourly
    hourly.update(cfg, c, now=T0 + H + 1000)         # no new rows, but the chart is rebuilt now
    assert len(c.rows) == 1 and c.chart[0] == ["時刻", "pc1"]
    c.chart = None
    hourly.update(cfg, c, now=T0 + H + 1500)         # done: no further rewrites
    assert c.chart is None
