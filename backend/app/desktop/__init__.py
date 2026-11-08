"""BiG Equity 360 as a Windows application (docs/OPERATIONS_ROADMAP.md O3).

    app.desktop.appconfig     config/app.yaml + VERSION
    app.desktop.credentials   API keys in Windows Credential Manager (ctypes CredRead/CredWrite); .env fallback
    app.desktop.autostart     per-user start at login (HKCU ... \\Run)
    app.desktop.updates       "update available" check against a folder / share
    app.desktop.tray_actions  tray menu handlers (GUI-free, unit-tested)
    app.desktop.tray          the tray icon (pure ctypes Shell_NotifyIcon; no extra dependencies)

Nothing here is imported by pipeline jobs.
"""
