"""data/state/technicals_v3.json helper for the meta-labelling work: merge-updates key "meta" only.

The file is shared with the parallel Technicals v3 work (keys "b1", "b5", ...). Every update re-reads the file, replaces
only key "meta" (merged one level deep), writes a temp file and swaps it in atomically (retries on Windows locks), so
the other writer's keys are never clobbered (last-writer-wins per key).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

KEY = "meta"


def path(settings: Any = None) -> Path:
    from app.core.config import get_settings  # noqa: PLC0415

    s = settings or get_settings()
    p = s.data_dir / "state" / "technicals_v3.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_all(settings: Any = None) -> dict[str, Any]:
    p = path(settings)
    for _ in range(5):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (ValueError, OSError):
            time.sleep(0.2)
    return {}


def read(settings: Any = None) -> dict[str, Any]:
    v = read_all(settings).get(KEY)
    return v if isinstance(v, dict) else {}


def update(settings: Any = None, **kv: Any) -> dict[str, Any]:
    """Merge kv into state["meta"] (dict values merged one more level deep)."""
    p = path(settings)
    st = read_all(settings)
    cur = st.get(KEY) if isinstance(st.get(KEY), dict) else {}
    for k, v in kv.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k] = {**cur[k], **v}
        else:
            cur[k] = v
    st = read_all(settings)                       # re-read right before the swap (the other agent writes too)
    st[KEY] = cur
    tmp = p.with_name(f"{p.name}.meta.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    for _ in range(20):
        try:
            tmp.replace(p)
            break
        except PermissionError:
            time.sleep(0.2)
    return cur


def parallel_features(settings: Any = None) -> dict[str, Any]:
    """Meta features built from the parallel #1 / #5 work once it announces "b1" / "b5" with status "ready":
    {"b1": {"features": features.B1_FEATURES (passing factors only), "raw": ...}, "b5": {...}}."""
    from app.techmeta import features as F  # noqa: PLC0415

    st = read_all(settings)
    out: dict[str, Any] = {}
    for k in ("b1", "b5"):
        v = st.get(k)
        status = v.get("status") if isinstance(v, dict) else v
        if status != "ready":
            continue
        feats = list(F.EXTRA_FEATURES[k])
        if k == "b1" and isinstance(v, dict) and v.get("passing_factors") is not None:
            keep = [f for f in ("hi52", "maxret", "ivol") if f in v["passing_factors"]]
            feats = [f for f in feats if any(x in f for x in keep)]
        out[k] = {"features": feats, "raw": v}
    return out
