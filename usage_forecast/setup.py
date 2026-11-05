"""Per-machine install / uninstall helpers (called by install.sh and uninstall.sh).

    python -m usage_forecast.setup env --spreadsheet-id ID --credentials PATH [--machine NAME]
    python -m usage_forecast.setup sheets            # check access, create missing sheets
    python -m usage_forecast.setup statusline install|uninstall|status
    python -m usage_forecast.setup cron install|uninstall|status
    python -m usage_forecast.setup remove-machine-data [--machine NAME]
    python -m usage_forecast.setup status

Every change to ~/.claude/settings.json or the crontab is preceded by a backup
in var/backups/. What was changed is remembered in var/install-state.json so
that uninstall can restore the original statusline.
"""
import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = ROOT / "var"
BACKUPS = VAR / "backups"
STATE = VAR / "install-state.json"
HOOK = ROOT / "hooks" / "statusline_hook.py"
RUNNER = ROOT / "bin" / "run.sh"
CRON_MARK = "# claude-usage-forecast"
SETTINGS = Path(os.path.expanduser(os.environ.get("CLAUDE_SETTINGS", "~/.claude/settings.json")))
DEFAULT_CREDENTIALS = Path(os.path.expanduser("~/.config/claude-usage-forecast/service-account.json"))


# ------------------------------------------------------------------ helpers
def _ts():
    return time.strftime("%Y%m%d_%H%M%S")


def _backup_file(path, label):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    os.chmod(VAR, 0o700)
    dst = BACKUPS / ("%s_%s" % (label, _ts()))
    shutil.copy2(path, dst)
    os.chmod(dst, 0o600)
    return dst


def _load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(st):
    VAR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(STATE, 0o600)


def _write_json_atomic(path, obj):
    tmp = path.with_name(".%s.tmp.%d" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def python_for_hook():
    """The hook is stdlib-only; prefer the system python so it works without the venv."""
    return shutil.which("python3") or sys.executable


# ------------------------------------------------------------------ .env
def write_env(spreadsheet_id, credentials, machine):
    env_path = ROOT / ".env"
    current = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                current[k.strip()] = v.strip()
    if spreadsheet_id:
        current["SPREADSHEET_ID"] = spreadsheet_id
    if credentials:
        src = Path(os.path.expanduser(credentials)).resolve()
        if not src.exists():
            raise SystemExit("credentials file not found: %s" % src)
        if src != DEFAULT_CREDENTIALS.resolve():
            DEFAULT_CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(DEFAULT_CREDENTIALS.parent, 0o700)
            shutil.copy2(src, DEFAULT_CREDENTIALS)
        os.chmod(DEFAULT_CREDENTIALS, 0o600)
        current["GOOGLE_APPLICATION_CREDENTIALS"] = str(DEFAULT_CREDENTIALS)
    current.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(DEFAULT_CREDENTIALS))
    current["MACHINE_NAME"] = machine or current.get("MACHINE_NAME") or socket.gethostname().split(".")[0]
    path_hint = os.pathsep.join([os.environ.get("PATH", ""), os.path.expanduser("~/.local/bin"),
                                 "/usr/local/bin", "/opt/homebrew/bin"])
    for key, exe in (("CLAUDE_BIN", "claude"), ("TMUX_BIN", "tmux")):
        found = shutil.which(exe, path=path_hint)
        if found:
            current[key] = found
    current.setdefault("NOTIFY_METHODS", "")
    if not current.get("SPREADSHEET_ID"):
        raise SystemExit("SPREADSHEET_ID is required (--spreadsheet-id)")
    body = "".join("%s=%s\n" % kv for kv in current.items())
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(env_path, 0o600)
    print("wrote .env (machine=%s, claude=%s, tmux=%s)" % (
        current["MACHINE_NAME"], current.get("CLAUDE_BIN", "-"), current.get("TMUX_BIN", "-")))


# ------------------------------------------------------------------ statusline
def _command_mentions_hook(command):
    """True if the statusline command runs our hook directly or through a script that does."""
    if "statusline_hook.py" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for p in parts:
        path = Path(os.path.expanduser(p))
        try:
            if path.is_file() and path.stat().st_size < 200_000 and \
                    "statusline_hook.py" in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def plan_statusline_install(settings, hook_cmd):
    """Return (new_settings, action, original) without touching disk."""
    cur = settings.get("statusLine")
    new = dict(settings)
    if not cur:
        new["statusLine"] = {"type": "command", "command": hook_cmd}
        return new, "set", None
    command = cur.get("command", "") if isinstance(cur, dict) else ""
    if _command_mentions_hook(command):
        return settings, "already", None
    wrapped = dict(cur)
    wrapped["type"] = "command"
    wrapped["command"] = "%s --wrap %s" % (hook_cmd, shlex.quote(command))
    new["statusLine"] = wrapped
    return new, "wrapped", cur


def statusline_install():
    hook_cmd = "%s %s" % (shlex.quote(python_for_hook()), shlex.quote(str(HOOK)))
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    new, action, original = plan_statusline_install(settings, hook_cmd)
    if action == "already":
        print("statusline: already records usage (left unchanged)")
        return
    if SETTINGS.exists():
        print("statusline: backup %s" % _backup_file(SETTINGS, "settings.json"))
    else:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(SETTINGS, new)
    st = _load_state()
    st["statusline"] = {"action": action, "original": original, "installed": new["statusLine"]}
    _save_state(st)
    print("statusline: %s (%s)" % (action, new["statusLine"]["command"]))


def statusline_uninstall():
    if not SETTINGS.exists():
        print("statusline: no settings file")
        return
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    st = _load_state()
    rec = st.get("statusline") or {}
    cur = settings.get("statusLine") or {}
    command = cur.get("command", "") if isinstance(cur, dict) else ""
    if "statusline_hook.py" not in command:
        if _command_mentions_hook(command):
            print("statusline: your statusline script calls the hook itself; remove that line by hand:")
            print("  %s" % command)
        else:
            print("statusline: hook not configured (left unchanged)")
        return
    print("statusline: backup %s" % _backup_file(SETTINGS, "settings.json"))
    new = dict(settings)
    if rec.get("action") == "wrapped" and rec.get("original"):
        new["statusLine"] = rec["original"]
        msg = "restored the original statusline"
    elif " --wrap " in command:
        original_cmd = shlex.split(command.split(" --wrap ", 1)[1])[0]
        new["statusLine"] = dict(cur, command=original_cmd)
        msg = "restored the wrapped command"
    else:
        new.pop("statusLine", None)
        msg = "removed"
    _write_json_atomic(SETTINGS, new)
    st.pop("statusline", None)
    _save_state(st)
    print("statusline: %s" % msg)


# ------------------------------------------------------------------ cron
def _crontab_read():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _crontab_write(text):
    subprocess.run(["crontab", "-"], input=text, text=True, check=True)


def cron_line():
    return "*/30 * * * * %s >/dev/null 2>&1 %s" % (shlex.quote(str(RUNNER)), CRON_MARK)


def _is_ours(line):
    return CRON_MARK in line or "usage_forecast.collector" in line or "usage_probe.py" in line


def plan_cron(current, install):
    kept = [l for l in current.splitlines() if not _is_ours(l)]
    if install:
        kept.append(cron_line())
    return "\n".join(kept) + ("\n" if kept else "")


def cron_install():
    cur = _crontab_read()
    new = plan_cron(cur, True)
    if new == cur:
        print("cron: already installed")
        return
    if cur:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        b = BACKUPS / ("crontab_%s.txt" % _ts())
        b.write_text(cur, encoding="utf-8")
        os.chmod(b, 0o600)
        print("cron: backup %s" % b)
    _crontab_write(new)
    print("cron: %s" % cron_line())


def cron_uninstall():
    cur = _crontab_read()
    new = plan_cron(cur, False)
    if new == cur or (not cur.strip() and not new.strip()):
        print("cron: nothing to remove")
        return
    BACKUPS.mkdir(parents=True, exist_ok=True)
    b = BACKUPS / ("crontab_%s.txt" % _ts())
    b.write_text(cur, encoding="utf-8")
    os.chmod(b, 0o600)
    _crontab_write(new)
    print("cron: removed (backup %s)" % b)


# ------------------------------------------------------------------ sheets
def sheets_setup():
    from .config import load_config
    from .sheets import GSheetClient
    cfg = load_config()
    client = GSheetClient(cfg)
    created = []
    if client.ensure_pace_sheet(ROOT / "templates" / "pace_sheet.json"):
        created.append(cfg.pace_sheet)
    titles = client.sheet_titles()
    if cfg.log_sheet not in titles:
        created.append(cfg.log_sheet)
    client.ensure_log_sheet()
    if client.ensure_chart_sheet():
        created.append(cfg.chart_sheet)
    if cfg.machines_sheet not in client.sheet_titles():
        client._machines_ws(create=True)
        created.append(cfg.machines_sheet)
    print("sheets: %s" % ("created " + ", ".join(created) if created else "all present"))


def remove_machine_data(machine):
    from .config import load_config
    from .sheets import GSheetClient
    cfg = load_config()
    name = machine or cfg.machine_name
    n = GSheetClient(cfg).delete_machine_rows(name)
    print("sheets: removed %d row(s) of machine %r from %s" % (n, name, cfg.machines_sheet))


def status():
    from .config import load_config
    cfg = load_config()
    settings = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    cmd = (settings.get("statusLine") or {}).get("command", "")
    print("machine     : %s" % cfg.machine_name)
    print("spreadsheet : %s" % ("set" if cfg.spreadsheet_id else "NOT SET"))
    print("credentials : %s" % ("ok" if cfg.credentials_path and os.path.exists(cfg.credentials_path) else "missing"))
    print("statusline  : %s" % ("records usage" if _command_mentions_hook(cmd) else "not installed"))
    print("cron        : %s" % ("installed" if any(_is_ours(l) for l in _crontab_read().splitlines())
                                 else "not installed"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("env")
    e.add_argument("--spreadsheet-id")
    e.add_argument("--credentials")
    e.add_argument("--machine")
    sub.add_parser("sheets")
    for name in ("statusline", "cron"):
        p = sub.add_parser(name)
        p.add_argument("action", choices=["install", "uninstall", "status"])
    r = sub.add_parser("remove-machine-data")
    r.add_argument("--machine")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "env":
        write_env(a.spreadsheet_id, a.credentials, a.machine)
    elif a.cmd == "sheets":
        sheets_setup()
    elif a.cmd == "statusline":
        {"install": statusline_install, "uninstall": statusline_uninstall, "status": status}[a.action]()
    elif a.cmd == "cron":
        {"install": cron_install, "uninstall": cron_uninstall, "status": status}[a.action]()
    elif a.cmd == "remove-machine-data":
        remove_machine_data(a.machine)
    elif a.cmd == "status":
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
