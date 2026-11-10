"""Token usage from Claude Code transcripts on this machine.

Each API response is recorded in ~/.claude/projects/**/*.jsonl as one or more
"assistant" entries sharing message.id; partial entries can carry smaller
output_tokens, so every field takes the maximum over the entries of one id.

Costs are API-list-price equivalents (what the same tokens would cost on the
Claude API), not what the subscription is billed. Prices live in config.json.
"""
import glob
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


@dataclass
class Msg:
    ts: float
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    session: str = ""
    agent: str = ""  # "" for the main conversation, else the subagent transcript name
    project: str = ""


@dataclass
class Totals:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cost: float = 0.0
    unpriced_models: set = field(default_factory=set)
    messages: int = 0

    @property
    def non_cache_read(self):
        """Input + output + cache writes: the tokens that are not cheap cache hits."""
        return self.input + self.output + self.cache_write_5m + self.cache_write_1h

    @property
    def total(self):
        return self.non_cache_read + self.cache_read


def _iso_to_ts(s):
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def source_of(path, base):
    """(project, session, agent) of a transcript path under the projects dir."""
    rel = os.path.relpath(path, base).split(os.sep)
    project = rel[0]
    session = rel[1].split(".")[0] if len(rel) > 1 else ""
    agent = "" if len(rel) <= 2 else os.path.splitext(rel[-1])[0]
    return project, session, agent


def msg_from_entry(d):
    """(key, Msg) for an assistant transcript entry with usage, else None."""
    if d.get("type") != "assistant":
        return None
    m = d.get("message")
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
        return None
    model = str(m.get("model") or "")
    if not model or model.startswith("<"):
        return None  # synthetic/local messages
    u = m["usage"]
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    cw_total = int(u.get("cache_creation_input_tokens") or 0)
    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    cw5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if cw1h + cw5m == 0 and cw_total:
        cw5m = cw_total  # older records without the split
    try:
        ts = _iso_to_ts(d.get("timestamp"))
    except (TypeError, ValueError, AttributeError):
        return None
    key = m.get("id") or d.get("requestId") or d.get("uuid")
    return key, Msg(ts, model, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0), cw5m, cw1h)


def merge_msg(msgs, key, new):
    old = msgs.get(key)
    if old is None:
        msgs[key] = new
    else:
        old.ts = min(old.ts, new.ts)
        for f_ in FIELDS:
            setattr(old, f_, max(getattr(old, f_), getattr(new, f_)))


def transcript_paths(projects_dir, since_ts=None):
    base = os.path.expanduser(projects_dir)
    for path in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True):
        try:
            if since_ts is not None and os.path.getmtime(path) < since_ts:
                continue
        except OSError:
            continue
        yield base, path


def scan(projects_dir="~/.claude/projects", since_ts=None):
    """Return {message_id: Msg}. Files untouched since `since_ts` are skipped."""
    msgs: Dict[str, Msg] = {}
    for base, path in transcript_paths(projects_dir, since_ts):
        project, session, agent = source_of(path, base)
        try:
            f = open(path, encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with f:
            for line in f:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    got = msg_from_entry(json.loads(line))
                except ValueError:
                    continue
                if got is None:
                    continue
                key, new = got
                new.project, new.session, new.agent = project, session, agent
                merge_msg(msgs, key, new)
    return msgs


def price_for(model, prices):
    """prices: {model_id: {"input": $/MTok, "output": ..., "cache_read": ...}}; '[1m]' suffix ignored."""
    base = model.split("[")[0]
    p = prices.get(base)
    if p is None:
        # longest prefix match, e.g. dated ids
        cands = [k for k in prices if base.startswith(k)]
        p = prices[max(cands, key=len)] if cands else None
    return p


def msg_cost(msg, prices) -> Optional[float]:
    p = price_for(msg.model, prices)
    if p is None:
        return None
    inp = float(p["input"])
    return (msg.input * inp
            + msg.output * float(p["output"])
            + msg.cache_read * float(p.get("cache_read", inp * 0.1))
            + msg.cache_write_5m * float(p.get("cache_write_5m", inp * 1.25))
            + msg.cache_write_1h * float(p.get("cache_write_1h", inp * 2.0))) / 1e6


def summarize(msgs, start_ts, end_ts, prices, model_contains=None, predicate=None) -> Totals:
    t = Totals()
    for m in msgs.values():
        if not (start_ts <= m.ts <= end_ts):
            continue
        if model_contains and model_contains.lower() not in m.model.lower():
            continue
        if predicate is not None and not predicate(m):
            continue
        t.messages += 1
        for f_ in FIELDS:
            setattr(t, f_, getattr(t, f_) + getattr(m, f_))
        c = msg_cost(m, prices)
        if c is None:
            t.unpriced_models.add(m.model)
        else:
            t.cost += c
    return t


def exhausted_predicate(intervals):
    """Messages sent while a plan limit was at 100% (billed to usage credits if they are on)."""
    def pred(m):
        return any(a <= m.ts < b for a, b in intervals)
    return pred


if __name__ == "__main__":  # quick manual check: this week's totals
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from usage_forecast.config import load_config
    cfg = load_config()
    now = time.time()
    ms = scan(since_ts=now - 8 * 86400)
    t = summarize(ms, now - 7 * 86400, now, cfg.token_prices)
    print("last 7 days: msgs=%d non-cache-read=%.2fM cache-read=%.2fM cost=$%.2f unpriced=%s" % (
        t.messages, t.non_cache_read / 1e6, t.cache_read / 1e6, t.cost, sorted(t.unpriced_models)))
