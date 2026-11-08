"""config/app.yaml (tray / supervisor app-mode settings) and the app version file (<root>/VERSION)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.ops.paths import config_dir, read_yaml

DEFAULTS: dict[str, Any] = {
    "api_host": "127.0.0.1", "api_port": 8010, "serve_ui": True, "filler": True, "schedule": True, "realtime": False,
    "live_news": True, "live_quotes": True, "notify_news_alerts": True,
    "start_supervisor": True, "status_poll_s": 60, "notifications": True, "notify_severities": ["error", "warn"],
    "update_dir": "", "update_check_h": 24,
}


def app_config(settings: Settings | None = None) -> dict[str, Any]:
    doc = read_yaml(config_dir(settings) / "app.yaml")
    out = dict(DEFAULTS)
    out.update({k: v for k, v in doc.items() if k != "version" and v is not None})
    if os.environ.get("BIG360_SUPERVISOR_API_PORT"):
        out["api_port"] = int(os.environ["BIG360_SUPERVISOR_API_PORT"])
    if os.environ.get("BIG360_UPDATE_DIR"):
        out["update_dir"] = os.environ["BIG360_UPDATE_DIR"]
    return out


def version_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).project_root / "VERSION"


def app_version(settings: Settings | None = None) -> str:
    try:
        v = version_file(settings).read_text(encoding="utf-8").strip()
        return v or "0.0.0"
    except OSError:
        return "0.0.0"


def parse_version(v: str) -> tuple[int, ...]:
    """'1.2.10' -> (1, 2, 10); non-numeric suffixes are ignored ('1.2.0-rc1' -> (1, 2, 0))."""
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out)
