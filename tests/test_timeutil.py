from usage_forecast.timeutil import cell_to_ts, fmt_jst, parse_jst, serial_to_ts, to_jst

RESET = 1792029600  # 2026-10-15 02:00 UTC = Thu 11:00 JST


def test_fmt_jst():
    assert fmt_jst(RESET) == "2026/10/15 11:00:00"
    assert to_jst(RESET).isoweekday() == 4


def test_jst_date_rollover():
    # 2026-10-10 15:30 UTC is already the 11th in Japan
    assert fmt_jst(1791646200) == "2026/10/11 00:30:00"


def test_parse_roundtrip():
    assert parse_jst("2026/10/15 11:00:00") == RESET
    assert parse_jst("2026/10/15 11:00") == RESET
    assert parse_jst(fmt_jst(1791702600)) == 1791702600


def test_serial_is_jst_wall_clock():
    # 2026/10/15 11:00 as a Sheets serial number (days since 1899-12-30)
    serial = 46310 + 11 / 24
    assert serial_to_ts(serial) == RESET
    assert cell_to_ts(serial) == RESET
    assert cell_to_ts("2026/10/15 11:00:00") == RESET
    assert cell_to_ts("") is None
