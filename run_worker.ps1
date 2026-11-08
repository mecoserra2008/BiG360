<#
.SYNOPSIS  BiG Equity 360 - start the worker (APScheduler nightly/weekly/monthly DAGs + job queue consumer).
.DESCRIPTION
  Crons come from contracts\jobs.yaml and are interpreted in Europe/Lisbon time (override: $env:BIG360_SCHEDULER_TZ).
  Only one worker can run at a time (data\state\worker.lock). Stop with Ctrl+C (graceful: running jobs finish/cancel).
.EXAMPLE   .\run_worker.ps1            .\run_worker.ps1 -NoSchedule            .\run_worker.ps1 -Once
#>
param(
    [switch]$Once,        # process queued requests once and exit
    [switch]$NoSchedule   # consume the queue only, no cron DAG runs
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found at $Py" }
$Host.UI.RawUI.WindowTitle = "BiG360 worker"
Set-Location (Join-Path $Root "backend")
$wArgs = @("-m", "app.scheduler.worker")
if ($Once) { $wArgs += "--once" }
if ($NoSchedule) { $wArgs += "--no-schedule" }
Write-Host "BiG Equity 360 worker (logs: data\logs\worker.log, per job: data\logs\jobs\)" -ForegroundColor DarkYellow
& $Py @wArgs
exit $LASTEXITCODE
