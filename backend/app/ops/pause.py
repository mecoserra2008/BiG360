"""PAUSE flag files with an expiry (coverage v3 A4, 2026-11). Owner: platform.

Before 2026-11 a pause was "the file exists": data/state/ops/filler.PAUSE written by the orchestrator on 2026-10-24
during the API restart loop was never removed, so the gap filler stayed paused for 13 days while the UI said "paused
by user" (docs/audits/APP_MAP_AND_GAPS_2026-11.md section 3 #3). Rules now:

* a pause file carries `until=<ISO timestamp>` (key=value lines; a JSON object with "until" is accepted too), plus
  optional `reason=`, `by=` and `since=` lines. The pause is active while now < until;
* new pauses written through write_pause() (tray, CLI, API helpers) get until = now + DEFAULT_HOURS (24 h) unless an
  explicit until / hours is given;
* a file without a usable `until` is a legacy pause (plain text, an empty `New-Item` file, or an unparseable until):
  it counts from its modification time and expires after LEGACY_MAX_DAYS (7 days), with a logged warning (once per
  file version per process);
* an expired file is ignored, never deleted here: whoever wrote it (or the operator) removes it. Its state is
  reported (`expired`, `expired_at`) on GET /system/status (`pauses`) and Data Health (filler block, `pause`).

read_pause() is pure over (text, mtime, now) via parse_pause(), so the rules are unit-tested without files.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

DEFAULT_HOURS = 24.0
LEGACY_MAX_DAYS = 7.0
_KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*?)\s*$")
_UNTIL_ANY = re.compile(r"\buntil\s*=\s*(\S+)", re.IGNORECASE)
_WARNED: set[tuple[str, float]] = set()


@dataclass
class PauseState:
    name: str
    path: str
    present: bool = False           # the file exists
    active: bool = False            # present, and not expired
    expired: bool = False
    legacy: bool = False            # no usable until: counts from mtime, LEGACY_MAX_DAYS
    reason: str | None = None
    by: str | None = None
    since: str | None = None        # ISO: `since=` line, else the file's modification time
    until: str | None = None        # ISO expiry (explicit or legacy mtime + 7 d)
    expired_at: str | None = None
    age_h: float | None = None
    remaining_h: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat(timespec="seconds") if t else None


def parse_ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _fields(text: str) -> dict[str, str]:
    """until / reason / by / since from a JSON object or key=value lines (unknown keys kept, first line wins)."""
    t = (text or "").strip()
    if t.startswith("{"):
        try:
            doc = json.loads(t)
            if isinstance(doc, dict):
                return {str(k).lower(): str(v) for k, v in doc.items() if v is not None}
        except ValueError:
            pass
    out: dict[str, str] = {}
    for line in t.splitlines():
        m = _KV.match(line)
        if m and m.group(1).lower() in ("until", "reason", "by", "since", "owner"):
            out.setdefault(m.group(1).lower(), m.group(2))
    if "until" not in out:   # legacy free text with an appended "until=..." somewhere
        m = _UNTIL_ANY.search(t)
        if m:
            out["until"] = m.group(1)
    return out


def parse_pause(name: str, path: str, text: str | None, mtime: float | None, now: datetime | None = None,
                legacy_max_days: float = LEGACY_MAX_DAYS) -> PauseState:
    """State of a pause file from its text and modification time (text None = no file)."""
    now = now or datetime.now(UTC)
    st = PauseState(name=name, path=path)
    if text is None:
        return st
    st.present = True
    f = _fields(text)
    mt = datetime.fromtimestamp(mtime, UTC) if mtime else now
    since = parse_ts(f.get("since")) or mt
    until = parse_ts(f.get("until"))
    st.by = f.get("by") or f.get("owner") or None
    st.reason = f.get("reason") or None
    if not st.reason and not (text or "").strip().startswith("{"):   # legacy free text: its first plain line
        plain = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not _KV.match(ln)
                 and not _UNTIL_ANY.search(ln)]
        st.reason = plain[0][:300] if plain else None
    if until is None:
        st.legacy = True
        until = mt + timedelta(days=legacy_max_days)
        st.detail = (f"legacy pause file without until=: expires {legacy_max_days:g} days after it was written"
                     + (" (until= unparseable)" if f.get("until") else ""))
    st.since, st.until = _iso(since), _iso(until)
    st.age_h = round((now - since).total_seconds() / 3600, 2)
    if now >= until:
        st.expired, st.active = True, False
        st.expired_at = _iso(until)
        st.remaining_h = 0.0
        st.detail = (st.detail + "; " if st.detail else "") + f"expired {_iso(until)}: ignored (remove the file)"
    else:
        st.active = True
        st.remaining_h = round((until - now).total_seconds() / 3600, 2)
    return st


def read_pause(path: Path, name: str | None = None, now: datetime | None = None, warn: bool = True) -> PauseState:
    name = name or path.stem
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return PauseState(name=name, path=str(path))
    except OSError as e:   # locked mid-write: treat as present and active (conservative) for this tick
        return PauseState(name=name, path=str(path), present=True, active=True, detail=f"unreadable: {e}"[:200])
    st = parse_pause(name, str(path), text, mtime, now)
    if warn and st.expired:
        key = (str(path), float(mtime))
        if key not in _WARNED:
            _WARNED.add(key)
            log.warning("%s: pause file %s expired at %s (%s) - ignored; remove it to clear this warning",
                        name, path, st.expired_at, "legacy, no until=" if st.legacy else "until passed")
    return st


def render(reason: str | None, by: str | None, until: datetime, since: datetime | None = None) -> str:
    since = since or datetime.now(UTC)
    lines = [f"until={_iso(until)}", f"since={_iso(since)}"]
    if by:
        lines.append(f"by={by}")
    if reason:
        lines.append("reason=" + " ".join(str(reason).split())[:500])
    return "\n".join(lines) + "\n"


def write_pause(path: Path, reason: str | None = None, by: str | None = None, hours: float | None = None,
                until: datetime | None = None, now: datetime | None = None) -> PauseState:
    """Create / replace a pause file. Default expiry: now + DEFAULT_HOURS (a pause never outlives its reason)."""
    now = now or datetime.now(UTC)
    until = until or now + timedelta(hours=float(hours if hours is not None else DEFAULT_HOURS))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(render(reason, by, until, now), encoding="utf-8")
    os.replace(tmp, path)
    return read_pause(path, now=now, warn=False)


def clear_pause(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ------------------------------------------------------------------------------------------------ known flags


def filler_pause_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "ops" / "filler.PAUSE"


def filler_pause(settings: Settings | None = None, now: datetime | None = None, warn: bool = True) -> PauseState:
    return read_pause(filler_pause_path(settings), "filler", now=now, warn=warn)


def pause_status(settings: Settings | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every operator pause flag (GET /system/status `pauses`). Only present files are listed."""
    out = []
    for st in (filler_pause(settings, now, warn=False),):
        if st.present:
            out.append(st.to_dict())
    return out


__all__ = ["DEFAULT_HOURS", "LEGACY_MAX_DAYS", "PauseState", "clear_pause", "filler_pause", "filler_pause_path",
           "parse_pause", "pause_status", "read_pause", "render", "runtime_status", "write_pause"]


def runtime_status(settings: Settings | None = None) -> dict[str, Any]:
    """Operator runtime state for GET /system/status (coverage v3 A2 / A4): `pauses` (pause flags with reason and
    expiry), `heavy_slot` (holder / queue / peers of the >= 7 GB slot) and `admission_waits` (jobs of every runner
    waiting for RAM, the floor or the slot, with the reason). Each part degrades to an empty value on its own."""
    out: dict[str, Any] = {"pauses": [], "heavy_slot": None, "admission_waits": []}
    try:
        out["pauses"] = pause_status(settings)
    except Exception as e:  # noqa: BLE001 - the System page must always render
        log.debug("pause status failed: %s", e)
    try:
        from app.ops.heavy_slot import slot_status  # noqa: PLC0415

        out["heavy_slot"] = slot_status(settings)
    except Exception as e:  # noqa: BLE001
        log.debug("heavy slot status failed: %s", e)
    try:
        from app.ops.ram_budget import waits  # noqa: PLC0415

        out["admission_waits"] = waits(settings)[:50]
    except Exception as e:  # noqa: BLE001
        log.debug("admission waits failed: %s", e)
    return out


def main(argv: list[str] | None = None) -> int:
    """python -m app.ops.pause status | set [--reason R] [--by B] [--hours H | --until ISO] | clear  (filler.PAUSE)."""
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="app.ops.pause", description="Gap filler pause flag with an expiry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print the flag state (reason, until, expired)")
    st = sub.add_parser("set", help="pause the gap filler until a time (default now + 24 h)")
    st.add_argument("--reason", default=None)
    st.add_argument("--by", default="cli")
    st.add_argument("--hours", type=float, default=None)
    st.add_argument("--until", default=None, help="ISO timestamp (UTC if no offset)")
    sub.add_parser("clear", help="remove the flag (the filler resumes within ~15 s)")
    a = ap.parse_args(argv)
    path = filler_pause_path()
    if a.cmd == "set":
        until = parse_ts(a.until) if a.until else None
        if a.until and until is None:
            ap.error(f"--until {a.until!r} is not an ISO timestamp")
        res = write_pause(path, reason=a.reason, by=a.by, hours=a.hours, until=until)
    elif a.cmd == "clear":
        res = PauseState(name="filler", path=str(path), detail="removed" if clear_pause(path) else "no flag")
    else:
        res = filler_pause(warn=False)
    print(json.dumps(res.to_dict(), indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
