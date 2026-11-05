import pytest

from usage_forecast import tokens

PRICES = {"claude-fable-5-1": {"input": 10, "output": 50, "cache_read": 0.25}}


def test_cost_uses_cache_multipliers_and_1m_suffix():
    m = tokens.Msg(0, "claude-fable-5-1[1m]", input=1_000_000, output=0, cache_read=1_000_000,
                   cache_write_5m=1_000_000, cache_write_1h=1_000_000)
    # 10 + 0.25 + 12.5 (1.25x) + 20 (2x)
    assert tokens.msg_cost(m, PRICES) == pytest.approx(42.75)


def test_unknown_model_is_reported_not_priced():
    msgs = {"a": tokens.Msg(5, "claude-new-9", output=10)}
    t = tokens.summarize(msgs, 0, 10, PRICES)
    assert t.output == 10 and t.cost == 0 and t.unpriced_models == {"claude-new-9"}


def test_summarize_window_and_model_filter():
    msgs = {"a": tokens.Msg(5, "claude-fable-5-1", output=1_000_000),
            "b": tokens.Msg(50, "claude-fable-5-1", output=1_000_000),
            "c": tokens.Msg(6, "claude-opus-5-5", output=1)}
    t = tokens.summarize(msgs, 0, 10, PRICES, model_contains="fable")
    assert t.messages == 1 and t.cost == pytest.approx(50.0)
