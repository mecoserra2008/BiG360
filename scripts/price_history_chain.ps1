<#
.SYNOPSIS  Price-history post-backfill chain (docs/PRICE_HISTORY_PLAN.md H5/H6), resumable per step.
  ref_universe (registers FX / commodity / vol instruments, no Yahoo refresh) -> prices_finalize (all regions, chunked)
  -> identity_resolve -> build_serving_db -> inventory --tag after -> footprint.
  Done-markers: data\state\price_history\chain_<step>.done (delete one to re-run that step). Log: data\logs\price_history\chain.log
.EXAMPLE  Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File scripts\price_history_chain.ps1' -WindowStyle Hidden
.EXAMPLE  pass 2 (re-finalize some regions, skip reference steps):
          ... -File scripts\price_history_chain.ps1 -Tag chain2 -Regions APAC,CN,LATAM -Skip ref_universe,identity_resolve
#>
param([string]$Tag = "chain", [string]$Regions = "", [string]$Skip = "")
$Root = Split-Path $PSScriptRoot -Parent
$Py   = Join-Path $Root ".venv\Scripts\python.exe"
$Be   = Join-Path $Root "backend"
$St   = Join-Path $Root "data\state\price_history"
$Log  = Join-Path $Root "data\logs\price_history\$Tag.log"
New-Item -ItemType Directory -Force $St, (Split-Path $Log) | Out-Null
Set-Content (Join-Path (Split-Path $Log) "$Tag.pid") $PID
$skipList = @($Skip -split "," | Where-Object { $_ })
$env:PYTHONIOENCODING = "utf-8"
function Step([string]$Name, [string[]]$PyArgs) {
    $done = Join-Path $St "$($Tag)_$Name.done"
    if ($skipList -contains $Name) { "$(Get-Date -Format s) skip $Name (-Skip)" | Add-Content $Log; return $true }
    if (Test-Path $done) { "$(Get-Date -Format s) skip $Name (done)" | Add-Content $Log; return $true }
    "$(Get-Date -Format s) start $Name : $($PyArgs -join ' ')" | Add-Content $Log
    $t0 = Get-Date
    Push-Location $Be
    & $Py @PyArgs *>> $Log
    $rc = $LASTEXITCODE
    Pop-Location
    $sec = [int]((Get-Date) - $t0).TotalSeconds
    "$(Get-Date -Format s) end $Name rc=$rc seconds=$sec" | Add-Content $Log
    if ($rc -eq 0) { Set-Content $done "$(Get-Date -Format s) seconds=$sec"; return $true }
    return $false
}
$finArgs = @("-m", "app.cli", "jobs", "run", "prices_finalize", "--force")
if ($Regions) { $finArgs += @("--param", "regions=$Regions") }
$steps = @(
    @("ref_universe",     @("-m", "app.cli", "jobs", "run", "ref_universe", "--param", "refresh_yf=false", "--force")),
    @("prices_finalize",  $finArgs),
    @("identity_resolve", @("-m", "app.cli", "jobs", "run", "identity_resolve", "--force")),
    @("build_serving_db", @("-m", "app.cli", "jobs", "run", "build_serving_db", "--force")),
    @("inventory_after",  @("-m", "app.ingestion.price_history", "inventory", "--tag", "after")),
    @("footprint",        @("-m", "app.ingestion.price_history", "footprint")),
    @("compare",          @("-m", "app.ingestion.price_history", "compare")),
    @("reconcile",        @("-m", "app.ingestion.price_history", "reconcile"))
)
foreach ($s in $steps) {
    if (-not (Step $s[0] $s[1])) { "$(Get-Date -Format s) chain stopped at $($s[0])" | Add-Content $Log; exit 1 }
}
"$(Get-Date -Format s) $Tag done" | Add-Content $Log
