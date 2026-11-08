<#
.SYNOPSIS  Launch one resumable price-history task detached (survives the terminal / Claude session).
.EXAMPLE   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\price_history_launch.ps1 -Name backfill -TaskArgs "backfill"
.EXAMPLE   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\price_history_launch.ps1 -Name compact -TaskArgs "compact --table prices_staging"
  Log: data\logs\price_history\<Name>.log (+ .err), PID: data\logs\price_history\<Name>.pid
  Re-running the same command resumes (batch plan data\state\price_history\plan.json, summary data\state\price_history.json).
  Skips if the PID is alive. Graceful stop: create data\state\price_history\STOP (delete it before resuming).
#>
param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$TaskArgs)
$Root = Split-Path $PSScriptRoot -Parent
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$LogD = Join-Path $Root "data\logs\price_history"
New-Item -ItemType Directory -Force $LogD | Out-Null
$pidFile = Join-Path $LogD "$Name.pid"
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile)
    $p = Get-Process -Id $old -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") { "already running: $Name pid $old"; exit 0 }
}
$env:PYTHONIOENCODING = "utf-8"
$argList = @("-u", "-m", "app.ingestion.price_history") + ($TaskArgs -split " ")
$proc = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory (Join-Path $Root "backend") `
    -RedirectStandardOutput (Join-Path $LogD "$Name.log") -RedirectStandardError (Join-Path $LogD "$Name.err") `
    -WindowStyle Hidden -PassThru
Set-Content $pidFile $proc.Id
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launched $Name pid $($proc.Id): $TaskArgs" | Add-Content (Join-Path $LogD "launches.log")
"launched $Name pid $($proc.Id)"
