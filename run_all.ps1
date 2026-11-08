<#
.SYNOPSIS  BiG Equity 360 - start API, worker and frontend dev server, each in its own PowerShell window.
.EXAMPLE   .\run_all.ps1                 # API + worker + frontend (mock data in the UI)
           .\run_all.ps1 -Live           # frontend talks to the local API
           .\run_all.ps1 -NoWorker -Doctor
#>
param(
    [switch]$Live,
    [switch]$NoWorker,
    [switch]$NoFrontend,
    [switch]$Doctor,      # run "python -m app.cli doctor --offline" first
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found at $Py" }
$Shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }

if ($Doctor) {
    Push-Location (Join-Path $Root "backend")
    & $Py -m app.cli doctor --offline
    Pop-Location
}

function Start-Window([string]$Script, [string[]]$Extra = @()) {
    $argList = @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root $Script)) + $Extra
    Start-Process -FilePath $Shell -ArgumentList $argList -WorkingDirectory $Root | Out-Null
}

Start-Window "run_api.ps1" @("-Port", "$Port")
if (-not $NoWorker) { Start-Window "run_worker.ps1" }
if (-not $NoFrontend) {
    $fe = @()
    if ($Live) { $fe = @("-Live", "-ApiUrl", "http://localhost:$Port") }
    Start-Window "run_frontend.ps1" $fe
}
Write-Host ""
Write-Host "BiG Equity 360 starting in separate windows:" -ForegroundColor DarkYellow
Write-Host "  API       http://127.0.0.1:$Port  (docs /docs, health /health)"
if (-not $NoWorker)   { Write-Host "  worker    data\logs\worker.log  (status: backend> python -m app.cli jobs status)" }
if (-not $NoFrontend) { Write-Host "  frontend  http://localhost:5173" }
Write-Host "Close a window (or Ctrl+C in it) to stop that process."
