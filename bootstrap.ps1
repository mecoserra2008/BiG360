<#
.SYNOPSIS  BiG Equity 360 - one-shot setup for a fresh clone: Python venv, locked dependencies, desktop UI build,
           .env from the template, seed files into the data folder, environment check; optionally the first data load
           and a logon task so the background services start by themselves.
.DESCRIPTION
  The data lake (data\) is not in git. Everything in it is fetched by the app's own jobs, except the curated seed
  files, which are committed under data\reference\seeds\ and copied into BIG360_DATA_DIR if that points elsewhere.
  After this script, start the app with BiG360.cmd (tray + supervisor: API, worker, filler, studio lane, realtime,
  startup sync). On an empty lake the worker's catch-up and the filler campaigns begin fetching immediately.
.EXAMPLE   .\bootstrap.ps1                       # setup only; then run BiG360.cmd
           .\bootstrap.ps1 -FirstLoad            # also run the first data load now (hours; MVP universe)
           .\bootstrap.ps1 -RegisterLogonTask    # start BiG360 in the background at every Windows logon
           .\bootstrap.ps1 -SkipFrontend -Python "C:\Python313\python.exe"
#>
param(
    [switch]$FirstLoad,
    [switch]$RegisterLogonTask,
    [switch]$SkipFrontend,
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Step([string]$msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor DarkYellow }
function Check([string]$what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

# 1. Python 3.13 virtual environment + locked dependencies
Step "Python environment"
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) {
    if ($Python) { & $Python -m venv $Venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.13 -m venv $Venv }
    else { & python -m venv $Venv }
    Check "venv creation"
}
& $Py -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.13 is required (packaging\requirements.lock.txt is pinned for 3.13). Pass -Python <path to python 3.13>." }
& $Py -m pip install --upgrade pip; Check "pip upgrade"
& $Py -m pip install -r (Join-Path $Root "packaging\requirements.lock.txt"); Check "pip install"

# 2. Desktop UI (served by the API in app mode)
if (-not $SkipFrontend) {
    Step "Frontend build (Node 20+)"
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm not found: install Node.js 20+ or rerun with -SkipFrontend" }
    Push-Location (Join-Path $Root "frontend")
    npm ci; Check "npm ci"
    npm run build:app; Check "npm run build:app"
    Pop-Location
}

# 3. Secrets template
Step ".env"
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "Created .env from .env.example. Fill at least FRED_APIKEY, Polygon_APIKEY and SEC_USER_AGENT (a real e-mail)." -ForegroundColor Yellow
} else { Write-Host ".env exists (left untouched)" }

# 4. Seed files into the data folder (only needed when BIG360_DATA_DIR is outside the repo)
Step "Seed files"
$DataDir = if ($env:BIG360_DATA_DIR) { $env:BIG360_DATA_DIR } else { Join-Path $Root "data" }
$SeedSrc = Join-Path $Root "data\reference\seeds"
$SeedDst = Join-Path $DataDir "reference\seeds"
if ((Resolve-Path -LiteralPath $SeedSrc).Path -ne [IO.Path]::GetFullPath($SeedDst)) {
    New-Item -ItemType Directory -Force $SeedDst | Out-Null
    Get-ChildItem $SeedSrc -Filter *.csv | ForEach-Object {
        $t = Join-Path $SeedDst $_.Name
        if (-not (Test-Path $t)) { Copy-Item $_.FullName $t; Write-Host "  copied $($_.Name)" }
    }
} else { Write-Host "Seeds already in place ($SeedSrc)" }

# 5. Environment check (missing keys are reported, not fatal)
Step "Doctor"
Push-Location (Join-Path $Root "backend")
& $Py -m app.cli doctor
Pop-Location

# 6. Optional first data load in this window (otherwise the supervisor's worker catch-up and filler do it)
if ($FirstLoad) {
    Step "First data load (MVP universe; this takes hours)"
    Push-Location (Join-Path $Root "backend")
    & $Py -m app.cli jobs run-dag nightly --universe mvp; Check "nightly DAG"
    & $Py -m app.cli jobs run backfill_prices; Check "price backfill"
    & $Py -m app.cli jobs run-dag weekly --universe mvp; Check "weekly DAG"
    & $Py -m app.cli db build-serving; Check "serving DB build"
    Pop-Location
}

# 7. Optional: start the background services at every logon
if ($RegisterLogonTask) {
    Step "Logon task"
    $action = New-ScheduledTaskAction -Execute (Join-Path $Root "BiG360.cmd") -Argument "--background" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName "BiG360" -Action $action -Trigger $trigger -Settings $settings -Description "BiG Equity 360 tray + supervisor (API, worker, filler, sync)" -Force | Out-Null
    Write-Host "Registered scheduled task 'BiG360' (at logon, --background). Remove: Unregister-ScheduledTask -TaskName BiG360"
}

Step "Done"
Write-Host "Start:  .\BiG360.cmd            (tray app; dashboard opens when ready, http://127.0.0.1:8010)"
Write-Host "Stop:   .\BiG360.cmd --quit"
Write-Host "Status: cd backend; ..\.venv\Scripts\python.exe -m app.cli jobs status"
