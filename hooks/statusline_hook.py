#!/usr/bin/env python3
"""Claude Code statusline hook.

Reads the statusline JSON from stdin, prints a short status line, and records
rate_limits into a state file (atomic write) for the cron collector.

Standard library only. Never raises: any error is logged and swallowed so the
Claude Code display keeps working.

Freshness: rate_limits in the statusline JSON only change after an API
response, so an idle session keeps re-rendering old values. A render counts as
a new observation only when, for that session, the rate_limits payload changed
or cost.total_api_duration_ms increased. Some sessions keep stale (lower)
values even after API calls, so a value lower than the stored one in the same
weekly window is accepted only when the same session itself saw the higher
value before (a real mid-week drop, e.g. limit reset or plan change), or when
the stored observation is older than 12h.
"""
import fcntl
import json
import os
import sys
import time

STATE_PATH = os.path.expanduser(os.environ.get("USAGE_STATE_FILE", "~/.claude/usage-state.json"))
LOG_PATH = os.path.expanduser(os.environ.get("USAGE_HOOK_LOG", "~/.claude/usage-hook.log"))
LOG_MAX_BYTES = 256 * 1024
SESSION_TTL_SEC = 8 * 24 * 3600
SAME_WEEK_TOLERANCE_SEC = 3600
STALE_REPLACE_SEC = 12 * 3600


def _log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _window(obj):
    """Return {'used_percentage': float, 'resets_at': int} or None if invalid."""
    if not isinstance(obj, dict):
        return None
    used = obj.get("used_percentage")
    resets = obj.get("resets_at")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    if isinstance(resets, bool) or not isinstance(resets, (int, float)):
        return None
    return {"used_percentage": used, "resets_at": int(resets)}


def render(data):
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return ""
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = _window(rl.get(key))
        if w is not None:
            parts.append("%s:%g%%" % (label, w["used_percentage"]))
    return " ".join(parts)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # corrupt file: start over
        _log("state load failed: %r" % (e,))
        return {}


def _atomic_write(path, obj):
    d = os.path.dirname(path) or "."
    tmp = os.path.join(d, ".%s.tmp.%d" % (os.path.basename(path), os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge(state, data, now):
    """Pure function: return (new_state, changed: bool, reason: str)."""
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return state, False, "no rate_limits"
    seven = _window(rl.get("seven_day"))
    if seven is None:
        return state, False, "no seven_day"
    five = _window(rl.get("five_hour"))
    if five is not None and five["resets_at"] <= now:
        five = None  # expired 5h window carried over from an old response

    session = str(data.get("session_id") or "unknown")
    cost = data.get("cost") if isinstance(data.get("cost"), dict) else {}
    api_ms = cost.get("total_api_duration_ms")
    if isinstance(api_ms, bool) or not isinstance(api_ms, (int, float)):
        api_ms = None
    payload = {"seven_day": seven, "five_hour": five}

    new = dict(state)
    sessions = dict(new.get("sessions") or {})
    prev = sessions.get(session)
    cur = new.get("seven_day") if isinstance(new.get("seven_day"), dict) else None

    if prev is None:
        # First time we see this session: only accept strictly newer info.
        fresh = cur is None or seven["resets_at"] > cur["resets_at"] + SAME_WEEK_TOLERANCE_SEC or (
            abs(seven["resets_at"] - cur["resets_at"]) <= SAME_WEEK_TOLERANCE_SEC
            and seven["used_percentage"] > cur["used_percentage"]
        )
    else:
        prev_ms = prev.get("api_ms")
        fresh = prev.get("payload") != payload or (
            api_ms is not None and prev_ms is not None and api_ms > prev_ms
        )

    sessions[session] = {"api_ms": api_ms, "payload": payload, "seen_at": int(now)}
    # keep the raw object (small) so new windows such as per-model limits can be inspected
    new["last_raw_rate_limits"] = {"seen_at": int(now), "data": rl}
    sessions = {k: v for k, v in sessions.items() if now - v.get("seen_at", 0) <= SESSION_TTL_SEC}
    new["sessions"] = sessions

    reason = "not fresh"
    if fresh:
        if cur is not None and seven["resets_at"] < cur["resets_at"] - SAME_WEEK_TOLERANCE_SEC:
            reason = "older week"
        elif (
            cur is not None
            and abs(seven["resets_at"] - cur["resets_at"]) <= SAME_WEEK_TOLERANCE_SEC
            and seven["used_percentage"] < cur["used_percentage"]
            and not _drop_trusted(prev, cur, new.get("observed_at"), now)
        ):
            reason = "lower than stored (stale session)"
        else:
            new["observed_at"] = int(now)
            new["seven_day"] = seven
            new["five_hour"] = five
            reason = "observed"
    new.pop("pending_drop", None)  # legacy key
    return new, True, reason


def _drop_trusted(prev, cur, stored_observed_at, now):
    """Accept a lower weekly value only when it is a real drop.

    Stale sessions keep reporting old (lower) values even after API calls, so a
    lower value is trusted only when this same session had itself seen a value
    at least as high as the stored one (it observed the drop), or when the
    stored observation is so old that it is better replaced.
    """
    if isinstance(prev, dict):
        ps = (prev.get("payload") or {}).get("seven_day") or {}
        if (abs(ps.get("resets_at", 0) - cur["resets_at"]) <= SAME_WEEK_TOLERANCE_SEC
                and ps.get("used_percentage", -1) >= cur["used_percentage"]):
            return True
    return stored_observed_at is None or now - stored_observed_at > STALE_REPLACE_SEC


def update_state(data, now=None, path=STATE_PATH):
    now = time.time() if now is None else now
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict) or _window(rl.get("seven_day")) is None:
        return "skipped: no rate_limits"
    lock_path = path + ".lock"
    with open(lock_path, "a") as lf:
        for _ in range(20):  # up to ~200 ms
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        else:
            return "skipped: lock busy"
        try:
            state = _load(path)
            new, changed, reason = merge(state, data, now)
            if changed:
                _atomic_write(path, new)
            return reason
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def run_wrapped(command, raw, timeout=5.0):
    """Run the user's original statusline command with the same stdin; return its stdout."""
    import subprocess
    r = subprocess.run(command, shell=True, input=raw, capture_output=True, text=True, timeout=timeout)
    return r.stdout.rstrip("\n")


def main(argv=None):
    """Usage: statusline_hook.py [--wrap '<original statusline command>']

    With --wrap, the original command's output is shown (the usage line is
    appended only if it printed nothing); without it, the usage line is shown.
    """
    argv = sys.argv[1:] if argv is None else argv
    wrapped = argv[argv.index("--wrap") + 1] if "--wrap" in argv[:-1] else None
    line = ""
    raw = ""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        try:
            line = render(data)
        except Exception as e:
            _log("render failed: %r" % (e,))
        update_state(data)
    except Exception as e:
        _log("hook failed: %r" % (e,))
    if wrapped:
        try:
            out = run_wrapped(wrapped, raw)
            if out.strip():
                line = out
        except Exception as e:
            _log("wrapped statusline failed: %r" % (e,))
    try:
        sys.stdout.write(line + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
