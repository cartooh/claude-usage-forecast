import gspread
import pytest

from usage_forecast.sheets import format_percent, with_retry


class Resp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {"error": {"code": self.status_code, "message": "x", "status": "x"}}


def api_error(code):
    return gspread.exceptions.APIError(Resp(code))


def test_format_percent():
    assert format_percent(60) == "60%"
    assert format_percent(55.4) == "55.4%"
    assert format_percent(55.40) == "55.4%"


def test_retry_on_429_then_success():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise api_error(429 if len(calls) == 1 else 503)
        return "ok"

    assert with_retry(fn, sleep=sleeps.append) == "ok"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 1


def test_no_retry_on_403():
    def fn():
        raise api_error(403)

    with pytest.raises(gspread.exceptions.APIError):
        with_retry(fn, sleep=lambda s: None)
