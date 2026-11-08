"""Leaked-process reaper (incident 2026-10-24): finds and kills OUR processes that outlived their owner.

    find_leaked()        multiprocessing spawn children ("from multiprocessing.spawn import spawn_main") whose parent
                         is dead, running our interpreter (the venv python or its base python) with a current
                         directory inside this installation
    reap()               kills them (whole remaining tree) and returns metrics for supervisor.json / Data Health
    find_stray_apis()    `app.cli serve api` processes of our interpreter that the supervisor does not own

Zombies: a process stuck INSIDE its exit (exit code set, process object not signalled; 2026-10-24: three job
processes that took the console's CTRL+BREAK default handler, 1 thread in WrAlertByThreadId, no handles left, ~0.9 GB
private each, for 4-6 h). TerminateProcess cannot cut a terminating process short, so they are classified
`zombie`, reported (count, private MB, first seen) and never retried; only their own exit (or a reboot) frees them.
Alert hygiene 2026-10-30: every process passes through that state while it frees its memory, which takes seconds
to minutes for a multi-GB process (07:27:52 UTC: the sync's confluence_zones job process, 2.8 GB private, caught
by a sweep 0.2 s after the job ended, gone by the next sweep). A process is therefore only a zombie once it has
been seen in its exit for at least ZOMBIE_MIN_S (two sweeps); before that it is listed under `exiting`
(informational, no alert).
Prevention: job processes run in their own process group and stop-file children get no CTRL+BREAK, so the console
handler never runs ExitProcess in them; kills are external (Job Object) and complete.

A spawn child is useless once its parent died (its only channel is a pipe to the parent), so killing it is always
safe; the ownership checks (interpreter path + command line + cwd under the project root) make sure we never touch
another program's Python. The supervisor runs reap() every REAP_EVERY_S from its loop. Before this fix such orphans
were the 0.3-1 GB/hour creep: `prices_global_yf` job processes of filler tasks whose taskrun parent crashed on
CTRL+BREAK (exit 0xC0000005) - kill_tree() walks parent links and could not see them.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.logsetup import get_logger
from app.ops import winsys

log = get_logger("app.ops.reaper")

SPAWN_MARKERS = ("multiprocessing.spawn", "spawn_main")
MIN_AGE_S = 120.0            # never touch a process younger than this (a parent may still be attaching to it)
REAP_EVERY_S = 300.0
#: in its exit at least this long (seen by two sweeps) -> zombie; younger = normal teardown (`exiting`)
ZOMBIE_MIN_S = 120.0
_FILETIME_PER_S = 10_000_000


def _mono() -> float:   # injectable clock (tests)
    return time.monotonic()


def _norm(p: str | os.PathLike[str] | None) -> str:
    return os.path.normcase(os.path.abspath(str(p))) if p else ""


def our_pythons(project_root: Path | None = None) -> set[str]:
    """Interpreter paths of this installation: the running one, its base, the venv launcher and the base python
    named in .venv/pyvenv.cfg (venv launchers re-exec the base interpreter, which is what the process list shows)."""
    out = {_norm(sys.executable), _norm(getattr(sys, "_base_executable", None))}
    roots = [Path(sys.prefix)]
    if project_root is not None:
        roots.append(project_root / ".venv")
    for venv in roots:
        for name in ("python.exe", "pythonw.exe"):
            out.add(_norm(venv / "Scripts" / name))
        cfg = venv / "pyvenv.cfg"
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                k, _, v = line.partition("=")
                if k.strip().lower() == "home" and v.strip():
                    out.add(_norm(Path(v.strip()) / "python.exe"))
                    out.add(_norm(Path(v.strip()) / "pythonw.exe"))
        except OSError:
            pass
    out.discard("")
    return out


def _under(path: str | None, roots: list[str]) -> bool:
    if not path:
        return False
    p = _norm(path.rstrip("\\/"))
    return any(p == r or p.startswith(r.rstrip("\\/") + os.sep) for r in roots)


def _parent_dead(pid: int, ppid: int, alive: set[int]) -> bool:
    """True when the parent no longer exists (or the pid was reused by a younger process)."""
    if ppid <= 0 or ppid not in alive:
        return True
    pc, cc = winsys.process_create_time(ppid), winsys.process_create_time(pid)
    return pc is not None and cc is not None and pc > cc


#: pid -> (creation FILETIME, first seen ISO) of zombies already reported (logged once, never killed again)
_ZOMBIES_SEEN: dict[int, tuple[int | None, str]] = {}
#: pid -> (creation FILETIME, first seen in exit ISO, first seen monotonic, command line) of processes in their exit
_EXITING_SEEN: dict[int, tuple[int | None, str, float, str | None]] = {}


def find_leaked(project_root: Path | None = None, roots: list[str | Path] | None = None,
                pythons: set[str] | None = None, procs: list[tuple[int, int, str]] | None = None,
                only_pids: set[int] | None = None, min_age_s: float = MIN_AGE_S) -> list[dict[str, Any]]:
    """Leaked spawn children of ours (see module doc). `only_pids` restricts the scan (tests). Each item has
    "zombie": True when the process is stuck in its exit (report only)."""
    if project_root is None:
        from app.core.config import get_settings  # noqa: PLC0415

        project_root = get_settings().project_root
    procs = winsys.process_list() if procs is None else procs
    alive = {p for p, _, _ in procs}
    rts = [_norm(r) for r in (roots or [project_root])]
    pys = pythons or our_pythons(project_root)
    me = os.getpid()
    now_ft = int((time.time() + 11644473600) * _FILETIME_PER_S)
    out: list[dict[str, Any]] = []
    for pid, ppid, exe in procs:
        if pid == me or not exe.lower().startswith("python") or (only_pids is not None and pid not in only_pids):
            continue
        cmd = winsys.process_cmdline(pid) or ""
        if not all(m in cmd for m in SPAWN_MARKERS):
            continue
        if not _parent_dead(pid, ppid, alive):
            continue
        img = winsys.process_image(pid)
        if _norm(img) not in pys:
            continue
        cwd = winsys.process_cwd(pid)
        if not _under(cwd, rts):
            continue
        ct = winsys.process_create_time(pid)
        age = (now_ft - ct) / _FILETIME_PER_S if ct else None
        if age is not None and age < min_age_s:
            continue
        det = winsys.process_memory_detail(pid) or {}
        out.append({"pid": pid, "ppid": ppid, "image": img, "cwd": cwd, "age_s": round(age) if age else None,
                    "rss_mb": round(winsys.tree_memory_mb(pid, procs)), "private_mb": round(det.get("private_mb") or 0),
                    "zombie": winsys.exiting(pid), "create_time": ct, "cmd": cmd[:200]})
    return out


def find_zombies(project_root: Path | None = None, procs: list[tuple[int, int, str]] | None = None,
                 pythons: set[str] | None = None, only_pids: set[int] | None = None) -> list[dict[str, Any]]:
    """Every python process of our interpreter stuck in its exit (parent alive or not)."""
    procs = winsys.process_list() if procs is None else procs
    pys = pythons or our_pythons(project_root)
    out = []
    for pid, ppid, exe in procs:
        if not exe.lower().startswith("python") or (only_pids is not None and pid not in only_pids):
            continue
        if not winsys.exiting(pid) or _norm(winsys.process_image(pid)) not in pys:
            continue
        det = winsys.process_memory_detail(pid) or {}
        out.append({"pid": pid, "ppid": ppid, "private_mb": round(det.get("private_mb") or 0),
                    "rss_mb": round(det.get("rss_mb") or 0), "create_time": winsys.process_create_time(pid)})
    return out


def reap(project_root: Path | None = None, dry_run: bool = False, **kw: Any) -> dict[str, Any]:
    """Kill leaked processes. Returns {"at", "leaked", "killed": [pid], "freed_mb", "items": [...]}."""
    found = find_leaked(project_root, **kw)
    zkw = {k: v for k, v in kw.items() if k in ("procs", "pythons", "only_pids")}
    zmap = {z["pid"]: z for z in find_zombies(project_root, **zkw)}
    for it in found:
        if it["zombie"]:
            zmap.setdefault(it["pid"], {k: it[k] for k in ("pid", "ppid", "private_mb", "rss_mb", "create_time")})
    items = [it for it in found if not it["zombie"]]
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    now_m = _mono()
    zombies, exiting = [], []
    for pid, z in zmap.items():
        ex = _EXITING_SEEN.get(pid)
        if ex is None or ex[0] != z.get("create_time"):          # first time seen in its exit (or pid reused)
            cmd = winsys.process_cmdline(pid)
            ex = _EXITING_SEEN[pid] = (z.get("create_time"), now_iso, now_m, (cmd or "")[:200] or None)
        info = {**{k: z.get(k) for k in ("pid", "ppid", "private_mb", "rss_mb")}, "first_seen": ex[1],
                "exiting_s": round(now_m - ex[2]), "cmd": ex[3]}
        if now_m - ex[2] < ZOMBIE_MIN_S:
            exiting.append(info)                                 # normal teardown so far: no alert
            continue
        seen = _ZOMBIES_SEEN.get(pid)
        if seen is None or seen[0] != z.get("create_time"):      # new zombie (or pid reused): report once
            _ZOMBIES_SEEN[pid] = (z.get("create_time"), ex[1])
            log.error("reaper: process %s is stuck in its exit for %.0fs (zombie, %s MB private, parent %s%s): it "
                      "cannot be killed - reported, not retried; only its exit or a reboot frees the memory", pid,
                      now_m - ex[2], z.get("private_mb"), z.get("ppid"), f", {ex[3][:120]}" if ex[3] else "")
        zombies.append({**info, "first_seen": _ZOMBIES_SEEN[pid][1]})
    for pid in [p for p in _EXITING_SEEN if p not in zmap]:
        if pid in _ZOMBIES_SEEN:
            log.info("reaper: zombie %s is gone (first seen %s)", pid, _ZOMBIES_SEEN[pid][1])
            _ZOMBIES_SEEN.pop(pid, None)
        _EXITING_SEEN.pop(pid, None)
    for pid in [p for p in _ZOMBIES_SEEN if p not in zmap]:
        log.info("reaper: zombie %s is gone (first seen %s)", pid, _ZOMBIES_SEEN[pid][1])
        _ZOMBIES_SEEN.pop(pid, None)
    killed: list[int] = []
    for it in items:
        if dry_run:
            continue
        log.warning("reaper: killing leaked process %s (parent %s dead, %s MB, age %ss): %s", it["pid"], it["ppid"],
                    it["rss_mb"], it["age_s"], it["cmd"][:120])
        killed += winsys.kill_tree(it["pid"])
    return {"at": now_iso, "leaked": len(items), "killed": killed,
            "freed_mb": sum(it["rss_mb"] for it in items) if not dry_run else 0, "items": items[:20],
            "zombies": zombies, "zombie_mb": sum(int(z.get("private_mb") or 0) for z in zombies),
            "exiting": exiting, "dry_run": dry_run}


# ------------------------------------------------------------------------------------------------ stray API servers


def is_serve_api(cmd: str | None) -> bool:
    c = (cmd or "").replace('"', " ")
    toks = c.split()
    return "app.cli" in toks and "serve" in toks and "api" in toks


def api_port_of(cmd: str, default: int = 8010) -> int:
    toks = (cmd or "").replace('"', " ").split()
    for i, t in enumerate(toks):
        if t == "--port" and i + 1 < len(toks):
            try:
                return int(toks[i + 1])
            except ValueError:
                return default
        if t.startswith("--port="):
            try:
                return int(t.split("=", 1)[1])
            except ValueError:
                return default
    return default


def find_stray_apis(owned: set[int], port: int, project_root: Path | None = None,
                    procs: list[tuple[int, int, str]] | None = None, pythons: set[str] | None = None,
                    only_pids: set[int] | None = None) -> list[dict[str, Any]]:
    """`app.cli serve api` processes of our interpreter not in `owned`. Each item says whether it targets our port
    and whether it is orphaned (the launcher's parent is dead). A venv launcher and its interpreter are reported as
    one item (the top-most process of the pair)."""
    if project_root is None:
        from app.core.config import get_settings  # noqa: PLC0415

        project_root = get_settings().project_root
    procs = winsys.process_list() if procs is None else procs
    alive = {p for p, _, _ in procs}
    parent = {p: pp for p, pp, _ in procs}
    pys = pythons or our_pythons(project_root)
    me = os.getpid()
    cands: dict[int, str] = {}
    for pid, _pp, exe in procs:
        if pid == me or pid in owned or not exe.lower().startswith("python"):
            continue
        if only_pids is not None and pid not in only_pids:
            continue
        cmd = winsys.process_cmdline(pid) or ""
        if not is_serve_api(cmd):
            continue
        if _norm(winsys.process_image(pid)) not in pys:
            continue
        cands[pid] = cmd
    out = []
    for pid, cmd in cands.items():
        if parent.get(pid) in cands:          # the interpreter under a venv launcher: reported with its launcher
            continue
        pp = parent.get(pid, 0)
        orphan = _parent_dead(pid, pp, alive)
        out.append({"pid": pid, "ppid": pp, "port": api_port_of(cmd), "ours_port": api_port_of(cmd) == port,
                    "orphaned": orphan, "rss_mb": round(winsys.tree_memory_mb(pid, procs)), "cmd": cmd[:200]})
    return out
