"""Configuration: non-secret settings in config.json, secrets in .env."""
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path):
    """Minimal .env loader. Existing environment variables win."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    spreadsheet_id: str = ""
    credentials_path: str = ""
    state_file: str = str(Path("~/.claude/usage-state.json").expanduser())
    var_dir: str = str(PROJECT_DIR / "var")
    stale_hours: float = 12.0
    forecast_threshold: float = 100.0
    used_thresholds: List[float] = field(default_factory=lambda: [80.0, 90.0])
    pace_sheet: str = "ペース表"
    log_sheet: str = "ログ"
    chart_sheet: str = "推移グラフ"
    chart_days: float = 14.0
    chart_min_elapsed_h: float = 12.0
    models_file: str = str(Path("~/.claude/usage-models.json").expanduser())
    model_label: str = "Fable"
    model_max_age_h: float = 2.0
    token_prices: dict = field(default_factory=dict)
    ratio_min_used: float = 5.0
    projects_dir: str = "~/.claude/projects"
    machine_name: str = ""
    machines_sheet: str = "端末別集計"
    current_usage_label: str = "現在の使用量(%)"
    reset_weekday_label_prefix: str = "リセット曜日"
    reset_hour_label: str = "リセット時刻(時)"
    notify_methods: List[str] = field(default_factory=list)


def load_config(config_path=None, env_path=None):
    load_dotenv(env_path or PROJECT_DIR / ".env")
    raw = {}
    cp = Path(config_path or PROJECT_DIR / "config.json")
    if cp.exists():
        raw = json.loads(cp.read_text(encoding="utf-8"))
    th = raw.get("thresholds", {})
    sh = raw.get("sheets", {})
    cfg = Config(
        spreadsheet_id=os.environ.get("SPREADSHEET_ID", ""),
        credentials_path=os.path.expanduser(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")),
        stale_hours=float(raw.get("stale_hours", 12)),
        forecast_threshold=float(th.get("forecast_percent", 100)),
        used_thresholds=[float(x) for x in th.get("used_percent", [80, 90])],
        notify_methods=[m.strip().lower() for m in os.environ.get("NOTIFY_METHODS", "").split(",") if m.strip()],
    )
    cfg.machine_name = os.environ.get("MACHINE_NAME") or socket.gethostname().split(".")[0]
    if os.environ.get("USAGE_MODELS_FILE"):
        cfg.models_file = os.path.expanduser(os.environ["USAGE_MODELS_FILE"])
    if os.environ.get("USAGE_STATE_FILE"):
        cfg.state_file = os.path.expanduser(os.environ["USAGE_STATE_FILE"])
    tk = raw.get("tokens", {})
    cfg.token_prices = tk.get("prices", {})
    cfg.ratio_min_used = float(tk.get("ratio_min_used_percent", 5))
    for key in ("pace_sheet", "log_sheet", "chart_sheet", "machines_sheet", "model_label", "current_usage_label",
                "reset_weekday_label_prefix", "reset_hour_label"):
        if key in sh:
            setattr(cfg, key, sh[key])
    return cfg
