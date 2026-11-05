"""Weekly usage forecast.

All usage values are percentages 0-100. Times are Unix seconds.
"""
from dataclasses import dataclass
from typing import Optional, Sequence

WEEK_HOURS = 168.0
MIN_HOURS = 1.0
RECENT_WINDOW_HOURS = 24.0
SAME_WEEK_TOLERANCE_SEC = 3600
DROP_MIN_POINTS = 10.0  # smaller dips are treated as noise, not a mid-week reset


@dataclass
class Point:
    observed_at: int
    used: float
    resets_at: int


@dataclass
class Forecast:
    elapsed_h: float
    avg_pace: Optional[float]
    recent_pace: Optional[float]
    forecast_avg: Optional[float]
    forecast_recent: Optional[float]
    depletion_avg: Optional[int]
    depletion_recent: Optional[int]


def same_week(a_resets, b_resets):
    return abs(a_resets - b_resets) <= SAME_WEEK_TOLERANCE_SEC


def elapsed_hours(observed_at, resets_at):
    """Hours since the weekly window started (168 - hours left until reset)."""
    return WEEK_HOURS - (resets_at - observed_at) / 3600.0


def is_valid_week(observed_at, resets_at, now):
    """The state is invalid once the reset time has passed."""
    return now < resets_at and observed_at < resets_at


def is_drop(before, after):
    """A mid-week drop big enough to be a limit reset or plan change."""
    return before - after >= DROP_MIN_POINTS


def recent_baseline(current: Point, history: Sequence[Point]) -> Optional[Point]:
    """Latest point at least 24h before current; else the first point of the week
    (or of the segment after the latest mid-week drop)."""
    week = sorted((p for p in history
                   if same_week(p.resets_at, current.resets_at) and p.observed_at < current.observed_at),
                  key=lambda p: p.observed_at)
    # A mid-week drop (limit reset / plan change) starts a new segment:
    # points before the latest drop are not comparable with the current value.
    seq = week + [current]
    start = 0
    for i in range(1, len(seq)):
        if is_drop(seq[i - 1].used, seq[i].used):
            start = i
    week = seq[start:-1]
    if not week:
        return None
    cutoff = current.observed_at - RECENT_WINDOW_HOURS * 3600
    older = [p for p in week if p.observed_at <= cutoff]
    if older:
        return max(older, key=lambda p: p.observed_at)
    return min(week, key=lambda p: p.observed_at)


def depletion_time(observed_at, used, pace, forecast):
    """When usage reaches 100% at the given pace; only if pace > 0 and forecast > 100."""
    if pace is None or forecast is None or pace <= 0 or forecast <= 100:
        return None
    return int(observed_at + max(0.0, 100.0 - used) / pace * 3600)


def compute(current: Point, history: Sequence[Point]) -> Forecast:
    elapsed = elapsed_hours(current.observed_at, current.resets_at)
    remaining = WEEK_HOURS - elapsed

    recent_pace = None
    base = recent_baseline(current, history)
    if base is not None:
        gap_h = (current.observed_at - base.observed_at) / 3600.0
        if gap_h >= MIN_HOURS:
            recent_pace = (current.used - base.used) / gap_h

    if elapsed < MIN_HOURS:
        return Forecast(elapsed, None, recent_pace, None, None, None, None)

    avg_pace = current.used / elapsed
    forecast_avg = avg_pace * WEEK_HOURS
    forecast_recent = None
    if recent_pace is not None:
        forecast_recent = current.used + recent_pace * remaining

    return Forecast(
        elapsed_h=elapsed,
        avg_pace=avg_pace,
        recent_pace=recent_pace,
        forecast_avg=forecast_avg,
        forecast_recent=forecast_recent,
        depletion_avg=depletion_time(current.observed_at, current.used, avg_pace, forecast_avg),
        depletion_recent=depletion_time(current.observed_at, current.used, recent_pace, forecast_recent),
    )
