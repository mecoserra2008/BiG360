"""BiG Equity 360 tray app (O3): one icon in the notification area that owns the background processes.

    BiG360.exe                         (= pythonw -m app.desktop.tray, cwd backend/)
    python -m app.desktop.tray                    start the supervisor if needed, show the icon, open the dashboard
    python -m app.desktop.tray --background       same, without opening the browser (start at login)
    python -m app.desktop.tray --quit             stop the supervisor gracefully and close a running tray
    python -m app.desktop.tray --status           print the status lines and exit
    python -m app.desktop.tray --no-supervisor    icon only (processes managed elsewhere)

Pure ctypes (Shell_NotifyIconW + a hidden window + TrackPopupMenu): no pystray / pywin32 needed. Menu handlers live in
app.desktop.tray_actions (unit-tested without a GUI). Notifications are Shell_NotifyIcon balloons, which Windows 10/11
shows as toasts. Single instance: data/state/ops/tray.lock (a second start just opens the dashboard).
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes as W
from pathlib import Path
from typing import Any

from app.desktop.tray_actions import MenuItem, TrayController

# ------------------------------------------------------------------------------------------------ win32 constants
WM_NULL, WM_DESTROY, WM_CLOSE, WM_COMMAND = 0x0000, 0x0002, 0x0010, 0x0111
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
WM_APP = 0x8000
WM_TRAY, WM_REFRESH_TIP, WM_SHOW_NOTE, WM_FINISH, WM_RUN_MENU = WM_APP + 1, WM_APP + 2, WM_APP + 3, WM_APP + 4, WM_APP + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF = {"info": 0x01, "warn": 0x02, "error": 0x03}
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0x0000, 0x0001, 0x0008, 0x0010, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
IDI_APPLICATION = 32512
SM_CXSMICON, SM_CYSMICON = 49, 50

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", W.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128), ("dwState", W.DWORD),
                ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256), ("uTimeoutOrVersion", W.UINT),
                ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD), ("guidItem", GUID), ("hBalloonIcon", W.HICON)]


def _dlls() -> tuple[Any, Any, Any]:
    u = ctypes.WinDLL("user32", use_last_error=True)
    sh = ctypes.WinDLL("shell32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    u.DefWindowProcW.restype = LRESULT
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.RegisterClassW.restype = W.ATOM
    u.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
    u.CreateWindowExW.restype = W.HWND
    u.DestroyWindow.argtypes = [W.HWND]
    u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    u.PostMessageW.restype = W.BOOL
    u.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    u.GetMessageW.restype = W.BOOL
    u.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
    u.DispatchMessageW.restype = LRESULT
    u.CreatePopupMenu.restype = W.HMENU
    u.AppendMenuW.argtypes = [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR]
    u.AppendMenuW.restype = W.BOOL
    u.SetMenuDefaultItem.argtypes = [W.HMENU, W.UINT, W.UINT]
    u.TrackPopupMenu.argtypes = [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.LPVOID]
    u.TrackPopupMenu.restype = ctypes.c_int
    u.DestroyMenu.argtypes = [W.HMENU]
    u.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
    u.SetForegroundWindow.argtypes = [W.HWND]
    u.LoadImageW.argtypes = [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT]
    u.LoadImageW.restype = W.HANDLE
    u.LoadIconW.argtypes = [W.HINSTANCE, W.LPVOID]
    u.LoadIconW.restype = W.HICON
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.RegisterWindowMessageW.argtypes = [W.LPCWSTR]
    u.RegisterWindowMessageW.restype = W.UINT
    sh.Shell_NotifyIconW.argtypes = [W.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    sh.Shell_NotifyIconW.restype = W.BOOL
    k.GetModuleHandleW.argtypes = [W.LPCWSTR]
    k.GetModuleHandleW.restype = W.HMODULE
    return u, sh, k


def icon_path(root: Path) -> Path | None:
    for p in (root / "big360.ico", root / "packaging" / "assets" / "big360.ico"):
        if p.is_file():
            return p
    return None


class TrayApp:
    CLASS = "BiG360TrayWindow"

    def __init__(self, ctl: TrayController, open_when_ready: bool = False, manage_supervisor: bool = True):
        self.ctl = ctl
        self.ctl.notify = self.queue_note
        self.open_when_ready = open_when_ready
        self.manage = manage_supervisor
        self.u, self.sh, self.k = _dlls()
        self.notes: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.cmd_ids: dict[int, str] = {}
        self.hwnd: Any = None
        self.hicon: Any = None
        self._wndproc = WNDPROC(self._proc)  # keep a reference (GC)
        self._stop = threading.Event()
        self.taskbar_created = 0
        self.stopping = False

    # ------------------------------------------------------------------ window / icon
    def create(self) -> None:
        hinst = self.k.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = self.CLASS
        self.u.RegisterClassW(ctypes.byref(wc))  # 0 when already registered in this process: fine
        self.hwnd = self.u.CreateWindowExW(0, self.CLASS, "BiG Equity 360", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        self.taskbar_created = self.u.RegisterWindowMessageW("TaskbarCreated")
        ico = icon_path(self.ctl.s.project_root)
        if ico is not None:
            cx, cy = self.u.GetSystemMetrics(SM_CXSMICON), self.u.GetSystemMetrics(SM_CYSMICON)
            self.hicon = self.u.LoadImageW(None, str(ico), IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if not self.hicon:
            self.hicon = self.u.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self._icon(NIM_ADD)

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd = self.hwnd
        n.uID = 1
        n.uFlags = flags
        n.uCallbackMessage = WM_TRAY
        n.hIcon = self.hicon
        return n

    def _icon(self, op: int) -> bool:
        n = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        n.szTip = self.ctl.tooltip()
        return bool(self.sh.Shell_NotifyIconW(op, ctypes.byref(n)))

    def balloon(self, title: str, msg: str, severity: str = "info") -> None:
        n = self._nid(NIF_INFO)
        n.szInfoTitle = (title or "BiG360")[:63]
        n.szInfo = (msg or " ")[:255]
        n.dwInfoFlags = NIIF.get(severity, NIIF["info"])
        self.sh.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(n))

    def queue_note(self, title: str, msg: str, severity: str = "info") -> None:
        self.notes.put((title, msg, severity))
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_SHOW_NOTE, 0, 0)

    # ------------------------------------------------------------------ menu
    def build_menu(self, items: list[MenuItem]) -> Any:
        self.cmd_ids = {}
        nxt = [1000]

        def add(h: Any, its: list[MenuItem]) -> None:
            for it in its:
                if it.separator:
                    self.u.AppendMenuW(h, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING | (0 if it.enabled else MF_GRAYED) | (MF_CHECKED if it.checked else 0)
                if it.children:
                    sub = self.u.CreatePopupMenu()
                    add(sub, it.children)
                    self.u.AppendMenuW(h, flags | MF_POPUP, sub, it.label)
                    continue
                cid = nxt[0]
                nxt[0] += 1
                if it.id:
                    self.cmd_ids[cid] = it.id
                self.u.AppendMenuW(h, flags, cid, it.label)
                if it.default:
                    self.u.SetMenuDefaultItem(h, cid, 0)

        h = self.u.CreatePopupMenu()
        add(h, items)
        return h

    def show_menu(self) -> None:
        h = self.build_menu(self.ctl.menu())
        pt = W.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        self.u.SetForegroundWindow(self.hwnd)
        cid = self.u.TrackPopupMenu(h, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        self.u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        self.u.DestroyMenu(h)
        cmd = self.cmd_ids.get(cid)
        if cmd:
            self.run_command(cmd)

    def run_command(self, cmd: str) -> None:
        if cmd == "quit":
            self.begin_quit()
            return

        def work() -> None:
            try:
                msg = self.ctl.dispatch(cmd)
            except Exception as e:  # never kill the tray on a handler error
                msg = f"{cmd} failed: {type(e).__name__}: {e}"
            if msg:
                self.queue_note("BiG360", msg, "info")
            self.u.PostMessageW(self.hwnd, WM_REFRESH_TIP, 0, 0)

        threading.Thread(target=work, name=f"tray-{cmd}", daemon=True).start()

    def begin_quit(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._stop.set()
        msg = self.ctl.quit()
        self.balloon("BiG360", msg, "info")

        def wait() -> None:
            self.ctl.wait_stopped()
            self.u.PostMessageW(self.hwnd, WM_FINISH, 0, 0)

        threading.Thread(target=wait, name="tray-quit", daemon=True).start()

    # ------------------------------------------------------------------ window procedure
    def _proc(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self.show_menu()
                elif ev == WM_LBUTTONDBLCLK:
                    self.run_command("open")
                return 0
            if msg == WM_REFRESH_TIP:
                self._icon(NIM_MODIFY)
                return 0
            if msg == WM_SHOW_NOTE:
                while not self.notes.empty():
                    self.balloon(*self.notes.get_nowait())
                return 0
            if msg == WM_RUN_MENU:
                self.show_menu()
                return 0
            if msg == WM_FINISH:
                self.u.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._icon(NIM_DELETE)
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
            if self.taskbar_created and msg == self.taskbar_created:  # explorer restarted: re-add the icon
                self._icon(NIM_ADD)
                return 0
        except Exception:  # pragma: no cover - a Python exception must not cross the C callback
            import traceback

            traceback.print_exc()
        return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------ background loop
    def background(self) -> None:
        poll_s = max(10.0, float(self.ctl.cfg.get("status_poll_s", 60)))
        quit_flag = self.ctl.ops_dir / "tray.QUIT"
        last = 0.0
        opened = not self.open_when_ready
        first_ready_note = True
        while not self._stop.is_set():
            try:
                if quit_flag.exists():
                    quit_flag.unlink(missing_ok=True)
                    self._quit_from_flag()
                    return
                if self.manage:
                    m = self.ctl.ensure_supervisor()
                    if m:
                        self.queue_note("BiG360", m, "warn")
                fast = self.ctl.status is None  # starting: poll every few seconds until the API answers
                if time.monotonic() - last >= (3.0 if fast else poll_s):
                    last = time.monotonic()
                    self.ctl.poll()
                    if self.ctl.status is not None and first_ready_note:
                        first_ready_note = False
                        if not opened:
                            opened = True
                            self.ctl.open_dashboard()
                    self.u.PostMessageW(self.hwnd, WM_REFRESH_TIP, 0, 0)
            except Exception:
                import traceback

                traceback.print_exc()
            self._stop.wait(2.0)

    def _quit_from_flag(self) -> None:
        self.stopping = True
        self.ctl.quit()
        self.ctl.wait_stopped()
        self.u.PostMessageW(self.hwnd, WM_FINISH, 0, 0)

    def run(self) -> int:
        self.create()
        if self.manage:
            self.ctl.ensure_supervisor()
            if self.ctl.supervisor_pid:
                self.ctl.quiet_until = time.monotonic() + 180  # no "worker not running" toasts while starting
            if self.ctl.supervisor_pid and self.ctl.cfg.get("notifications", True):
                self.balloon("BiG Equity 360", f"Starting... the dashboard opens at {self.ctl.base}/", "info")
        threading.Thread(target=self.background, name="tray-poll", daemon=True).start()
        msg = W.MSG()
        while self.u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.u.TranslateMessage(ctypes.byref(msg))
            self.u.DispatchMessageW(ctypes.byref(msg))
        self._stop.set()
        return 0

    def selftest(self) -> int:
        """Create the window + icon, build the menu (not shown), remove everything. Prints the menu model."""
        self.create()
        h = self.build_menu(self.ctl.menu())
        self.u.DestroyMenu(h)
        ok = self._icon(NIM_MODIFY)
        self._icon(NIM_DELETE)
        self.u.DestroyWindow(self.hwnd)
        print(f"selftest: window ok, icon {'ok' if ok else 'FAILED'}, {len(self.cmd_ids)} menu commands: "
              f"{sorted(set(self.cmd_ids.values()))}")
        return 0 if ok else 1


def _print_menu(items: list[MenuItem], indent: int = 0) -> None:
    for it in items:
        if it.separator:
            print(" " * indent + "-----")
            continue
        mark = "[x] " if it.checked else ""
        print(" " * indent + f"{mark}{it.label}" + ("" if it.enabled else "  (disabled)") + (f"  <{it.id}>" if it.id else ""))
        if it.children:
            _print_menu(it.children, indent + 4)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.desktop.tray", description="BiG Equity 360 tray app")
    ap.add_argument("--background", action="store_true", help="do not open the dashboard (start at login)")
    ap.add_argument("--no-supervisor", action="store_true", help="tray only; do not start / restart the supervisor")
    ap.add_argument("--quit", action="store_true", help="stop the supervisor and close a running tray")
    ap.add_argument("--status", action="store_true", help="print the status lines and the menu, then exit")
    ap.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    from filelock import FileLock, Timeout

    from app.core.config import get_settings
    from app.core.logsetup import setup_logging

    s = get_settings()
    if not (a.status or a.quit or a.selftest):
        from app.desktop.tray_actions import ensure_models_link

        ensure_models_link(s)  # before ensure_dirs() (which would create an empty <root>/models_store)
    s.ensure_dirs()
    ctl = TrayController(s)
    if a.status:
        ctl.poll()
        print("\n".join(ctl.status_lines()))
        print()
        _print_menu(ctl.menu())
        return 0
    if a.quit:
        from app.desktop.tray_actions import _lock_held

        if _lock_held(ctl.ops_dir / "tray.lock"):  # a tray is running: let it close its icon too
            (ctl.ops_dir / "tray.QUIT").write_text("quit", encoding="utf-8")
        print(ctl.quit())
        ok = ctl.wait_stopped()
        print("stopped" if ok else "still stopping (see data/logs/app/supervisor.json)")
        return 0 if ok else 1
    if sys.platform != "win32":  # pragma: no cover
        print("the tray app needs Windows", file=sys.stderr)
        return 2
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: crisp menu
    except Exception:
        pass
    if a.selftest:
        ctl.cfg = {**ctl.cfg, "notifications": False}
        return TrayApp(ctl, manage_supervisor=False).selftest()
    lock = FileLock(str(ctl.ops_dir / "tray.lock"), timeout=0)
    try:
        lock.acquire(timeout=0)
    except Timeout:  # already running: behave like a shortcut to the dashboard
        if not a.background:
            ctl.open_dashboard()
        return 0
    (ctl.ops_dir / "tray.QUIT").unlink(missing_ok=True)  # stale request from a previous session
    if sys.stderr is None or sys.stdout is None:  # pythonw: keep tracebacks in a file instead of nowhere
        from app.ops.paths import app_logs_dir

        fh = open(app_logs_dir(s) / "tray.out.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or fh
        sys.stderr = sys.stderr or fh
    setup_logging(log_file="tray.log", service="tray")
    try:
        return TrayApp(ctl, open_when_ready=not a.background, manage_supervisor=not a.no_supervisor).run()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
