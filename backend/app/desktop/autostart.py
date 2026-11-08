"""Start the tray app at login: a per-user value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
(no admin rights). The installer offers the same as a task; the tray menu has a "Start at login" toggle.

    python -m app.desktop.autostart status | on | off
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BiG360"


def launch_command(root: Path | None = None) -> str:
    """Command line that starts the tray hidden without opening the browser: <root>/BiG360.exe --background, else
    wscript <root>/BiG360.vbs --background."""
    if root is None:
        from app.core.config import get_settings  # noqa: PLC0415

        root = get_settings().project_root
    exe = root / "BiG360.exe"
    if exe.is_file():
        return f'"{exe}" --background'
    vbs = root / "BiG360.vbs"
    return f'wscript.exe "{vbs}" --background'


def _winreg():  # pragma: no cover - trivial
    import winreg  # noqa: PLC0415

    return winreg


def get(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> str | None:
    if sys.platform != "win32":
        return None
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key_path, 0, wr.KEY_READ) as k:
            val, _ = wr.QueryValueEx(k, name)
            return str(val)
    except FileNotFoundError:
        return None


def is_enabled(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    return get(key_path, name) is not None


def enable(command: str | None = None, key_path: str = RUN_KEY, name: str = VALUE_NAME) -> str:
    wr = _winreg()
    cmd = command or launch_command()
    with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, key_path, 0, wr.KEY_SET_VALUE) as k:
        wr.SetValueEx(k, name, 0, wr.REG_SZ, cmd)
    return cmd


def disable(key_path: str = RUN_KEY, name: str = VALUE_NAME) -> bool:
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, key_path, 0, wr.KEY_SET_VALUE) as k:
            wr.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.desktop.autostart")
    ap.add_argument("action", choices=["status", "on", "off"])
    a = ap.parse_args(argv)
    if a.action == "on":
        print(f"start at login: on ({enable()})")
    elif a.action == "off":
        disable()
        print("start at login: off")
    else:
        v = get()
        print(f"start at login: {'on (' + v + ')' if v else 'off'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
