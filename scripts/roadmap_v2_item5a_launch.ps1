<#
.SYNOPSIS  Launch one roadmap v2 item #5a task detached (futures term structures / cross-asset signals).
.EXAMPLE   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\roadmap_v2_item5a_launch.ps1 -Name item5a_backfill -TaskArgs "backfill"
.EXAMPLE   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\roadmap_v2_item5a_launch.ps1 -Name item5a_signals -Module app.analytics.cross_asset -TaskArgs "run"
  Log: data\logs\roadmap_v2\<Name>.log (+ .err), PID: data\logs\roadmap_v2\<Name>.pid
  State: data\state\roadmap_v2.json key "item5a". Re-running resumes (the fetch is idempotent: upsert per contract/date).
  Graceful stop of a fetch: create data\state\roadmap_v2_item5a.STOP (delete it before resuming).
#>
param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$TaskArgs,
      [string]$Module = "app.ingestion.futures_curve")
$Root = Split-Path $PSScriptRoot -Parent
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$LogD = Join-Path $Root "data\logs\roadmap_v2"
New-Item -ItemType Directory -Force $LogD | Out-Null
$pidFile = Join-Path $LogD "$Name.pid"
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile)
    $p = Get-Process -Id $old -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") { "already running: $Name pid $old"; exit 0 }
}
$env:PYTHONIOENCODING = "utf-8"
$argList = @("-u", "-m", $Module) + ($TaskArgs -split " ")
$proc = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory (Join-Path $Root "backend") `
    -RedirectStandardOutput (Join-Path $LogD "$Name.log") -RedirectStandardError (Join-Path $LogD "$Name.err") `
    -WindowStyle Hidden -PassThru
Set-Content $pidFile $proc.Id
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launched $Name pid $($proc.Id): $Module $TaskArgs" | Add-Content (Join-Path $LogD "launches.log")
"launched $Name pid $($proc.Id)"
