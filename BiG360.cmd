@echo off
rem BiG Equity 360 - start the tray app (no console stays open). Same as BiG360.exe / BiG360.vbs.
rem   BiG360.cmd               start (opens the dashboard when ready)
rem   BiG360.cmd --background  start without opening the browser
rem   BiG360.cmd --quit        stop everything gracefully
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\pythonw.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
  echo BiG Equity 360: Python runtime not found in %ROOT%
  exit /b 1
)
set "BIG360_APP=1"
pushd "%ROOT%backend"
if /I "%~1"=="--quit" (
  "%PY:pythonw.exe=python.exe%" -m app.desktop.tray --quit
) else (
  start "" "%PY%" -m app.desktop.tray %*
)
popd
endlocal
