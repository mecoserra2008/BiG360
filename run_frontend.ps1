<#
.SYNOPSIS  BiG Equity 360 - start the Vite dev server (http://localhost:5173).
.DESCRIPTION
  By default the frontend uses frontend\.env (VITE_USE_MOCK=true -> in-browser mock data).
  -Live sets VITE_USE_MOCK=false for this session so the UI calls the API at -ApiUrl.
.EXAMPLE   .\run_frontend.ps1          .\run_frontend.ps1 -Live
#>
param(
    [switch]$Live,
    [string]$ApiUrl = "http://localhost:8000",
    [switch]$Fallback     # with -Live: serve mock data for endpoints the API does not implement yet (501)
)
$ErrorActionPreference = "Stop"
$Front = Join-Path $PSScriptRoot "frontend"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm not found on PATH (install Node.js 20+)" }
$Host.UI.RawUI.WindowTitle = "BiG360 frontend"
Set-Location $Front
if (-not (Test-Path (Join-Path $Front "node_modules"))) {
    Write-Host "node_modules missing -> npm install" -ForegroundColor DarkYellow
    npm install
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if ($Live) {
    $env:VITE_USE_MOCK = "false"
    $env:VITE_API_URL = $ApiUrl
    if ($Fallback) { $env:VITE_MOCK_FALLBACK = "true" }
    Write-Host "frontend -> live API $ApiUrl" -ForegroundColor DarkYellow
}
npm run dev
exit $LASTEXITCODE
