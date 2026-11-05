import json

import statusline_hook as hook

RESET = 1792029600
NOW = RESET - 70 * 3600


def payload(session, used, api_ms, five=None, resets=RESET):
    rl = {"seven_day": {"used_percentage": used, "resets_at": resets}}
    if five is not None:
        rl["five_hour"] = five
    return {"session_id": session, "cost": {"total_api_duration_ms": api_ms}, "rate_limits": rl}


def test_render():
    assert hook.render(payload("a", 60, 1, {"used_percentage": 4, "resets_at": NOW + 99})) == "5h:4% 7d:60%"
    assert hook.render({}) == ""


def test_first_session_on_empty_state_is_observation():
    st, changed, reason = hook.merge({}, payload("a", 58, 100), NOW)
    assert reason == "observed" and st["observed_at"] == NOW and st["seven_day"]["used_percentage"] == 58


def test_idle_rerender_does_not_refresh_observed_at():
    st, _, _ = hook.merge({}, payload("a", 58, 100), NOW)
    st2, _, reason = hook.merge(st, payload("a", 58, 100), NOW + 600)
    assert reason == "not fresh" and st2["observed_at"] == NOW


def test_api_call_with_same_value_refreshes():
    st, _, _ = hook.merge({}, payload("a", 58, 100), NOW)
    st2, _, reason = hook.merge(st, payload("a", 58, 250), NOW + 600)
    assert reason == "observed" and st2["observed_at"] == NOW + 600


def test_stale_other_session_cannot_lower_value():
    st, _, _ = hook.merge({}, payload("a", 60, 100), NOW)
    # session b first seen with an old 58% -> not accepted
    st2, _, reason = hook.merge(st, payload("b", 58, 999), NOW + 60)
    assert reason == "not fresh" and st2["seven_day"]["used_percentage"] == 60
    # b gets a response but its (stale) value is still lower -> rejected
    st3, _, reason = hook.merge(st2, payload("b", 59, 1200), NOW + 120)
    assert reason.startswith("lower") and st3["seven_day"]["used_percentage"] == 60


def test_new_week_replaces_old_week():
    st, _, _ = hook.merge({}, payload("a", 99, 100), NOW)
    st2, _, reason = hook.merge(st, payload("a", 1, 200, resets=RESET + 168 * 3600), RESET + 60)
    assert reason == "observed" and st2["seven_day"]["used_percentage"] == 1


def test_expired_five_hour_is_dropped():
    st, _, _ = hook.merge({}, payload("a", 58, 1, {"used_percentage": 22, "resets_at": NOW - 1}), NOW)
    assert st["five_hour"] is None


def test_missing_rate_limits_does_not_touch_file(tmp_path):
    p = tmp_path / "state.json"
    assert hook.update_state({"session_id": "a"}, NOW, str(p)).startswith("skipped")
    assert not p.exists()


def test_update_state_writes_atomically(tmp_path):
    p = tmp_path / "state.json"
    assert hook.update_state(payload("a", 58, 1), NOW, str(p)) == "observed"
    d = json.loads(p.read_text())
    assert d["observed_at"] == NOW and (p.stat().st_mode & 0o777) == 0o600
    assert not [x for x in tmp_path.iterdir() if ".tmp." in x.name]


def test_main_survives_garbage(monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setattr(hook, "LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main() == 0
    assert capsys.readouterr().out == "\n"


def test_real_drop_seen_by_same_session_is_accepted():
    st, _, _ = hook.merge({}, payload("a", 99, 100), NOW)
    st, _, reason = hook.merge(st, payload("a", 25, 200), NOW + 600)
    assert reason == "observed" and st["seven_day"]["used_percentage"] == 25


def test_stale_sessions_cannot_fake_a_drop_even_together():
    st, _, _ = hook.merge({}, payload("a", 70, 100), NOW)
    st, _, _ = hook.merge(st, payload("b", 64, 50), NOW + 1)     # b first seen, stale
    st, _, _ = hook.merge(st, payload("c", 64, 50), NOW + 2)     # c first seen, stale
    st, _, r1 = hook.merge(st, payload("b", 64, 80), NOW + 600)  # b API call, still stale
    st, _, r2 = hook.merge(st, payload("c", 64, 90), NOW + 700)
    assert r1.startswith("lower") and r2.startswith("lower")
    assert st["seven_day"]["used_percentage"] == 70 and st["observed_at"] == NOW


def test_lower_value_replaces_very_old_observation():
    st, _, _ = hook.merge({}, payload("a", 99, 100), NOW)
    st, _, _ = hook.merge(st, payload("b", 30, 50), NOW + 60)
    st, _, reason = hook.merge(st, payload("b", 31, 80), NOW + 13 * 3600)
    assert reason == "observed" and st["seven_day"]["used_percentage"] == 31


def test_legacy_pending_key_is_dropped():
    st, _, _ = hook.merge({"pending_drop": {"x": 1}}, payload("a", 50, 1), NOW)
    assert "pending_drop" not in st


def test_wrap_shows_original_statusline(monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setattr(hook, "LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(hook, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(hook, "update_state", lambda data: hook.__dict__["merge"])  # no file writes
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload("a", 58, 1))))
    assert hook.main(["--wrap", "python3 -c 'import sys,json; print(\"orig:\", json.load(sys.stdin)[\"session_id\"])'"]) == 0
    assert capsys.readouterr().out == "orig: a\n"


def test_wrap_failure_falls_back_to_usage_line(monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setattr(hook, "LOG_PATH", str(tmp_path / "hook.log"))
    monkeypatch.setattr(hook, "update_state", lambda data: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload("a", 58, 1))))
    assert hook.main(["--wrap", "exit 3"]) == 0
    assert capsys.readouterr().out == "7d:58%\n"
