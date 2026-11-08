"""Locations of Data Ops configuration and state (no imports of job modules: safe to use anywhere).

    config_dir()        <project>/config            (override BIG360_CONFIG_DIR) - schedule.yaml, ops.yaml
    ops_state_dir()     data/state/ops              filler.json, budgets.json, alerts.json, coverage_matrix.json ...
    read_json / write_json_atomic                   small state files (atomic replace, retried on Windows locks)
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings, get_settings


def config_dir(settings: Settings | None = None) -> Path:
    env = os.environ.get("BIG360_CONFIG_DIR")
    if env:
        return Path(env)
    return (settings or get_settings()).project_root / "config"


def ops_state_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).state_dir / "ops"
    p.mkdir(parents=True, exist_ok=True)
    return p


def app_logs_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).logs_dir / "app"
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def read_json(path: Path, default: Any = None) -> Any:
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (ValueError, PermissionError, OSError):
            time.sleep(0.05)
    return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str, sort_keys=False), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    os.replace(tmp, path)


def ops_config(settings: Settings | None = None) -> dict[str, Any]:
    """config/ops.yaml (governor, housekeeping, filler) merged over built-in defaults."""
    from app.ops.governor import DEFAULTS as GOV
    from app.ops.housekeeping import DEFAULTS as HK

    doc = read_yaml(config_dir(settings) / "ops.yaml")
    out = {"governor": dict(GOV), "housekeeping": dict(HK), "filler": {}}
    for k in out:
        out[k].update(doc.get(k) or {})
    return out
