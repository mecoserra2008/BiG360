"""'Update available' check against a folder or share (config/app.yaml update_dir or env BIG360_UPDATE_DIR).

The folder holds either a latest.json  {"version": "1.0.1", "file": "BiG360Setup-1.0.1.exe", "notes": "..."}
or plain build files named BiG360Setup-<version>.exe / BiG360-<version>-portable.zip (the newest version wins).
packaging/build.ps1 -PublishTo <dir> writes both. A full auto-updater can come later (O3.6).

    python -m app.desktop.updates            # prints the check result
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.desktop.appconfig import app_config, app_version, parse_version

_FILE_RE = re.compile(r"^BiG360(?:Setup)?-(\d+(?:\.\d+)*)(?:-portable)?\.(?:exe|zip)$", re.IGNORECASE)


def scan_update_dir(folder: Path) -> dict[str, Any] | None:
    """Newest build offered by `folder` -> {"version", "file", "notes"} or None."""
    if not folder.is_dir():
        return None
    latest = folder / "latest.json"
    if latest.is_file():
        try:
            doc = json.loads(latest.read_text(encoding="utf-8"))
            if doc.get("version"):
                f = doc.get("file")
                return {"version": str(doc["version"]), "file": str(folder / f) if f else None,
                        "notes": doc.get("notes")}
        except (OSError, ValueError):
            pass
    best: tuple[tuple[int, ...], Path, str] | None = None
    for p in folder.iterdir():
        m = _FILE_RE.match(p.name)
        if not m:
            continue
        v = parse_version(m.group(1))
        # prefer the installer over the zip for the same version
        if best is None or v > best[0] or (v == best[0] and p.suffix.lower() == ".exe"):
            best = (v, p, m.group(1))
    if best is None:
        return None
    return {"version": best[2], "file": str(best[1]), "notes": None}


def check_for_update(current: str | None = None, folder: str | Path | None = None) -> dict[str, Any]:
    """{"enabled", "current", "available": bool, "latest", "file", "notes", "folder", "error"}."""
    cur = current or app_version()
    src = folder if folder is not None else app_config().get("update_dir")
    out: dict[str, Any] = {"enabled": bool(src), "current": cur, "available": False, "latest": None, "file": None,
                           "notes": None, "folder": str(src) if src else None, "error": None}
    if not src:
        return out
    try:
        found = scan_update_dir(Path(src))
    except OSError as e:  # unreachable share
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    if found is None:
        return out
    out.update(latest=found["version"], file=found["file"], notes=found["notes"])
    out["available"] = parse_version(found["version"]) > parse_version(cur)
    return out


def main() -> int:
    r = check_for_update()
    if not r["enabled"]:
        print(f"BiG360 {r['current']}: update check off (set update_dir in config/app.yaml or BIG360_UPDATE_DIR)")
    elif r["error"]:
        print(f"BiG360 {r['current']}: update folder unreachable: {r['error']}")
    elif r["available"]:
        print(f"BiG360 {r['current']}: update available -> {r['latest']} ({r['file']})")
    else:
        print(f"BiG360 {r['current']}: up to date (folder {r['folder']}, latest {r['latest'] or '-'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
