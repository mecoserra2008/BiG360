<#
.SYNOPSIS  BiG Equity 360 - start the supervisor (API + worker with the scheduler ON + gap filler) in the background.
.DESCRIPTION
  python -m app.ops.supervisor runs hidden; it starts / monitors / restarts the API (default :8010), the worker
  (active schedule profile, default weekly = Saturday 08:00 Lisbon) and the low-priority gap filler.
  Status: data\logs\app\supervisor.json (python -m app.ops.supervisor --status); pids: data\logs\app\*.pid.
  Stop:   .\scripts\run_supervisor.ps1 -Stop   (graceful: running jobs finish / cancel, up to ~5 min)
.EXAMPLE  .\scripts\run_supervisor.ps1 -App       .\scripts\run_supervisor.ps1 -ApiPort 8010 -NoFiller            .\scripts\run_supervisor.ps1 -Stop
#>
param(
    [int]$ApiPort = 8010,
    [switch]$NoFiller,
    [switch]$App,         # O3 app mode: the API also serves the built UI at / (frontend\dist_app)
    [switch]$Stop
)
$ErrorActionPreference = "Stop"
$R = Split-Path $PSScriptRoot -Parent
$py = Join-Path $R ".venv\Scripts\python.exe"
$logs = Join-Path $R "data\logs\app"
New-Item -ItemType Directory -Force $logs | Out-Null
if ($Stop) {
    New-Item -ItemType Directory -Force (Join-Path $R "data\state\ops") | Out-Null
    Set-Content -Path (Join-Path $R "data\state\ops\supervisor.STOP") -Value "stop $(Get-Date -Format s)"
    Write-Host "Stop requested (data\state\ops\supervisor.STOP); children stop gracefully."
    return
}
$a = @("-m", "app.ops.supervisor", "--api-port", "$ApiPort")
if ($NoFiller) { $a += "--no-filler" }
if ($App) { $a += "--app" }
$p = Start-Process $py -ArgumentList $a -WorkingDirectory (Join-Path $R "backend") -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs "supervisor.stdout.log") -RedirectStandardError (Join-Path $logs "supervisor.stderr.log") -PassThru
Write-Host "supervisor started pid $($p.Id) (status: $py -m app.ops.supervisor --status, from backend\)"
