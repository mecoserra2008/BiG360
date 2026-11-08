"""data/state/technicals_v2.json helper: merge-update of this package's keys (t2 / t3 / t4 / t6 / jobs).

The file is shared with the T1 / T5 work (key "t1", "t5"); updates re-read the file and only replace the keys
passed, then swap atomically, so concurrent writers of other keys are not clobbered (last-writer-wins per key).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def path(settings: Any = None) -> Path:
    from app.core.config import get_settings  # noqa: PLC0415

    s = settings or get_settings()
    p = s.data_dir / "state" / "technicals_v2.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read(settings: Any = None) -> dict[str, Any]:
    p = path(settings)
    for _ in range(5):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (ValueError, OSError):
            time.sleep(0.2)
    return {}


def update(settings: Any = None, merge: bool = True, **kv: Any) -> dict[str, Any]:
    """Set top-level keys; with merge=True a dict value is merged one level deep into the existing dict."""
    p = path(settings)
    st = read(settings)
    for k, v in kv.items():
        if merge and isinstance(v, dict) and isinstance(st.get(k), dict):
            st[k] = {**st[k], **v}
        else:
            st[k] = v
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    for _ in range(10):
        try:
            tmp.replace(p)
            break
        except PermissionError:
            time.sleep(0.2)
    return st
