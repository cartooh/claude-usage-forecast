"""Read plan usage (incl. per-model weekly limits such as Fable) from Claude Code's /usage screen.

The statusline JSON only carries five_hour and seven_day (all models). The
per-model weekly figure is shown only on the interactive /usage screen, so
this script starts Claude Code in a detached tmux session, opens /usage,
parses the text, and exits. No prompt is sent to a model.

Output: ~/.claude/usage-models.json (atomic write)
  {"observed_at": <unix>, "windows": {"Fable": {"used_percentage": 5, "resets_at": <unix>,
   "resets_text": "Nov 5, 11am (Asia/Tokyo)"}, "all models": {...}, "session": {...}}}

Usage: python3 scripts/usage_probe.py [--print]
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import shutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from usage_forecast.config import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")  # CLAUDE_BIN / TMUX_BIN written by the installer (cron has a minimal PATH)
_PATH = os.pathsep.join([os.environ.get("PATH", ""), os.path.expanduser("~/.local/bin"),
                         "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin"])
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude", path=_PATH) or os.path.expanduser("~/.local/bin/claude")
TMUX = os.environ.get("TMUX_BIN") or shutil.which("tmux", path=_PATH) or "/usr/bin/tmux"
OUT = Path(os.path.expanduser(os.environ.get("USAGE_MODELS_FILE", "~/.claude/usage-models.json")))
WORKDIR = Path(__file__).resolve().parent.parent / "var" / "probe"
# Remote Control at startup would register a visible session on every probe.
SETTINGS = json.dumps({"remoteControlAtStartup": False})
LOG = Path(__file__).resolve().parent.parent / "var" / "probe.log"

HEADER_RE = re.compile(r"^\s*Current (week|session)(?: \(([^)]+)\))?\s*$")
USED_RE = re.compile(r"(\d+(?:\.\d+)?)% used")
RESETS_RE = re.compile(r"^\s*Resets (.+?)\s*$")


def log(msg):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 512 * 1024:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def parse_resets(text, now):
    """'Nov 5, 11am (Asia/Tokyo)' / '11:30am (Asia/Tokyo)' -> unix seconds (next occurrence)."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", text)
    body, tzname = (m.group(1), m.group(2)) if m else (text, "Asia/Tokyo")
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("Asia/Tokyo")
    local_now = datetime.fromtimestamp(now, tz)
    body = body.strip().replace(" ", " ")
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%I:%M%p", "%I%p"):
        try:
            t = datetime.strptime(body.upper() if "%p" in fmt else body, fmt)
        except ValueError:
            continue
        if "%b" in fmt:
            dt = t.replace(year=local_now.year, tzinfo=tz)
            if dt.timestamp() < now - 86400:
                dt = dt.replace(year=local_now.year + 1)
        else:
            dt = local_now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if dt.timestamp() < now - 60:
                dt = dt.fromtimestamp(dt.timestamp() + 86400, tz)
        return int(dt.timestamp())
    return None


def parse_usage_screen(text, now):
    """Return {label: {used_percentage, resets_at, resets_text}} from the /usage screen text."""
    windows = {}
    current = None
    for line in text.splitlines():
        h = HEADER_RE.match(line)
        if h:
            current = "session" if h.group(1) == "session" else (h.group(2) or "week")
            windows[current] = {}
            continue
        if current is None:
            continue
        u = USED_RE.search(line)
        if u and "used_percentage" not in windows[current]:
            windows[current]["used_percentage"] = float(u.group(1))
            continue
        r = RESETS_RE.match(line)
        if r and "resets_text" not in windows[current]:
            windows[current]["resets_text"] = r.group(1)
            windows[current]["resets_at"] = parse_resets(r.group(1), now)
            current = None
    return {k: v for k, v in windows.items() if "used_percentage" in v}


def parse_usage_credits(text):
    """Text of the 'Usage credits' section (pay-as-you-go), and any dollar amounts in it."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.strip() == "Usage credits":
            for nxt in lines[i + 1:]:
                t = re.sub(r"\s*[↑↓]\s*$", "", nxt).strip()
                if not t or t.startswith("Esc to") or t.startswith("Current "):
                    break
                out.append(t)
            break
    joined = " / ".join(out)
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)", joined)]
    return {"text": joined, "amounts_usd": amounts} if out else None


def _tmux(*args, check=True):
    return subprocess.run([TMUX, *args], capture_output=True, text=True, check=check, timeout=20)


def capture(name):
    return _tmux("capture-pane", "-p", "-t", name, "-S", "-200").stdout


def wait_for(name, predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        text = capture(name)
        if predicate(text):
            return text
        time.sleep(0.5)
    return None


def probe():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    name = "usage-probe-%d" % os.getpid()
    cmd = "%s --model haiku --settings '%s'" % (CLAUDE, SETTINGS)
    _tmux("new-session", "-d", "-s", name, "-x", "160", "-y", "60", "-c", str(WORKDIR), cmd)
    try:
        text = wait_for(name, lambda t: "❯" in t or "trust this folder" in t, 40)
        if text is None:
            raise RuntimeError("Claude Code did not start")
        if "trust this folder" in text and "Yes, I trust" in text:
            _tmux("send-keys", "-t", name, "Down", "Enter")  # dedicated empty folder
            if wait_for(name, lambda t: "❯" in t and "trust this folder" not in t, 30) is None:
                raise RuntimeError("trust prompt did not close")
        time.sleep(1.0)
        _tmux("send-keys", "-t", name, "-l", "/usage")
        time.sleep(0.8)
        _tmux("send-keys", "-t", name, "Enter")
        text = wait_for(name, lambda t: "Current week" in t and "Resets" in t.split("Current week")[-1], 30)
        if text is None:
            raise RuntimeError("usage screen did not appear")
        time.sleep(1.5)  # let every section render
        text = capture(name)
        # the usage-credits section sits below the fold of the scrollable panel
        for _ in range(3):
            if "Usage credits" in text.split("What's contributing")[-1]:
                break
            _tmux("send-keys", "-t", name, "Down", check=False)
            time.sleep(0.3)
            _tmux("send-keys", "-t", name, "PageDown", check=False)
            time.sleep(0.8)
            text = text + "\n" + capture(name)
        _tmux("send-keys", "-t", name, "Escape", check=False)
        return text
    finally:
        _tmux("kill-session", "-t", name, check=False)


def write_atomic(path, obj):
    tmp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    now = time.time()
    if not os.path.exists(TMUX) or not os.path.exists(CLAUDE):
        log("skipped: tmux or claude not found (TMUX=%s CLAUDE=%s)" % (TMUX, CLAUDE))
        return 0
    try:
        text = probe()
        windows = parse_usage_screen(text, now)
        if not any(k not in ("session", "all models") for k in windows):
            log("no per-model window found; windows=%s" % sorted(windows))
        if not windows:
            raise RuntimeError("could not parse usage screen")
        out = {"observed_at": int(now), "windows": windows, "usage_credits": parse_usage_credits(text)}
        write_atomic(OUT, out)
        log("ok %s" % json.dumps({k: v.get("used_percentage") for k, v in windows.items()}, ensure_ascii=False))
        if "--print" in sys.argv:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    except Exception as e:
        log("failed: %s %s" % (type(e).__name__, e))
        if "--print" in sys.argv:
            print("failed: %s %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
