import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hooks"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_alerts_file(monkeypatch, tmp_path):
    """Keep the statusline hook from reading this machine's real alert file."""
    import statusline_hook
    monkeypatch.setattr(statusline_hook, "ALERTS_PATH", str(tmp_path / "no-alerts.json"))
