<#
.SYNOPSIS  BiG Equity 360 - start the FastAPI server (http://127.0.0.1:8000, docs at /docs).
.EXAMPLE   .\run_api.ps1            .\run_api.ps1 -Port 8001 -Reload
#>
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$Reload
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found at $Py (create it and pip install -r backend\requirements.txt)" }
$Host.UI.RawUI.WindowTitle = "BiG360 API :$Port"
Set-Location (Join-Path $Root "backend")
$uvArgs = @("-m", "uvicorn", "app.api.main:app", "--host", $BindHost, "--port", "$Port")
if ($Reload) { $uvArgs += @("--reload", "--reload-dir", "app") }
Write-Host "BiG Equity 360 API -> http://${BindHost}:$Port  (docs: /docs)" -ForegroundColor DarkYellow
& $Py @uvArgs
exit $LASTEXITCODE
