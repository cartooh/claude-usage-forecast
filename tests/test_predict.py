import pytest

from usage_forecast import predict
from usage_forecast.predict import Point

H = 3600
RESET = 1792029600
START = RESET - 168 * H


def at(h):
    return START + int(h * H)


def test_elapsed_hours():
    assert predict.elapsed_hours(at(63.5), RESET) == pytest.approx(63.5)


def test_average_method():
    fc = predict.compute(Point(at(84), 50, RESET), [])
    assert fc.avg_pace == pytest.approx(50 / 84)
    assert fc.forecast_avg == pytest.approx(100.0)
    assert fc.recent_pace is None and fc.forecast_recent is None


def test_recent_uses_latest_row_at_least_24h_old():
    hist = [Point(at(10), 5, RESET), Point(at(30), 20, RESET), Point(at(50), 40, RESET)]
    fc = predict.compute(Point(at(60), 46, RESET), hist)
    # baseline = at(30) (latest <= 60-24=36), gap 30h, (46-20)/30
    assert fc.recent_pace == pytest.approx(26 / 30)
    assert fc.forecast_recent == pytest.approx(46 + 26 / 30 * 108)


def test_recent_falls_back_to_first_row_of_week():
    hist = [Point(at(40), 30, RESET), Point(at(50), 40, RESET)]
    fc = predict.compute(Point(at(60), 46, RESET), hist)
    assert fc.recent_pace == pytest.approx(16 / 20)


def test_recent_blank_when_gap_under_1h():
    hist = [Point(at(59.5), 45, RESET)]
    fc = predict.compute(Point(at(60), 46, RESET), hist)
    assert fc.recent_pace is None and fc.forecast_recent is None


def test_blank_when_elapsed_under_1h():
    fc = predict.compute(Point(at(0.5), 1, RESET), [])
    assert fc.avg_pace is None and fc.forecast_avg is None and fc.forecast_recent is None


def test_previous_week_rows_are_ignored():
    prev_reset = RESET - 168 * H
    hist = [Point(at(-30), 90, prev_reset), Point(at(-2), 99, prev_reset)]
    fc = predict.compute(Point(at(30), 20, RESET), hist)
    assert fc.recent_pace is None


def test_depletion_only_when_forecast_over_100():
    fc = predict.compute(Point(at(50), 60, RESET), [])  # pace 1.2%/h, forecast 201.6
    assert fc.depletion_avg == at(50) + int(40 / 1.2 * H)
    fc2 = predict.compute(Point(at(100), 50, RESET), [])  # forecast 84
    assert fc2.depletion_avg is None


def test_depletion_none_for_non_positive_pace():
    assert predict.depletion_time(0, 50, 0, 150) is None
    assert predict.depletion_time(0, 50, -1, 150) is None


def test_week_validity_across_reset():
    assert predict.is_valid_week(at(167), RESET, RESET - 1)
    assert not predict.is_valid_week(at(167), RESET, RESET)
    assert not predict.is_valid_week(at(167), RESET, RESET + 60)


def test_recent_pace_ignores_points_before_mid_week_drop():
    hist = [Point(at(80), 90, RESET), Point(at(100), 99, RESET)]
    fc = predict.compute(Point(at(130), 30, RESET), hist)  # dropped now: no baseline
    assert fc.recent_pace is None and fc.forecast_recent is None
    hist2 = hist + [Point(at(130), 30, RESET)]
    fc2 = predict.compute(Point(at(140), 35, RESET), hist2)  # baseline = first point after drop
    assert fc2.recent_pace == pytest.approx(0.5)


def test_small_dip_does_not_restart_segment():
    hist = [Point(at(100), 60, RESET), Point(at(120), 70, RESET), Point(at(121), 64, RESET)]
    fc = predict.compute(Point(at(130), 74, RESET), hist)
    # baseline is still the latest point >= 24h old (at(100)), not the dip
    assert fc.recent_pace == pytest.approx(14 / 30)
