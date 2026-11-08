"""Windows system helpers via ctypes (no psutil dependency). Every function degrades to None / no-op elsewhere.

    memory_status()            {"total_gb", "avail_gb", "load_pct"}
    power_status()             {"on_ac", "battery_pct", "battery_present"}
    process_list()             [(pid, ppid, exe)]
    process_tree(pid)          [pid, child, grandchild, ...]
    process_memory_mb(pid)     working set MB (and private bytes via process_memory_detail)
    tree_memory_mb(pid)        working set of the whole tree
    pid_alive(pid)             liveness without signalling (os.kill(pid, 0) would TerminateProcess on Windows)
    kill_tree(pid)             TerminateProcess children first, then the root
    set_priority(pid, cls)     "idle" | "below_normal" | "normal"
    KeepAwake                  ref-counted SetThreadExecutionState(ES_SYSTEM_REQUIRED) held by a dedicated thread
    WakeTimer                  waitable timer with resume=True (wakes the PC from sleep when wake timers are allowed)
    network_cost()             {"metered": bool | None, "cost_type": str} (WinRT via powershell, cached 10 min)
    disk_free_gb(path)
    JobObject                  Windows Job Object (KILL_ON_JOB_CLOSE): kills a whole process tree, including
                               descendants whose parent already died (incident 2026-10-24)
    process_cmdline(pid)       command line (NtQueryInformationProcess class 60); process_image(pid); process_cwd(pid)
    process_create_time(pid)   creation FILETIME (pid-reuse guard for parent liveness)
    listening_pids(port)       pids with a TCP listener on `port` (GetExtendedTcpTable, IPv4 + IPv6)
    pressure_sample()          RAM / commit / pagefile estimate for the machine-level guard
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040
NORMAL_PRIORITY_CLASS = 0x00000020
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
PRIORITY = {"idle": IDLE_PRIORITY_CLASS, "below_normal": BELOW_NORMAL_PRIORITY_CLASS, "normal": NORMAL_PRIORITY_CLASS}

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_TH32CS_SNAPPROCESS = 0x00000002

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def _k32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


# ------------------------------------------------------------------------------------------------ memory / power


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD), ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64), ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64), ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64), ("ullAvailExtendedVirtual", ctypes.c_uint64)]


def memory_status() -> dict[str, float] | None:
    if not IS_WINDOWS:
        try:
            pages, avail, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_AVPHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            return {"total_gb": pages * size / 1024**3, "avail_gb": avail * size / 1024**3,
                    "load_pct": 100.0 * (1 - avail / pages)}
        except (ValueError, OSError, AttributeError):
            return None
    st = _MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return {"total_gb": st.ullTotalPhys / 1024**3, "avail_gb": st.ullAvailPhys / 1024**3,
            "load_pct": float(st.dwMemoryLoad), "commit_avail_gb": st.ullAvailPageFile / 1024**3}


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


def power_status() -> dict[str, Any]:
    """on_ac None = unknown (non-Windows / API failure): callers treat unknown as AC (desktop)."""
    if not IS_WINDOWS:
        return {"on_ac": None, "battery_pct": None, "battery_present": None}
    st = _SYSTEM_POWER_STATUS()
    if not _k32().GetSystemPowerStatus(ctypes.byref(st)):
        return {"on_ac": None, "battery_pct": None, "battery_present": None}
    on_ac = {0: False, 1: True}.get(st.ACLineStatus)
    no_battery = bool(st.BatteryFlag & 128) or st.BatteryFlag == 255
    return {"on_ac": on_ac, "battery_pct": None if st.BatteryLifePercent == 255 else int(st.BatteryLifePercent),
            "battery_present": not no_battery}


def disk_free_gb(path: str | Path) -> float | None:
    try:
        return shutil.disk_usage(str(path)).free / 1024**3
    except OSError:
        return None


# ------------------------------------------------------------------------------------------------ processes


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_list() -> list[tuple[int, int, str]]:
    """[(pid, parent_pid, exe_name)] of every process (Windows); [] elsewhere."""
    if not IS_WINDOWS:
        return []
    k = _k32()
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    out: list[tuple[int, int, str]] = []
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def process_tree(pid: int, procs: list[tuple[int, int, str]] | None = None) -> list[int]:
    """pid followed by all live descendants (breadth first). Parent-pid reuse is guarded by requiring the child to
    be listed after the parent was seen alive (good enough for our own short-lived process trees)."""
    procs = process_list() if procs is None else procs
    children: dict[int, list[int]] = {}
    for p, pp, _ in procs:
        if p != pp:
            children.setdefault(pp, []).append(p)
    out, todo, seen = [], [pid], set()
    while todo:
        p = todo.pop(0)
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        todo.extend(children.get(p, []))
    return out


def _open(pid: int, access: int) -> Any:
    k = _k32()
    k.OpenProcess.restype = wintypes.HANDLE
    return k.OpenProcess(access, False, int(pid))


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # incident 2026-10-24: a process stuck INSIDE its exit (CTRL+BREAK -> ExitProcess -> DLL-detach deadlock) already
    # has an exit code but keeps its memory / handles; only the process object's signalled state says it is gone.
    h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE)
    if h:
        try:
            k = _k32()
            k.WaitForSingleObject.restype = wintypes.DWORD
            return k.WaitForSingleObject(wintypes.HANDLE(h), 0) == _WAIT_TIMEOUT
        finally:
            _k32().CloseHandle(wintypes.HANDLE(h))
    h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not _k32().GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        _k32().CloseHandle(h)


def pid_running(pid: int | None) -> bool:
    """Alive AND not stuck in its exit: the process can still do work (or be terminated)."""
    return pid_alive(pid) and not exiting(int(pid or 0))


def exiting(pid: int) -> bool:
    """True for a process that has an exit code but has not finished terminating ("zombie", stuck in its exit:
    incident 2026-10-24 - 3 such processes held 2.7 GB for 4-6 h; TerminateProcess cannot cut them short, they
    already are terminating). Callers must report them, never wait on them or retry killing them."""
    if not IS_WINDOWS:
        return False
    h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        ok = _k32().GetExitCodeProcess(h, ctypes.byref(code))
    finally:
        _k32().CloseHandle(h)
    return bool(ok) and code.value != _STILL_ACTIVE and pid_alive(pid)


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def process_memory_detail(pid: int) -> dict[str, float] | None:
    """{"rss_mb": working set, "private_mb": private commit} or None."""
    if not IS_WINDOWS:
        try:
            with open(f"/proc/{pid}/statm") as f:
                rss_pages = int(f.read().split()[1])
            mb = rss_pages * os.sysconf("SC_PAGE_SIZE") / 1024**2
            return {"rss_mb": mb, "private_mb": mb}
        except (OSError, ValueError, IndexError):
            return None
    h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ)
    if not h:
        h = _open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return None
    try:
        c = _PROCESS_MEMORY_COUNTERS_EX()
        c.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS_EX)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return None
        return {"rss_mb": c.WorkingSetSize / 1024**2, "private_mb": c.PrivateUsage / 1024**2}
    finally:
        _k32().CloseHandle(h)


def process_memory_mb(pid: int) -> float | None:
    d = process_memory_detail(pid)
    return None if d is None else d["rss_mb"]


def tree_memory_mb(pid: int, procs: list[tuple[int, int, str]] | None = None) -> float:
    total = 0.0
    for p in process_tree(pid, procs):
        m = process_memory_mb(p)
        total += m or 0.0
    return total


def release_memory(trim_working_set: bool = False) -> dict[str, float | None]:
    """Give freed memory of THIS process back to the OS (ram_fix 2026-10-31; run after thread jobs).

    gc.collect() frees Python objects (and the Polars / numpy / DuckDB buffers they own); on Windows HeapCompact on
    every process heap plus the CRT's _heapmin() decommit free heap blocks (malloc_trim has no Windows equivalent;
    allocators with their own arenas keep what they cache). trim_working_set=True also calls EmptyWorkingSet:
    resident pages leave the working set (free RAM rises), but dirty private pages are written to the pagefile and
    fault back in on the next touch, so it is only used when the process goes idle (end of a DAG run).
    Returns {"before_mb", "after_mb", "private_before_mb", "private_after_mb"}."""
    import gc

    pid = os.getpid()
    d0 = process_memory_detail(pid) or {}
    gc.collect()
    if IS_WINDOWS:
        try:
            k = _k32()
            k.GetProcessHeaps.restype = wintypes.DWORD
            k.GetProcessHeaps.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
            k.HeapCompact.restype = ctypes.c_size_t
            k.HeapCompact.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            n = k.GetProcessHeaps(0, None)
            if n:
                arr = (wintypes.HANDLE * n)()
                n = k.GetProcessHeaps(n, arr)
                for i in range(n):
                    k.HeapCompact(arr[i], 0)
        except (OSError, AttributeError):  # pragma: no cover
            pass
        try:
            ctypes.CDLL("ucrtbase")._heapmin()
        except (OSError, AttributeError):  # pragma: no cover
            pass
        if trim_working_set:
            try:
                k = _k32()
                k.GetCurrentProcess.restype = wintypes.HANDLE
                k.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]
                k.K32EmptyWorkingSet(k.GetCurrentProcess())
            except (OSError, AttributeError):  # pragma: no cover
                pass
    else:  # pragma: no cover - glibc only
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass
    d1 = process_memory_detail(pid) or {}
    return {"before_mb": d0.get("rss_mb"), "after_mb": d1.get("rss_mb"), "private_before_mb": d0.get("private_mb"),
            "private_after_mb": d1.get("private_mb")}


def kill_tree(pid: int, procs: list[tuple[int, int, str]] | None = None) -> list[int]:
    """Terminate pid and all descendants (deepest first). Returns the pids that were signalled."""
    tree = process_tree(pid, procs)
    killed = []
    for p in reversed(tree):
        if IS_WINDOWS:
            h = _open(p, _PROCESS_TERMINATE)
            if h:
                try:
                    if _k32().TerminateProcess(h, 1):
                        killed.append(p)
                finally:
                    _k32().CloseHandle(h)
        else:  # pragma: no cover
            try:
                os.kill(p, 9)
                killed.append(p)
            except OSError:
                pass
    return killed


def set_priority(pid: int, cls: str = "below_normal") -> bool:
    if not IS_WINDOWS:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, {"idle": 19, "below_normal": 10, "normal": 0}[cls])  # type: ignore[attr-defined]
            return True
        except (OSError, AttributeError, KeyError):
            return False
    h = _open(pid, _PROCESS_SET_INFORMATION | _PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        return bool(_k32().SetPriorityClass(h, PRIORITY[cls]))
    finally:
        _k32().CloseHandle(h)


def has_console() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # pragma: no cover
        return False


def priority_creationflags(cls: str = "below_normal", new_group: bool = True, no_window: bool | None = None) -> int:
    """subprocess.Popen(creationflags=...) for a child that starts at `cls` priority (inherited by its own children)
    in its own process group (CTRL+BREAK reaches only that tree). no_window=None: share our console when we have one
    (so CTRL+BREAK can be delivered), else CREATE_NO_WINDOW (no console windows popping up)."""
    if not IS_WINDOWS:
        return 0
    f = PRIORITY.get(cls, 0)
    if new_group:
        f |= CREATE_NEW_PROCESS_GROUP
    if no_window is None:
        no_window = not has_console()
    if no_window:
        f |= CREATE_NO_WINDOW
    return f


# ------------------------------------------------------------------------------------------------ keep awake


class KeepAwake:
    """Ref-counted 'system required' execution state. SetThreadExecutionState applies to the calling thread, so one
    dedicated daemon thread owns it: acquire(reason) / release(reason) from anywhere; the PC may sleep again once
    every reason is released. Display sleep is not prevented (ES_DISPLAY_REQUIRED not set)."""

    def __init__(self) -> None:
        self._reasons: dict[str, int] = {}
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._thread: threading.Thread | None = None
        self.active = False

    def _loop(self) -> None:
        while True:
            self._changed.wait(60)
            self._changed.clear()
            with self._lock:
                want = bool(self._reasons)
            if IS_WINDOWS:
                flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0)
                try:
                    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
                except Exception:  # pragma: no cover
                    pass
            self.active = want

    def _ensure(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="keep-awake", daemon=True)
            self._thread.start()

    def acquire(self, reason: str) -> None:
        with self._lock:
            self._reasons[reason] = self._reasons.get(reason, 0) + 1
        self._ensure()
        self._changed.set()

    def release(self, reason: str) -> None:
        with self._lock:
            n = self._reasons.get(reason, 0) - 1
            if n <= 0:
                self._reasons.pop(reason, None)
            else:
                self._reasons[reason] = n
        self._ensure()
        self._changed.set()

    def set(self, reason: str, on: bool) -> None:
        """Idempotent toggle (one count per reason)."""
        with self._lock:
            have = reason in self._reasons
        if on and not have:
            self.acquire(reason)
        elif not on and have:
            with self._lock:
                self._reasons.pop(reason, None)
            self._changed.set()

    def reasons(self) -> list[str]:
        with self._lock:
            return sorted(self._reasons)


KEEP_AWAKE = KeepAwake()


class WakeTimer:
    """Arm a waitable timer that resumes the PC from sleep at `when` (needs 'Allow wake timers' in the power plan).
    The handle is kept open by this object; re-arming replaces the due time; cancel() disarms it."""

    def __init__(self) -> None:
        self._h: Any = None
        self.due: datetime | None = None

    def arm(self, when: datetime) -> bool:
        if not IS_WINDOWS:
            return False
        k = _k32()
        if self._h is None:
            k.CreateWaitableTimerW.restype = wintypes.HANDLE
            self._h = k.CreateWaitableTimerW(None, True, None)
            if not self._h:
                return False
        # FILETIME: 100 ns intervals since 1601-01-01 UTC (positive = absolute)
        ft = int((when.astimezone(UTC).timestamp() + 11644473600) * 10_000_000)
        li = ctypes.c_longlong(ft)
        ok = bool(k.SetWaitableTimer(self._h, ctypes.byref(li), 0, None, None, True))
        self.due = when if ok else None
        return ok

    def cancel(self) -> None:
        if IS_WINDOWS and self._h:
            k = _k32()
            k.CancelWaitableTimer(self._h)
            k.CloseHandle(self._h)
        self._h, self.due = None, None


# ------------------------------------------------------------------------------------------------ job objects
# incident 2026-10-24: kill_tree() walks parent-pid links, so a multiprocessing spawn child whose parent had already
# died (taskrun crashed on CTRL+BREAK, pool owner exited) was never found and leaked ~0.3-1 GB per hour. A Job Object
# tracks membership independently of the parent chain: every process created inside the job stays in it.

_JobObjectBasicAccountingInformation = 1
_JobObjectBasicProcessIdList = 3
_JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_PROCESS_SET_QUOTA = 0x0100


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


_KJ: Any = None


def _kj() -> Any:
    """kernel32 with explicit prototypes for the job / process-information calls (64-bit safe handles)."""
    global _KJ  # noqa: PLW0603
    if _KJ is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        H, D, B, P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
        k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = H, [P, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype, k.SetInformationJobObject.argtypes = B, [H, ctypes.c_int, P, D]
        k.QueryInformationJobObject.restype = B
        k.QueryInformationJobObject.argtypes = [H, ctypes.c_int, P, D, ctypes.POINTER(D)]
        k.AssignProcessToJobObject.restype, k.AssignProcessToJobObject.argtypes = B, [H, H]
        k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = B, [H, ctypes.c_uint]
        k.OpenProcess.restype, k.OpenProcess.argtypes = H, [D, B, D]
        k.CloseHandle.restype, k.CloseHandle.argtypes = B, [H]
        k.TerminateProcess.restype, k.TerminateProcess.argtypes = B, [H, ctypes.c_uint]
        k.IsProcessInJob.restype, k.IsProcessInJob.argtypes = B, [H, H, ctypes.POINTER(B)]
        k.ReadProcessMemory.restype = B
        k.ReadProcessMemory.argtypes = [H, P, P, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k.QueryFullProcessImageNameW.restype = B
        k.QueryFullProcessImageNameW.argtypes = [H, D, wintypes.LPWSTR, ctypes.POINTER(D)]
        k.GetProcessTimes.restype = B
        k.GetProcessTimes.argtypes = [H] + [ctypes.POINTER(ctypes.c_uint64)] * 4
        _KJ = k
    return _KJ


class JobObject:
    """A Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.

    assign(pid) puts a process into the job; everything it starts afterwards (venv launcher -> interpreter ->
    multiprocessing spawn workers -> their pools) is in the job too, whatever happens to the intermediate parents.
    terminate() kills every member; close() releases the handle (with kill_on_close the remaining members die then,
    and also when the owning process dies - the kernel closes the handle). Jobs nest (Windows 8+), so a worker in
    the supervisor's job can give each job process its own job. Elsewhere / on failure `ok` is False and every
    method is a no-op (callers fall back to kill_tree)."""

    def __init__(self, kill_on_close: bool = True, name: str | None = None) -> None:
        self.handle: Any = None
        self.ok = False
        self.error: int | None = None
        self.name = name
        self.assigned: list[int] = []
        if not IS_WINDOWS:
            return
        k = _kj()
        h = k.CreateJobObjectW(None, None)
        if not h:
            self.error = ctypes.get_last_error()
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if kill_on_close:
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            self.error = ctypes.get_last_error()
            k.CloseHandle(h)
            return
        self.handle, self.ok = h, True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JobObject({self.name!r}, ok={self.ok}, assigned={self.assigned})"

    def assign(self, pid: int | None) -> bool:
        if not self.ok or not pid:
            return False
        k = _kj()
        h = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not h:
            self.error = ctypes.get_last_error()
            return False
        try:
            if k.AssignProcessToJobObject(self.handle, h):
                self.assigned.append(int(pid))
                return True
            self.error = ctypes.get_last_error()
            return False
        finally:
            k.CloseHandle(h)

    def assign_tree(self, pid: int | None, procs: list[tuple[int, int, str]] | None = None, rounds: int = 4) -> int:
        """Assign pid and the descendants it already started (a child created before the root was assigned is not
        in the job automatically). Re-scans until a scan finds nothing new (a venv launcher may create the
        interpreter between the snapshot and the assignment). Prefer start_in_job() (no race at all)."""
        if not self.ok or not pid:
            return 0
        n = 0
        for i in range(max(1, rounds)):
            new = 0
            # deepest first: a process already in another job (e.g. the venv launcher's) can only join ours while
            # ours is still empty (Windows nested-job rule), so the descendants go in before the root
            for p in reversed(process_tree(int(pid), procs if i == 0 else None)):
                if p in self.assigned or self.contains(p):
                    continue
                new += bool(self.assign(p))
            n += new
            if i and not new:
                break
            time.sleep(0.02)
        return n

    def contains(self, pid: int) -> bool:
        if not self.ok:
            return False
        k = _kj()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            res = wintypes.BOOL(False)
            return bool(k.IsProcessInJob(h, self.handle, ctypes.byref(res))) and bool(res.value)
        finally:
            k.CloseHandle(h)

    def pids(self) -> list[int]:
        """Live member pids."""
        if not self.ok:
            return []
        k = _kj()
        n = 64
        while True:
            class _L(ctypes.Structure):
                _fields_ = [("Assigned", wintypes.DWORD), ("InList", wintypes.DWORD), ("Ids", ctypes.c_size_t * n)]

            lst = _L()
            ok = k.QueryInformationJobObject(self.handle, _JobObjectBasicProcessIdList, ctypes.byref(lst),
                                             ctypes.sizeof(lst), None)
            if ok or ctypes.get_last_error() != 234 or n >= 65536:  # 234 = ERROR_MORE_DATA
                if not ok and lst.InList == 0:
                    return []
                return [int(lst.Ids[i]) for i in range(min(lst.InList, n))]
            n *= 4

    def live_pids(self) -> list[int]:
        """Members that still run (zombies stuck in their exit excluded)."""
        return [p for p in self.pids() if not exiting(p)]

    def zombies(self) -> list[int]:
        return [p for p in self.pids() if exiting(p)]

    def active(self) -> int:
        """Number of live member processes (0 = the whole tree is gone)."""
        if not self.ok:
            return 0
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _kj().QueryInformationJobObject(self.handle, _JobObjectBasicAccountingInformation, ctypes.byref(info),
                                               ctypes.sizeof(info), None):
            return len(self.pids())
        return int(info.ActiveProcesses)

    def memory_mb(self) -> float:
        """Working set of every member (orphaned descendants included)."""
        return sum(process_memory_mb(p) or 0.0 for p in self.pids())

    def memory_detail(self) -> dict[str, float]:
        """{"ws_mb", "private_mb"} summed over the live members (ram_fix 2026-10-31: per-job sampling)."""
        ws = pv = 0.0
        for p in self.pids():
            d = process_memory_detail(p)
            if d:
                ws += float(d.get("rss_mb") or 0.0)
                pv += float(d.get("private_mb") or 0.0)
        return {"ws_mb": ws, "private_mb": pv}

    def _extended(self) -> _JOBOBJECT_EXTENDED_LIMIT_INFORMATION | None:
        if not self.ok:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not _kj().QueryInformationJobObject(self.handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                               ctypes.sizeof(info), None):
            self.error = ctypes.get_last_error()
            return None
        return info

    def set_memory_limit(self, limit_bytes: int | None) -> bool:
        """JOB_OBJECT_LIMIT_JOB_MEMORY: cap the committed memory of all members together (ram_fix 2026-10-31). An
        allocation that would exceed it fails inside the job (MemoryError / allocator abort); the rest of the machine
        is unaffected. KILL_ON_JOB_CLOSE (and any other limit already set) is kept. None / 0 removes the cap."""
        info = self._extended()
        if info is None:
            return False
        flags = int(info.BasicLimitInformation.LimitFlags)
        if limit_bytes:
            flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = int(limit_bytes)
        else:
            flags &= ~JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = 0
        info.BasicLimitInformation.LimitFlags = flags
        if not _kj().SetInformationJobObject(self.handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                             ctypes.sizeof(info)):
            self.error = ctypes.get_last_error()
            return False
        return True

    def limit_info(self) -> dict[str, float | None]:
        """{"limit_mb": job memory cap or None, "peak_job_mb": peak committed memory of the job so far,
        "peak_process_mb": largest single-member peak}."""
        info = self._extended()
        if info is None:
            return {"limit_mb": None, "peak_job_mb": None, "peak_process_mb": None}
        lim = info.JobMemoryLimit if int(info.BasicLimitInformation.LimitFlags) & JOB_OBJECT_LIMIT_JOB_MEMORY else 0
        return {"limit_mb": (lim / 1024**2) if lim else None, "peak_job_mb": info.PeakJobMemoryUsed / 1024**2,
                "peak_process_mb": info.PeakProcessMemoryUsed / 1024**2}

    def terminate(self, exit_code: int = 1) -> int:
        """Kill every member. Returns how many were alive."""
        if not self.ok:
            return 0
        n = self.active()
        if n:
            _kj().TerminateJobObject(self.handle, exit_code)
        return n

    def wait_empty(self, timeout_s: float = 10.0, include_exiting: bool = False) -> bool:
        """Wait until no member runs any more (zombies stuck in their exit do not count: they never finish).
        include_exiting=True waits for members still tearing down too (a process in its exit for a moment after a
        kill / normal exit - conhost, a multi-GB interpreter freeing its memory) and returns active() == 0."""
        end = time.monotonic() + timeout_s
        while self.active() and time.monotonic() < end:
            if not include_exiting and not self.live_pids():
                return True
            time.sleep(0.05)
        return self.active() == 0 or (not include_exiting and not self.live_pids())

    def close(self) -> None:
        if self.ok and self.handle:
            _kj().CloseHandle(self.handle)
        self.handle, self.ok = None, False


CREATE_SUSPENDED = 0x00000004


def _resume_process(handle: Any) -> bool:
    try:
        nt = ctypes.WinDLL("ntdll")
        f = nt.NtResumeProcess
        f.restype, f.argtypes = ctypes.c_long, [wintypes.HANDLE]
        return f(int(handle)) == 0
    except Exception:  # pragma: no cover
        return False


def start_in_job(cmd: list[str], job: JobObject | None, **popen_kw: Any) -> subprocess.Popen:
    """subprocess.Popen whose process is in `job` before it runs a single instruction: created suspended, assigned,
    resumed (NtResumeProcess). So a venv launcher's interpreter and every later descendant are members - no
    start-up race. Falls back to a normal start + assign_tree when anything fails (never leaves a process
    suspended)."""
    if not IS_WINDOWS or job is None or not job.ok:
        p = subprocess.Popen(cmd, **popen_kw)
        if job is not None and job.ok:
            job.assign_tree(p.pid)
        return p
    flags = int(popen_kw.pop("creationflags", 0) or 0)
    p = subprocess.Popen(cmd, creationflags=flags | CREATE_SUSPENDED, **popen_kw)
    assigned = job.assign(p.pid)
    if _resume_process(getattr(p, "_handle", 0)):
        if not assigned:
            job.assign_tree(p.pid)
        return p
    try:  # could not resume: never leave it suspended - kill it and start normally
        p.kill()
        p.wait(10)
    except Exception:  # pragma: no cover
        pass
    p = subprocess.Popen(cmd, creationflags=flags, **popen_kw)
    job.assign_tree(p.pid)
    return p


# ------------------------------------------------------------------------------------------------ process details


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p)]


_NT: Any = None


def _ntq() -> Any:
    global _NT  # noqa: PLW0603
    if _NT is None:
        nt = ctypes.WinDLL("ntdll")
        f = nt.NtQueryInformationProcess
        f.restype = ctypes.c_long
        f.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        _NT = f
    return _NT


def process_cmdline(pid: int) -> str | None:
    """Full command line of pid (Windows 8.1+), None when inaccessible."""
    if not IS_WINDOWS:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            return None
    k = _kj()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        q = _ntq()
        size = ctypes.c_ulong(0)
        q(h, 60, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if q(h, 60, buf, size.value, ctypes.byref(size)) != 0:
            return None
        us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer and us.Length else ""
    finally:
        k.CloseHandle(h)


def process_image(pid: int) -> str | None:
    """Full path of the executable of pid."""
    if not IS_WINDOWS:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    k = _kj()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k.CloseHandle(h)


def process_cwd(pid: int) -> str | None:
    """Current directory of a 64-bit process (PEB -> RTL_USER_PROCESS_PARAMETERS.CurrentDirectory), None when
    inaccessible. Used to verify that an orphan belongs to this installation before the reaper kills it."""
    if not IS_WINDOWS:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    if ctypes.sizeof(ctypes.c_void_p) != 8:  # pragma: no cover - 32-bit interpreter
        return None
    k = _kj()
    h = k.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, int(pid))
    if not h:
        return None
    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        if _ntq()(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None) != 0 or not pbi.PebBaseAddress:
            return None

        def read(addr: int, size: int) -> bytes | None:
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            if not k.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)) or got.value != size:
                return None
            return buf.raw

        raw = read(pbi.PebBaseAddress + 0x20, 8)               # PEB.ProcessParameters
        if raw is None:
            return None
        params = int.from_bytes(raw, "little")
        raw = read(params + 0x38, 16)                          # CurrentDirectory.DosPath (UNICODE_STRING)
        if raw is None:
            return None
        length = int.from_bytes(raw[0:2], "little")
        ptr = int.from_bytes(raw[8:16], "little")
        if not ptr or not length or length > 4096:
            return None
        txt = read(ptr, length)
        return txt.decode("utf-16-le", "replace") if txt is not None else None
    finally:
        k.CloseHandle(h)


def process_create_time(pid: int) -> int | None:
    """Creation time as a FILETIME integer (100 ns since 1601), None when inaccessible."""
    if not IS_WINDOWS:
        try:
            return int(os.stat(f"/proc/{pid}").st_mtime * 1e7)
        except OSError:
            return None
    k = _kj()
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        c, e, kt, ut = (ctypes.c_uint64() for _ in range(4))
        if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return int(c.value)
    finally:
        k.CloseHandle(h)


def kill_pid(pid: int) -> bool:
    if not IS_WINDOWS:  # pragma: no cover
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False
    k = _kj()
    h = k.OpenProcess(_PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(k.TerminateProcess(h, 1))
    finally:
        k.CloseHandle(h)


# ------------------------------------------------------------------------------------------------ TCP listeners


def listening_pids(port: int) -> set[int]:
    """pids owning a TCP listener on `port` (any address, IPv4 + IPv6). Empty set elsewhere / on failure."""
    if not IS_WINDOWS:
        return set()
    try:
        ip = ctypes.WinDLL("iphlpapi")
        f = ip.GetExtendedTcpTable
        f.restype = wintypes.DWORD
        f.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int,
                      wintypes.ULONG]
    except (OSError, AttributeError):  # pragma: no cover
        return set()
    out: set[int] = set()
    for af, row_size, port_off, pid_off in ((2, 24, 8, 20), (23, 56, 20, 52)):   # AF_INET / AF_INET6 rows
        size = wintypes.DWORD(0)
        f(None, ctypes.byref(size), False, af, 3, 0)          # 3 = TCP_TABLE_OWNER_PID_LISTENER
        if not size.value:
            continue
        buf = ctypes.create_string_buffer(size.value + 1024)
        size = wintypes.DWORD(len(buf))
        if f(buf, ctypes.byref(size), False, af, 3, 0) != 0:
            continue
        raw = buf.raw
        n = int.from_bytes(raw[0:4], "little")
        for i in range(n):
            base = 4 + i * row_size
            if base + row_size > len(raw):
                break
            p = int.from_bytes(raw[base + port_off:base + port_off + 2], "big")   # network byte order
            if p == int(port):
                out.add(int.from_bytes(raw[base + pid_off:base + pid_off + 4], "little"))
    return out


# ------------------------------------------------------------------------------------------------ machine pressure


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t), ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]


def pressure_sample() -> dict[str, float] | None:
    """{"avail_gb", "total_gb", "load_pct", "commit_gb", "commit_limit_gb", "pagefile_used_gb"}: pagefile_used is the
    commit charge not backed by RAM in use (commit - (total - available)), a cheap proxy for pagefile occupancy."""
    m = memory_status()
    if m is None:
        return None
    out = {"avail_gb": m["avail_gb"], "total_gb": m["total_gb"], "load_pct": m["load_pct"]}
    if IS_WINDOWS:
        pi = _PERFORMANCE_INFORMATION()
        pi.cb = ctypes.sizeof(pi)
        try:
            ok = ctypes.WinDLL("psapi").GetPerformanceInfo(ctypes.byref(pi), pi.cb)
        except (OSError, AttributeError):  # pragma: no cover
            ok = False
        if ok:
            page = pi.PageSize / 1024**3
            commit, limit = pi.CommitTotal * page, pi.CommitLimit * page
            used_phys = (pi.PhysicalTotal - pi.PhysicalAvailable) * page
            out.update(commit_gb=commit, commit_limit_gb=limit, pagefile_used_gb=max(0.0, commit - used_phys))
    return out


# ------------------------------------------------------------------------------------------------ network cost

_NET_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_NET_PS = (
    "[void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime];"
    "$p=[Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile();"
    "if($p){$c=$p.GetConnectionCost(); \"$($c.NetworkCostType)|$($c.Roaming)|$($c.OverDataLimit)\"} else {'none'}"
)


def network_cost(max_age_s: float = 600.0) -> dict[str, Any]:
    """{"metered": True/False/None, "cost_type": Unrestricted|Fixed|Variable|Unknown|none|error}. Metered = Fixed or
    Variable cost, roaming, or over the data limit (phone hotspots are usually flagged metered)."""
    now = time.monotonic()
    if _NET_CACHE["value"] is not None and now - _NET_CACHE["at"] < max_age_s:
        return _NET_CACHE["value"]
    val: dict[str, Any] = {"metered": None, "cost_type": "unknown"}
    if IS_WINDOWS:
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _NET_PS],
                               capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or "").strip().splitlines()[-1:] or ["error"]
            parts = out[0].split("|")
            cost = parts[0] or "error"
            roaming = len(parts) > 1 and parts[1].lower() == "true"
            over = len(parts) > 2 and parts[2].lower() == "true"
            metered = None if cost in ("error", "Unknown") else (cost in ("Fixed", "Variable") or roaming or over)
            if cost == "none":
                metered = None
            val = {"metered": metered, "cost_type": cost}
        except (OSError, subprocess.SubprocessError, IndexError):
            val = {"metered": None, "cost_type": "error"}
    _NET_CACHE.update(at=now, value=val)
    return val
