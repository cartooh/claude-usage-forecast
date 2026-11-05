import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import usage_probe  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 30, 11, 2, tzinfo=JST).timestamp()

SCREEN = """   Settings  Status   Config   Usage   Stats
   Current session
   █████████████████████████████████████▌             75% used
   Resets 11:30am (Asia/Tokyo)
   Current week (all models)
   █████████████                                      26% used
   Resets Nov 5, 11am (Asia/Tokyo)
   Current week (Fable)
   ██▌                                                5% used
   Resets Nov 5, 10:59am (Asia/Tokyo)
   What's contributing to your usage?
   86%  Start fresh when you switch tasks · run /clear
"""


def test_parse_usage_screen():
    w = usage_probe.parse_usage_screen(SCREEN, NOW)
    assert set(w) == {"session", "all models", "Fable"}
    assert w["Fable"]["used_percentage"] == 5.0
    assert datetime.fromtimestamp(w["Fable"]["resets_at"], JST) == datetime(2026, 11, 5, 10, 59, tzinfo=JST)
    assert datetime.fromtimestamp(w["session"]["resets_at"], JST) == datetime(2026, 10, 30, 11, 30, tzinfo=JST)
    assert w["all models"]["used_percentage"] == 26.0  # "86%" tips line is not picked up


def test_resets_across_year_end():
    now = datetime(2026, 12, 30, 12, 0, tzinfo=JST).timestamp()
    ts = usage_probe.parse_resets("Jan 2, 11am (Asia/Tokyo)", now)
    assert datetime.fromtimestamp(ts, JST) == datetime(2027, 1, 2, 11, 0, tzinfo=JST)


def test_time_only_rolls_to_next_day():
    now = datetime(2026, 10, 30, 23, 0, tzinfo=JST).timestamp()
    ts = usage_probe.parse_resets("1:30am (Asia/Tokyo)", now)
    assert datetime.fromtimestamp(ts, JST) == datetime(2026, 10, 31, 1, 30, tzinfo=JST)


def test_usage_credits_section():
    off = SCREEN + "   Usage credits\n   Usage credits are off · /usage-credits to turn them on\n   Esc to cancel\n"
    assert usage_probe.parse_usage_credits(off) == {
        "text": "Usage credits are off · /usage-credits to turn them on", "amounts_usd": []}
    on = SCREEN + "   Usage credits\n   $12.40 spent this month · $50.00 limit\n   Esc to cancel\n"
    assert usage_probe.parse_usage_credits(on)["amounts_usd"] == [12.4, 50.0]
    assert usage_probe.parse_usage_credits(SCREEN) is None
