<#
.SYNOPSIS  Launch one resumable price-coverage task detached (survives the terminal / Claude session).
.EXAMPLE   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\price_coverage_launch.ps1 -Name cn_w0 -TaskArgs "cn --worker 0 --workers 3"
  Log: data\logs\price_coverage\<Name>.log (+ .err), PID: data\logs\price_coverage\<Name>.pid
  Re-running the same command resumes (done-files under data\state\price_coverage\). Skips if the PID is alive.
#>
param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$TaskArgs)
$Root = Split-Path $PSScriptRoot -Parent
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$LogD = Join-Path $Root "data\logs\price_coverage"
New-Item -ItemType Directory -Force $LogD | Out-Null
$pidFile = Join-Path $LogD "$Name.pid"
if (Test-Path $pidFile) {
    $old = [int](Get-Content $pidFile)
    $p = Get-Process -Id $old -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") { "already running: $Name pid $old"; exit 0 }
}
$env:PYTHONIOENCODING = "utf-8"
$argList = @("-m", "app.ingestion.price_coverage") + ($TaskArgs -split " ")
$proc = Start-Process -FilePath $Py -ArgumentList $argList -WorkingDirectory (Join-Path $Root "backend") `
    -RedirectStandardOutput (Join-Path $LogD "$Name.log") -RedirectStandardError (Join-Path $LogD "$Name.err") `
    -WindowStyle Hidden -PassThru
Set-Content $pidFile $proc.Id
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launched $Name pid $($proc.Id): $TaskArgs" | Add-Content (Join-Path $LogD "launches.log")
"launched $Name pid $($proc.Id)"
